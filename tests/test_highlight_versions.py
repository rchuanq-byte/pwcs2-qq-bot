from dataclasses import replace
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_highlight_worker_protocol import _load_windows_worker, _worker_config

from pwcs2_bot.highlight_api import API_PREFIX, create_highlight_app
from pwcs2_bot.highlight_jobs import HighlightJobStore
from pwcs2_bot.highlight_protocol import (
    HighlightProtocolError,
    HighlightSegment,
    normalize_cs2_patch,
    sha256_bytes,
    signed_headers,
)


def _enqueue(store: HighlightJobStore, tmp_path: Path, patch: str) -> str:
    demo = tmp_path / "source.dem"
    demo.write_bytes(b"test demo")
    return store.enqueue_demo(
        group_id="987654",
        match_id=f"PVP@{patch}",
        steam_id="76561197960266101",
        demo_path=demo,
        segments=(HighlightSegment(100, 500),),
        demo_patch=patch,
    )


@pytest.mark.parametrize("value", ["abc14181", "1..41.8.1", True, {}, 14181.0, "0", "9" * 10])
def test_bad_patch_numbers_are_rejected(value) -> None:
    with pytest.raises(HighlightProtocolError, match="patch"):
        normalize_cs2_patch(value)


def test_patch_normalization() -> None:
    assert normalize_cs2_patch("1.41.8.1") == normalize_cs2_patch(14181) == "14181"
    assert normalize_cs2_patch(None) == ""


def test_claim_syncs_patch_even_when_idle_and_persists(tmp_path: Path) -> None:
    store = HighlightJobStore(tmp_path / "jobs")
    assert store.renderer_patches(fallback="14180") == ("14180",)
    assert store.claim("pc-01", renderer_patch="1.41.8.1") is None
    reopened = HighlightJobStore(store.root)
    assert reopened.renderer_patches(fallback="14180") == ("14181",)
    assert reopened.claim("pc-01", renderer_patch="14182") is None
    assert store.renderer_patches() == ("14182",)


def test_old_demo_is_skipped_without_lease_but_newer_demo_is_retained(tmp_path: Path) -> None:
    store = HighlightJobStore(tmp_path / "jobs")
    old = _enqueue(store, tmp_path, "14180")
    new = _enqueue(store, tmp_path, "14182")
    assert store.claim("pc-01", renderer_patch="14181") is None
    with store._connect() as connection:
        rows = {r["job_id"]: r for r in connection.execute("SELECT * FROM highlight_worker_jobs")}
    assert rows[old]["status"] == "failed"
    assert rows[old]["attempts"] == 0
    assert rows[new]["status"] == "queued"
    assert rows[new]["attempts"] == 0
    claimed = store.claim("pc-01", renderer_patch="14182")
    assert claimed is not None and claimed.job_id == new
    assert claimed.as_worker_payload()["demo"]["patch_version"] == "14182"


def test_matching_worker_can_claim_and_live_lease_is_not_revoked(tmp_path: Path) -> None:
    store = HighlightJobStore(tmp_path / "jobs")
    job_id = _enqueue(store, tmp_path, "14180")
    lease = store.claim("pc-01", renderer_patch="14180")
    assert lease is not None
    assert store.claim("pc-01", renderer_patch="14181") is None
    with store._connect() as connection:
        row = connection.execute(
            "SELECT status, lease_token_hash FROM highlight_worker_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
    assert row["status"] == "leased"
    assert row["lease_token_hash"]


def test_another_compatible_worker_protects_older_jobs(tmp_path: Path) -> None:
    store = HighlightJobStore(tmp_path / "jobs")
    assert store.claim("older-pc", renderer_patch="14180") is None
    job_id = _enqueue(store, tmp_path, "14180")
    assert store.claim("newer-pc", renderer_patch="14181") is None
    claimed = store.claim("older-pc", renderer_patch="14180")
    assert claimed is not None and claimed.job_id == job_id


def test_old_queue_without_patch_still_works(tmp_path: Path) -> None:
    store = HighlightJobStore(tmp_path / "jobs")
    job_id = _enqueue(store, tmp_path, "")
    claimed = store.claim("pc-01", renderer_patch="14181")
    assert claimed is not None and claimed.job_id == job_id


async def test_signed_claim_reports_version_but_unsigned_or_malformed_cannot(tmp_path: Path):
    store = HighlightJobStore(tmp_path / "jobs")
    secret = b"s" * 40
    client = TestClient(
        TestServer(create_highlight_app(store=store, worker_secrets={"pc-01": secret}))
    )
    async with client:
        path = f"{API_PREFIX}/jobs/claim"
        good_body = b'{"renderer_patch":"14181"}'
        response = await client.post(path, data=good_body)
        assert response.status == 401
        assert store.renderer_patches() == ()
        for body, status in [(b'{"renderer_patch":"bad14181"}', 400), (good_body, 204)]:
            response = await client.post(
                path,
                data=body,
                headers={
                    **signed_headers(
                        secret=secret,
                        worker_id="pc-01",
                        method="POST",
                        path=path,
                        content_sha256=sha256_bytes(body),
                    ),
                    "Content-Type": "application/json",
                },
            )
            assert response.status == status
            assert store.renderer_patches() == (() if status == 400 else ("14181",))


def test_worker_reads_version_again_after_update(tmp_path: Path, monkeypatch):
    module = _load_windows_worker()
    steam_inf = tmp_path / "steam.inf"
    config = replace(_worker_config(module, tmp_path), steam_inf_path=steam_inf)
    api = module.HighlightAPI(config, b"s" * 40)
    reports = []
    monkeypatch.setattr(
        api, "_request_json", lambda _method, _path, body, **_kw: reports.append(body)
    )
    for patch in ("1.41.8.0", "1.41.8.1"):
        steam_inf.write_text(f"PatchVersion={patch}\n")
        assert api.claim() is None
    assert [r["renderer_patch"] for r in reports] == ["14180", "14181"]


def test_newer_demo_is_not_a_terminal_failure() -> None:
    module = _load_windows_worker()
    assert not module._is_terminal_render_error(
        "Demo and CS2 versions do not match (Demo 14182, installed game 14181)"
    )
    assert not module._is_terminal_render_error("CS2 launch failed")
