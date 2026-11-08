import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from pwcs2_bot.highlight_api import API_PREFIX, create_highlight_app
from pwcs2_bot.highlight_delivery import (
    HighlightPreDispatchError,
    HighlightVideoDelivery,
)
from pwcs2_bot.highlight_jobs import HighlightJobStore
from pwcs2_bot.highlight_protocol import (
    HighlightProtocolError,
    HighlightSegment,
    RequestAuthenticator,
    sha256_bytes,
    signed_headers,
)


def _mp4() -> bytes:
    return b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


def _highlight_context() -> dict:
    return {
        "schema_version": 1,
        "source": "perfectworld",
        "highlight_id": "0123456789abcdef01234567",
        "match_id": "PVP@test",
        "map_name": "de_mirage",
        "player_steam_id": "76561197960266101",
        "round_number": 12,
        "tags": ["4k", "headshot_streak"],
        "window": {
            "start_seconds": 501.25,
            "end_seconds": 527.0,
            "start_tick": 100,
            "end_tick": 500,
        },
        "score": 82,
        "score_breakdown": {"4k": 78, "headshot_streak": 44},
        "stats": {
            "kills": 4,
            "headshots": 3,
            "knife_kills": 0,
            "headshot_streak": 2,
        },
        "clutch": None,
        "events": [
            {
                "tick": 250,
                "time_seconds": 510.0,
                "victim_steam_id": "76561198000000001",
                "weapon": "ak47",
                "headshot": True,
            }
        ],
    }


def _load_windows_worker():
    name = "windows_highlight_worker"
    if name in sys.modules:
        return sys.modules[name]
    source = Path(__file__).parents[1] / "deploy" / "windows-highlight-worker" / "worker.py"
    spec = importlib.util.spec_from_file_location(name, source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _worker_config(module, tmp_path: Path, *, mode: str = "csdm"):
    return module.WorkerConfig(
        api_base_url="http://127.0.0.1:18791/internal/highlights/v1/",
        worker_id="pc-01",
        secret_env="NIUNIU_HIGHLIGHT_WORKER_SECRET",
        workspace_dir=tmp_path / "jobs",
        renderer_mode=mode,
        csdm_executable="csdm.exe",
        ffmpeg_executable="",
        steam_inf_path=None,
        renderer_command=("renderer.exe", "{demo}", "{output}"),
        renderer_timeout_seconds=7200,
        lease_seconds=1800,
        lease_renew_interval_seconds=300,
        poll_seconds=20,
        max_demo_bytes=2 * 1024**3,
        max_video_bytes=24 * 1024**2,
        keep_successful_jobs=False,
    )


def test_windows_worker_config_matches_server_video_limits(tmp_path: Path) -> None:
    module = _load_windows_worker()
    payload = {
        "api_base_url": "http://127.0.0.1:18791/internal/highlights/v1/",
        "worker_id": "pc-01",
        "workspace_dir": str(tmp_path / "jobs"),
        "max_video_bytes": 24 * 1024**2,
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    assert module.WorkerConfig.load(config_path).max_video_bytes == 24 * 1024**2

    payload["max_video_bytes"] = 96 * 1024**2 + 1
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(module.WorkerError, match="video size must be between"):
        module.WorkerConfig.load(config_path)


def test_windows_worker_attaches_input_threads_before_focusing_cs2() -> None:
    module = _load_windows_worker()

    class Kernel32:
        @staticmethod
        def GetCurrentThreadId() -> int:
            return 10

    class User32:
        def __init__(self) -> None:
            self.foreground = 12
            self.attachments: list[tuple[int, int, bool]] = []
            self.keys: list[tuple[int, int]] = []

        def GetForegroundWindow(self) -> int:
            return self.foreground

        @staticmethod
        def GetWindowThreadProcessId(hwnd: int, _process_id) -> int:
            return 30 if hwnd == 99 else 20

        def AttachThreadInput(self, current: int, other: int, attach: bool) -> bool:
            self.attachments.append((current, other, bool(attach)))
            return True

        @staticmethod
        def IsIconic(_hwnd: int) -> bool:
            return False

        @staticmethod
        def ShowWindowAsync(_hwnd: int, _mode: int) -> bool:
            return True

        def keybd_event(self, key: int, _scan: int, flags: int, _extra: int) -> None:
            self.keys.append((key, flags))

        @staticmethod
        def BringWindowToTop(_hwnd: int) -> bool:
            return True

        @staticmethod
        def SetWindowPos(*_args) -> bool:
            return True

        def SetForegroundWindow(self, hwnd: int) -> bool:
            self.foreground = hwnd
            return True

        @staticmethod
        def SetActiveWindow(_hwnd: int) -> int:
            return 99

        @staticmethod
        def SetFocus(_hwnd: int) -> int:
            return 99

    user32 = User32()

    focused = module._focus_cs2_window(
        99,
        user32=user32,
        kernel32=Kernel32(),
        monotonic=lambda: 0.0,
        sleep=lambda _seconds: None,
    )

    assert focused is True
    assert user32.attachments == [
        (10, 20, True),
        (10, 30, True),
        (10, 30, False),
        (10, 20, False),
    ]
    assert user32.keys == [(0x12, 0), (0x12, 0x0002)]


def test_windows_worker_rejects_removed_local_directory_watcher(tmp_path: Path) -> None:
    module = _load_windows_worker()
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "api_base_url": "http://127.0.0.1:18791/internal/highlights/v1/",
                "worker_id": "pc-01",
                "workspace_dir": str(tmp_path / "jobs"),
                "watch_directories": [str(tmp_path / "demo")],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(module.WorkerError, match="directory watching was removed"):
        module.WorkerConfig.load(config_path)


def test_hmac_authentication_rejects_nonce_replay() -> None:
    secret = b"a" * 32
    body = b'{"lease_seconds":900}'
    headers = signed_headers(
        secret,
        worker_id="pc-01",
        method="POST",
        path="/internal/highlights/v1/jobs/claim",
        content_sha256=sha256_bytes(body),
        timestamp=1_700_000_000,
        nonce="1" * 32,
    )
    auth = RequestAuthenticator({"pc-01": secret}, now=lambda: 1_700_000_000)
    worker = auth.authenticate(
        method="POST",
        path="/internal/highlights/v1/jobs/claim",
        headers=headers,
    )
    assert worker.worker_id == "pc-01"
    with pytest.raises(HighlightProtocolError, match="already been used"):
        auth.authenticate(
            method="POST",
            path="/internal/highlights/v1/jobs/claim",
            headers=headers,
        )


def test_job_store_leases_demo_and_accepts_verified_mp4(tmp_path: Path) -> None:
    demo = tmp_path / "source.dem"
    demo.write_bytes(b"HL2DEMO" + b"x" * 100)
    store = HighlightJobStore(tmp_path / "store")
    job_id = store.enqueue_demo(
        group_id="987654",
        match_id="PVP@test",
        steam_id="76561197960266101",
        demo_path=demo,
        segments=(HighlightSegment(100, 500, "四杀", 10),),
        context=_highlight_context(),
    )
    claimed = store.claim("pc-01", lease_seconds=600)
    assert claimed is not None and claimed.job_id == job_id
    assert claimed.context == _highlight_context()
    assert claimed.as_worker_payload()["context"]["map_name"] == "de_mirage"
    leased_demo, digest, size = store.demo_for_lease(
        job_id,
        worker_id="pc-01",
        lease_token=claimed.lease_token,
    )
    assert leased_demo.read_bytes() == demo.read_bytes()
    assert digest == sha256_bytes(demo.read_bytes())
    assert size == demo.stat().st_size
    store.record_manifest(
        job_id,
        worker_id="pc-01",
        lease_token=claimed.lease_token,
        manifest={"job_id": job_id, "segments": [{"start_tick": 100, "end_tick": 500}]},
    )
    temporary = store.tmp_root / f"{job_id}.mp4.part"
    temporary.write_bytes(_mp4())
    completed = store.complete(
        job_id,
        worker_id="pc-01",
        lease_token=claimed.lease_token,
        temporary_video_path=temporary,
    )
    assert completed.output_path.read_bytes() == _mp4()
    if os.name != "nt":
        for directory in (store.root, store.demo_root, store.output_root, store.tmp_root):
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        for private_file in (store.database_path, leased_demo, completed.output_path):
            assert stat.S_IMODE(private_file.stat().st_mode) == 0o600
    assert store.completed()[0].job_id == job_id
    assert store.claim_delivery(job_id) is True
    assert store.claim_delivery(job_id) is False
    assert store.completed() == ()
    store.mark_delivered(job_id)
    assert store.completed() == ()


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and modes only")
def test_job_store_can_share_only_its_mount_root_and_render_output(tmp_path: Path) -> None:
    gid = os.getgid()
    store = HighlightJobStore(
        tmp_path / "store",
        shared_root_gid=gid,
        shared_output_gid=gid,
    )

    assert store.root.stat().st_gid == gid
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o770
    assert stat.S_IMODE(store.demo_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.tmp_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.output_root.stat().st_mode) == 0o750


def test_demo_backlog_measures_actual_managed_files(tmp_path: Path) -> None:
    store = HighlightJobStore(tmp_path / "store")
    one = tmp_path / "one.dem"
    two = tmp_path / "two.dem"
    one.write_bytes(b"a" * 17)
    two.write_bytes(b"b" * 23)
    for index, demo in enumerate((one, two), start=1):
        store.enqueue_demo(
            group_id="987654",
            match_id=f"PVP@backlog-{index}",
            steam_id="76561197960266101",
            demo_path=demo,
            segments=(HighlightSegment(100, 500, "四杀", 10),),
            job_id=f"backlog0{index}",
        )
    (store.demo_root / "ignored.part").write_bytes(b"c" * 100)
    (store.tmp_root / "unknown-upload.part").write_bytes(b"d" * 31)

    backlog = store.demo_backlog()

    assert backlog.demo_count == 2
    assert backlog.demo_bytes == 171
    assert backlog.can_accept(
        reserved_bytes=29,
        max_demo_count=3,
        max_demo_bytes=200,
    )
    assert not backlog.can_accept(
        reserved_bytes=30,
        max_demo_count=3,
        max_demo_bytes=200,
    )
    assert not backlog.can_accept(
        reserved_bytes=0,
        max_demo_count=2,
        max_demo_bytes=100,
    )

    with store._connect() as connection:  # noqa: SLF001 - terminal-state setup
        connection.execute(
            "UPDATE highlight_worker_jobs SET status = 'failed' WHERE job_id = ?",
            ("backlog01",),
        )
        connection.execute(
            """
            UPDATE highlight_worker_jobs
            SET status = 'completed', delivery_state = 'unknown'
            WHERE job_id = ?
            """,
            ("backlog02",),
        )
    terminal_backlog = store.demo_backlog()
    assert terminal_backlog.demo_count == 0
    assert terminal_backlog.demo_bytes == 171


def test_missing_completed_output_becomes_visible_terminal_delivery_failure(
    tmp_path: Path,
) -> None:
    demo = tmp_path / "source.dem"
    demo.write_bytes(b"HL2DEMO" + b"x" * 100)
    store = HighlightJobStore(tmp_path / "store")
    job_id = store.enqueue_demo(
        group_id="987654",
        match_id="PVP@missing-output",
        steam_id="76561197960266101",
        demo_path=demo,
        segments=(HighlightSegment(100, 500, "四杀", 10),),
        job_id="missing01",
    )
    claimed = store.claim("pc-01")
    assert claimed is not None
    store.record_manifest(
        job_id,
        worker_id="pc-01",
        lease_token=claimed.lease_token,
        manifest={"job_id": job_id},
    )
    temporary = store.tmp_root / f"{job_id}.mp4.part"
    temporary.write_bytes(_mp4())
    completed = store.complete(
        job_id,
        worker_id="pc-01",
        lease_token=claimed.lease_token,
        temporary_video_path=temporary,
    )
    completed.output_path.unlink()

    assert store.completed() == ()
    failures = store.failed_deliveries()
    assert len(failures) == 1
    assert failures[0].job_id == job_id
    assert failures[0].last_error == "stored highlight output is unavailable"
    assert failures[0].demo_present is False
    assert failures[0].output_present is False


def test_pre_dispatch_failure_refuses_unsafe_path_but_still_becomes_terminal(
    tmp_path: Path,
) -> None:
    demo = tmp_path / "source.dem"
    demo.write_bytes(b"HL2DEMO" + b"x" * 100)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(_mp4())
    store = HighlightJobStore(tmp_path / "store")
    job_id = store.enqueue_demo(
        group_id="987654",
        match_id="PVP@unsafe-output",
        steam_id="76561197960266101",
        demo_path=demo,
        segments=(HighlightSegment(100, 500, "四杀", 10),),
        job_id="unsafe001",
    )
    with store._connect() as connection:  # noqa: SLF001 - corrupt-path fixture
        connection.execute(
            """
            UPDATE highlight_worker_jobs
            SET status = 'completed', output_path = ?, completed_at = ?
            WHERE job_id = ?
            """,
            (str(outside.resolve()), "2000-01-01T00:00:00+00:00", job_id),
        )

    assert store.completed() == ()
    assert outside.exists()
    failures = store.failed_deliveries()
    assert failures[0].job_id == job_id
    assert failures[0].last_error == "stored highlight output path is unsafe"
    assert failures[0].output_present is True


def test_expired_final_worker_attempt_is_marked_failed(tmp_path: Path) -> None:
    demo = tmp_path / "source.dem"
    demo.write_bytes(b"HL2DEMO" + b"x" * 100)
    store = HighlightJobStore(tmp_path / "store", max_attempts=2)
    job_id = store.enqueue_demo(
        group_id="987654",
        match_id="PVP@attempt-limit",
        steam_id="76561197960266101",
        demo_path=demo,
        segments=(HighlightSegment(100, 500, "四杀", 10),),
    )
    first = store.claim("pc-01")
    assert first is not None
    with store._connect() as connection:  # noqa: SLF001 - lease expiry setup
        connection.execute(
            "UPDATE highlight_worker_jobs SET lease_until = 0 WHERE job_id = ?",
            (job_id,),
        )
    second = store.claim("pc-02")
    assert second is not None
    with store._connect() as connection:  # noqa: SLF001 - lease expiry setup
        connection.execute(
            "UPDATE highlight_worker_jobs SET lease_until = 0 WHERE job_id = ?",
            (job_id,),
        )

    assert store.claim("pc-03") is None
    with store._connect() as connection:  # noqa: SLF001 - terminal-state assertion
        row = connection.execute(
            "SELECT status, attempts, last_error FROM highlight_worker_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    assert row is not None
    assert row["status"] == "failed"
    assert row["attempts"] == 2
    assert "lease expired" in row["last_error"]


def test_ambiguous_delivery_is_quarantined_from_automatic_retry(tmp_path: Path) -> None:
    demo = tmp_path / "source.dem"
    demo.write_bytes(b"HL2DEMO" + b"x" * 100)
    store = HighlightJobStore(tmp_path / "store")
    job_id = store.enqueue_demo(
        group_id="987654",
        match_id="PVP@timeout",
        steam_id="76561197960266101",
        demo_path=demo,
        segments=(HighlightSegment(100, 500, "四杀", 10),),
    )
    claimed = store.claim("pc-01")
    assert claimed is not None
    store.record_manifest(
        job_id,
        worker_id="pc-01",
        lease_token=claimed.lease_token,
        manifest={"job_id": job_id},
    )
    temporary = store.tmp_root / f"{job_id}.mp4.part"
    temporary.write_bytes(_mp4())
    store.complete(
        job_id,
        worker_id="pc-01",
        lease_token=claimed.lease_token,
        temporary_video_path=temporary,
    )

    assert store.claim_delivery(job_id) is True
    store.mark_delivery_unknown(job_id, "socket read timed out")
    assert store.completed() == ()
    assert store.claim_delivery(job_id) is False
    with store._connect() as connection:  # noqa: SLF001 - state-machine assertion
        row = connection.execute(
            "SELECT status, delivery_state, delivery_last_error "
            "FROM highlight_worker_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    assert row["status"] == "completed"
    assert row["delivery_state"] == "unknown"
    assert row["delivery_last_error"] == "socket read timed out"
    audit = store.unknown_deliveries()
    assert audit[0].job_id == job_id
    assert audit[0].state == "unknown"
    assert audit[0].demo_present is True
    assert audit[0].output_present is True
    for artifact in (
        store.demo_root / f"{job_id}.dem",
        store.output_root / f"{job_id}.mp4",
    ):
        os.utime(artifact, (1_000_000_000, 1_000_000_000))
    pruned = store.prune(
        failed_retention_seconds=0,
        delivered_retention_seconds=0,
        orphan_retention_seconds=0,
    )
    assert pruned.failed_jobs == pruned.delivered_jobs == pruned.orphan_files == ()
    after_prune = store.unknown_deliveries()[0]
    assert after_prune.state == "unknown"
    assert after_prune.demo_present is True
    assert after_prune.output_present is True
    with pytest.raises(HighlightProtocolError, match="requires confirmation"):
        store.retry_unknown_delivery(job_id)
    store.retry_unknown_delivery(job_id, confirmed_not_delivered=True)
    assert store.completed()[0].job_id == job_id


def test_stale_unknown_delivery_archives_files_without_retry(tmp_path: Path) -> None:
    demo = tmp_path / "source.dem"
    demo.write_bytes(b"HL2DEMO" + b"x" * 100)
    store = HighlightJobStore(tmp_path / "store")
    job_id = store.enqueue_demo(
        group_id="987654",
        match_id="PVP@archive",
        steam_id="76561197960266101",
        demo_path=demo,
        segments=(HighlightSegment(100, 500, "四杀", 10),),
    )
    claimed = store.claim("pc-01")
    assert claimed is not None
    store.record_manifest(
        job_id,
        worker_id="pc-01",
        lease_token=claimed.lease_token,
        manifest={"job_id": job_id},
    )
    temporary = store.tmp_root / f"{job_id}.mp4.part"
    temporary.write_bytes(_mp4())
    completed = store.complete(
        job_id,
        worker_id="pc-01",
        lease_token=claimed.lease_token,
        temporary_video_path=temporary,
    )
    assert store.claim_delivery(job_id) is True
    store.mark_delivery_unknown(job_id, "response lost")
    with store._connect() as connection:  # noqa: SLF001 - age the audit record
        connection.execute(
            "UPDATE highlight_worker_jobs SET delivery_started_at = ? WHERE job_id = ?",
            ("2000-01-01T00:00:00+00:00", job_id),
        )

    assert store.archive_unknown_delivery_files(older_than_seconds=0) == (job_id,)
    assert not completed.output_path.exists()
    assert store.completed() == ()
    audit = store.unknown_deliveries()
    assert audit[0].state == "unknown_archived"
    assert audit[0].demo_present is False
    assert audit[0].output_present is False
    with pytest.raises(HighlightProtocolError, match="cannot be retried"):
        store.retry_unknown_delivery(job_id, confirmed_not_delivered=True)


def test_stale_in_flight_claim_becomes_unknown_not_retryable(tmp_path: Path) -> None:
    demo = tmp_path / "source.dem"
    demo.write_bytes(b"HL2DEMO" + b"x" * 100)
    store = HighlightJobStore(tmp_path / "store")
    job_id = store.enqueue_demo(
        group_id="987654",
        match_id="PVP@crash",
        steam_id="76561197960266101",
        demo_path=demo,
        segments=(HighlightSegment(100, 500, "四杀", 10),),
    )
    claimed = store.claim("pc-01")
    assert claimed is not None
    store.record_manifest(
        job_id,
        worker_id="pc-01",
        lease_token=claimed.lease_token,
        manifest={"job_id": job_id},
    )
    temporary = store.tmp_root / f"{job_id}.mp4.part"
    temporary.write_bytes(_mp4())
    store.complete(
        job_id,
        worker_id="pc-01",
        lease_token=claimed.lease_token,
        temporary_video_path=temporary,
    )
    assert store.claim_delivery(job_id) is True
    with store._connect() as connection:  # noqa: SLF001 - simulate a dead bot
        connection.execute(
            "UPDATE highlight_worker_jobs SET delivery_started_at = ? WHERE job_id = ?",
            ("2000-01-01T00:00:00+00:00", job_id),
        )

    assert store.quarantine_stale_delivery_claims(older_than_seconds=0) == (job_id,)
    assert store.completed() == ()
    audit = store.unknown_deliveries()
    assert audit[0].state == "unknown"
    assert audit[0].last_error == "stale in-flight claim after restart"


def test_prune_retains_failed_demo_for_seven_days_then_keeps_audit_row(
    tmp_path: Path,
) -> None:
    demo = tmp_path / "source.dem"
    demo.write_bytes(b"HL2DEMO" + b"x" * 100)
    store = HighlightJobStore(tmp_path / "store", max_attempts=1)
    job_id = store.enqueue_demo(
        group_id="987654",
        match_id="PVP@failed-retention",
        steam_id="76561197960266101",
        demo_path=demo,
        segments=(HighlightSegment(100, 500, "四杀", 10),),
        job_id="failed01",
    )
    claimed = store.claim("pc-01")
    assert claimed is not None
    store.fail(
        job_id,
        worker_id="pc-01",
        lease_token=claimed.lease_token,
        error="renderer unavailable",
    )
    managed_demo = store.demo_root / f"{job_id}.dem"
    assert managed_demo.exists()

    assert store.prune().failed_jobs == ()
    with store._connect() as connection:  # noqa: SLF001 - age retention fixture
        connection.execute(
            "UPDATE highlight_worker_jobs SET updated_at = ? WHERE job_id = ?",
            ("2000-01-01T00:00:00+00:00", job_id),
        )
    result = store.prune(failed_retention_seconds=0)
    assert result.failed_jobs == (job_id,)
    assert not managed_demo.exists()
    with store._connect() as connection:  # noqa: SLF001 - audit assertion
        row = connection.execute(
            "SELECT status, demo_path, demo_sha256, last_error "
            "FROM highlight_worker_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    assert row["status"] == "failed"
    assert row["demo_path"] == ""
    assert len(row["demo_sha256"]) == 64
    assert row["last_error"] == "renderer unavailable"


def test_terminal_worker_failure_does_not_retry_an_immutable_demo(tmp_path: Path) -> None:
    demo = tmp_path / "source.dem"
    demo.write_bytes(b"HL2DEMO" + b"x" * 100)
    store = HighlightJobStore(tmp_path / "store", max_attempts=3)
    job_id = store.enqueue_demo(
        group_id="987654",
        match_id="PVP@version-mismatch",
        steam_id="76561197960266101",
        demo_path=demo,
        segments=(HighlightSegment(100, 500, "四杀", 10),),
        job_id="version01",
    )
    claimed = store.claim("pc-01")
    assert claimed is not None

    store.fail(
        job_id,
        worker_id="pc-01",
        lease_token=claimed.lease_token,
        error="Demo and CS2 versions do not match",
        terminal=True,
    )

    assert store.claim("pc-01") is None
    with store._connect() as connection:  # noqa: SLF001 - terminal-state assertion
        row = connection.execute(
            "SELECT status, attempts FROM highlight_worker_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    assert row["status"] == "failed"
    assert row["attempts"] == 1


def test_prune_removes_old_delivered_residue_without_deleting_job(tmp_path: Path) -> None:
    demo = tmp_path / "source.dem"
    demo.write_bytes(b"HL2DEMO" + b"x" * 100)
    store = HighlightJobStore(tmp_path / "store")
    job_id = store.enqueue_demo(
        group_id="987654",
        match_id="PVP@delivered-retention",
        steam_id="76561197960266101",
        demo_path=demo,
        segments=(HighlightSegment(100, 500, "四杀", 10),),
        job_id="deliver01",
    )
    claimed = store.claim("pc-01")
    assert claimed is not None
    store.record_manifest(
        job_id,
        worker_id="pc-01",
        lease_token=claimed.lease_token,
        manifest={"job_id": job_id},
    )
    temporary = store.tmp_root / f"{job_id}.mp4.part"
    temporary.write_bytes(_mp4())
    completed = store.complete(
        job_id,
        worker_id="pc-01",
        lease_token=claimed.lease_token,
        temporary_video_path=temporary,
    )
    assert store.claim_delivery(job_id) is True
    store.mark_delivered(job_id)
    with store._connect() as connection:  # noqa: SLF001 - age retention fixture
        connection.execute(
            "UPDATE highlight_worker_jobs SET updated_at = ? WHERE job_id = ?",
            ("2000-01-01T00:00:00+00:00", job_id),
        )

    result = store.prune(delivered_retention_seconds=0)
    assert result.delivered_jobs == (job_id,)
    assert not completed.output_path.exists()
    assert not (store.demo_root / f"{job_id}.dem").exists()
    with store._connect() as connection:  # noqa: SLF001 - audit assertion
        row = connection.execute(
            "SELECT status, demo_path, output_path, output_sha256 "
            "FROM highlight_worker_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    assert row["status"] == "delivered"
    assert row["demo_path"] == row["output_path"] == ""
    assert len(row["output_sha256"]) == 64


def test_prune_only_removes_old_strictly_named_unreferenced_artifacts(
    tmp_path: Path,
) -> None:
    store = HighlightJobStore(tmp_path / "store")
    orphan_demo = store.demo_root / "orphan01.dem"
    orphan_video = store.output_root / "orphan02.mp4"
    invalid_name = store.demo_root / "short.dem"
    partial = store.output_root / "orphan03.mp4.part"
    old_demo_part = store.tmp_root / "tmpjob01.0123456789abcdef.dem.part"
    old_video_part = store.tmp_root / "tmpjob02.fedcba9876543210.mp4.part"
    fresh_part = store.tmp_root / "tmpjob03.0123456789abcdef.dem.part"
    for path in (
        orphan_demo,
        orphan_video,
        invalid_name,
        partial,
        old_demo_part,
        old_video_part,
    ):
        path.write_bytes(b"orphan")
        os.utime(path, (1_000_000_000, 1_000_000_000))
    fresh_part.write_bytes(b"active copy")

    result = store.prune(orphan_retention_seconds=0)
    assert result.orphan_files == (
        "orphan01.dem",
        "orphan02.mp4",
        "tmp/tmpjob01.0123456789abcdef.dem.part",
        "tmp/tmpjob02.fedcba9876543210.mp4.part",
    )
    assert not orphan_demo.exists()
    assert not orphan_video.exists()
    assert not old_demo_part.exists()
    assert not old_video_part.exists()
    assert invalid_name.exists()
    assert partial.exists()
    assert fresh_part.exists()


def test_prune_never_follows_a_managed_looking_temporary_symlink(
    tmp_path: Path,
) -> None:
    store = HighlightJobStore(tmp_path / "store")
    outside = tmp_path / "outside.dem"
    outside.write_bytes(b"must remain")
    link = store.tmp_root / "tmpjob04.0123456789abcdef.dem.part"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable on this platform")

    result = store.prune(orphan_retention_seconds=0)
    assert result.orphan_files == ()
    assert link.is_symlink()
    assert outside.read_bytes() == b"must remain"


def test_prune_refuses_a_job_path_that_names_another_jobs_artifact(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.dem"
    second = tmp_path / "second.dem"
    first.write_bytes(b"HL2DEMO" + b"a" * 100)
    second.write_bytes(b"HL2DEMO" + b"b" * 100)
    store = HighlightJobStore(tmp_path / "store", max_attempts=1)
    first_id = store.enqueue_demo(
        group_id="987654",
        match_id="PVP@first",
        steam_id="76561197960266101",
        demo_path=first,
        segments=(HighlightSegment(100, 500, "四杀", 10),),
        job_id="unsafe01",
    )
    second_id = store.enqueue_demo(
        group_id="987654",
        match_id="PVP@second",
        steam_id="76561197960266101",
        demo_path=second,
        segments=(HighlightSegment(100, 500, "四杀", 10),),
        job_id="victim01",
    )
    claimed = store.claim("pc-01")
    assert claimed is not None and claimed.job_id == first_id
    store.fail(
        first_id,
        worker_id="pc-01",
        lease_token=claimed.lease_token,
        error="failed",
    )
    victim_path = store.demo_root / f"{second_id}.dem"
    with store._connect() as connection:  # noqa: SLF001 - corrupt-path fixture
        connection.execute(
            "UPDATE highlight_worker_jobs SET demo_path = ?, updated_at = ? WHERE job_id = ?",
            (str(victim_path), "2000-01-01T00:00:00+00:00", first_id),
        )

    result = store.prune(failed_retention_seconds=0)
    assert result.failed_jobs == ()
    assert result.skipped_jobs == (first_id,)
    assert victim_path.exists()

    outside = tmp_path / "outside-managed-storage.dem"
    outside.write_bytes(b"must not be deleted")
    with store._connect() as connection:  # noqa: SLF001 - traversal fixture
        connection.execute(
            "UPDATE highlight_worker_jobs SET demo_path = ? WHERE job_id = ?",
            (str(outside), first_id),
        )
    escaped = store.prune(failed_retention_seconds=0)
    assert escaped.skipped_jobs == (first_id,)
    assert outside.read_bytes() == b"must not be deleted"


async def test_known_pre_dispatch_failure_can_release_for_retry(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(_mp4())
    ready = iter((True, False))
    actions: list[str] = []

    async def call_action(action: str, _params: dict) -> dict:
        actions.append(action)
        return {"status": "ok"}

    delivery = HighlightVideoDelivery(call_action, ready_check=lambda: next(ready))
    prepared = await delivery.prepare("987654", video)
    with pytest.raises(HighlightPreDispatchError, match="not connected"):
        await delivery.dispatch(prepared)
    assert actions == []

    demo = tmp_path / "source.dem"
    demo.write_bytes(b"HL2DEMO" + b"x" * 100)
    store = HighlightJobStore(tmp_path / "store")
    job_id = store.enqueue_demo(
        group_id="987654",
        match_id="PVP@preflight",
        steam_id="76561197960266101",
        demo_path=demo,
        segments=(HighlightSegment(100, 500, "四杀", 10),),
    )
    claimed = store.claim("pc-01")
    assert claimed is not None
    store.record_manifest(
        job_id,
        worker_id="pc-01",
        lease_token=claimed.lease_token,
        manifest={"job_id": job_id},
    )
    temporary = store.tmp_root / f"{job_id}.mp4.part"
    temporary.write_bytes(_mp4())
    store.complete(
        job_id,
        worker_id="pc-01",
        lease_token=claimed.lease_token,
        temporary_video_path=temporary,
    )
    assert store.claim_delivery(job_id) is True
    store.release_delivery_claim(job_id, "OneBot transport is not connected")
    assert store.completed()[0].job_id == job_id


async def test_private_api_requires_hmac_and_completes_upload(tmp_path: Path) -> None:
    secret = b"s" * 32
    store = HighlightJobStore(tmp_path / "store")
    demo = tmp_path / "source.dem"
    demo.write_bytes(b"HL2DEMO" + b"x" * 100)
    job_id = store.enqueue_demo(
        group_id="987654",
        match_id="PVP@api",
        steam_id="76561197960266101",
        demo_path=demo,
        segments=(HighlightSegment(100, 500, "四杀", 10),),
        context=_highlight_context(),
    )
    client = TestClient(
        TestServer(create_highlight_app(store=store, worker_secrets={"pc-01": secret}))
    )
    await client.start_server()
    try:
        unauthorized = await client.get(f"{API_PREFIX}/health")
        assert unauthorized.status == 401

        claim_path = f"{API_PREFIX}/jobs/claim"
        claim_body = b'{"lease_seconds":600}'
        claim_response = await client.post(
            claim_path,
            data=claim_body,
            headers={
                **signed_headers(
                    secret,
                    worker_id="pc-01",
                    method="POST",
                    path=claim_path,
                    content_sha256=sha256_bytes(claim_body),
                ),
                "Content-Type": "application/json",
            },
        )
        assert claim_response.status == 200
        claimed = await claim_response.json()
        assert claimed["job_id"] == job_id
        assert claimed["context"]["tags"] == ["4k", "headshot_streak"]
        lease = claimed["lease_token"]

        demo_path = f"{API_PREFIX}/jobs/{job_id}/demo"
        demo_response = await client.get(
            demo_path,
            headers={
                **signed_headers(
                    secret,
                    worker_id="pc-01",
                    method="GET",
                    path=demo_path,
                ),
                "X-Highlight-Lease": lease,
            },
        )
        assert demo_response.status == 200
        assert await demo_response.read() == demo.read_bytes()

        manifest_path = f"{API_PREFIX}/jobs/{job_id}/manifest"
        manifest_body = json.dumps({"job_id": job_id}, separators=(",", ":")).encode()
        manifest_response = await client.put(
            manifest_path,
            data=manifest_body,
            headers={
                **signed_headers(
                    secret,
                    worker_id="pc-01",
                    method="PUT",
                    path=manifest_path,
                    content_sha256=sha256_bytes(manifest_body),
                ),
                "X-Highlight-Lease": lease,
                "Content-Type": "application/json",
            },
        )
        assert manifest_response.status == 200

        lease_path = f"{API_PREFIX}/jobs/{job_id}/lease"
        lease_body = b'{"lease_seconds":600}'
        lease_response = await client.put(
            lease_path,
            data=lease_body,
            headers={
                **signed_headers(
                    secret,
                    worker_id="pc-01",
                    method="PUT",
                    path=lease_path,
                    content_sha256=sha256_bytes(lease_body),
                ),
                "X-Highlight-Lease": lease,
                "Content-Type": "application/json",
            },
        )
        assert lease_response.status == 200
        renewed = await lease_response.json()
        assert renewed["status"] == "renewed"
        assert renewed["lease_until"] > 0

        video_path = f"{API_PREFIX}/jobs/{job_id}/video"
        video = _mp4()
        video_response = await client.put(
            video_path,
            data=video,
            headers={
                **signed_headers(
                    secret,
                    worker_id="pc-01",
                    method="PUT",
                    path=video_path,
                    content_sha256=sha256_bytes(video),
                ),
                "X-Highlight-Lease": lease,
                "Content-Type": "video/mp4",
            },
        )
        assert video_response.status == 200
        assert store.completed()[0].job_id == job_id
    finally:
        await client.close()


async def test_video_delivery_uses_onebot_video_not_nanobot_image(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(_mp4())
    actions: list[tuple[str, dict]] = []

    async def call_action(action: str, params: dict) -> dict:
        actions.append((action, params))
        return {"status": "ok"}

    delivery = HighlightVideoDelivery(call_action, allowed_groups={"987654"})
    prepared = await delivery.prepare("987654", video, caption="本场高光")
    assert actions == []
    await delivery.dispatch(prepared)
    assert actions[0][0] == "send_group_msg"
    message = actions[0][1]["message"]
    assert message[0]["type"] == "video"
    assert message[0]["data"]["file"].startswith("base64://")
    assert message[1] == {"type": "text", "data": {"text": "本场高光"}}


async def test_video_delivery_prefers_napcat_read_only_media_path(tmp_path: Path) -> None:
    video = tmp_path / "local-0123456789abcdef01234567.mp4"
    video.write_bytes(_mp4())

    async def call_action(_action: str, _params: dict) -> dict:
        return {"status": "ok"}

    delivery = HighlightVideoDelivery(
        call_action,
        allowed_groups={"987654"},
        napcat_media_root="/app/highlights",
    )
    prepared = await delivery.prepare("987654", video)
    assert prepared.params["message"][0]["data"]["file"] == (
        "file:///app/highlights/local-0123456789abcdef01234567.mp4"
    )


def test_windows_worker_writes_bounded_manifest_and_argv(tmp_path: Path) -> None:
    module = _load_windows_worker()
    job = {
        "job_id": "12345678abcdef",
        "match_id": "PVP@test",
        "steam_id": "76561197960266101",
        "segments": [{"start_tick": 100, "end_tick": 500, "label": "四杀", "score": 10.0}],
        "context": _highlight_context(),
    }
    manifest, commands, output = module._write_render_files(job, tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["demo_file"] == "input.dem"
    assert payload["highlight_context"]["events"][0]["weapon"] == "ak47"
    assert "demo_gototick 100" in commands.read_text(encoding="utf-8")
    assert "ak47" not in commands.read_text(encoding="utf-8")
    argv = module._renderer_arguments(
        ("renderer.exe", "--manifest", "{manifest}", "--output", "{output}"),
        demo=tmp_path / "input.dem",
        manifest=manifest,
        commands=commands,
        output=output,
    )
    assert argv == ["renderer.exe", "--manifest", str(manifest), "--output", str(output)]


def test_csdm_config_matches_documented_video_schema(tmp_path: Path) -> None:
    module = _load_windows_worker()
    config = _worker_config(module, tmp_path)
    demo = tmp_path / "match.dem"
    demo.write_bytes(b"HL2DEMO")
    job = {
        "steam_id": "76561197960266101",
        "segments": [
            {"start_tick": 100, "end_tick": 500, "label": "4K", "score": 82.0},
            {"start_tick": 900, "end_tick": 1200, "label": "ACE", "score": 95.0},
        ],
    }
    path, output_directory = module._write_csdm_config(job, demo, tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["demoPath"] == str(demo.resolve())
    assert payload["outputFolderPath"] == str(output_directory.resolve())
    assert payload["recordingSystem"] == "CS"
    assert payload["recordingOutput"] == "video"
    assert payload["encoderSoftware"] == "FFmpeg"
    assert (payload["width"], payload["height"], payload["framerate"]) == (1280, 720, 30)
    assert payload["trueView"] is False
    assert payload["concatenateSequences"] is True
    assert payload["ffmpegSettings"]["videoContainer"] == "mp4"
    assert payload["ffmpegSettings"]["videoCodec"] == "libx264"
    assert payload["ffmpegSettings"]["audioCodec"] == "aac"
    assert payload["ffmpegSettings"]["customLocationEnabled"] is False
    assert payload["ffmpegSettings"]["customExecutableLocation"] == ""
    assert payload["ffmpegSettings"]["outputParameters"] == (
        "-pix_fmt yuv420p -crf 26 -maxrate 3M -bufsize 6M -movflags +faststart"
    )
    assert [item["number"] for item in payload["sequences"]] == [1, 2]
    first = payload["sequences"][0]
    assert first["playerVoicesEnabled"] is False
    assert first["recordAudio"] is True
    assert first["playersOptions"] == []
    assert first["playerCameras"] == [
        {
            "tick": 100,
            "playerSteamId": "76561197960266101",
            "playerName": "",
        }
    ]
    assert module._csdm_analyze_arguments(config, demo, "perfectworld") == [
        "csdm.exe",
        "analyze",
        str(demo),
        "--source",
        "perfectworld",
        "--force",
    ]
    assert module._csdm_video_arguments(config, path) == [
        "csdm.exe",
        "video",
        "--config-file",
        str(path),
    ]

    custom_path, _ = module._write_csdm_config(
        job,
        demo,
        tmp_path / "custom-ffmpeg",
        ffmpeg_executable=r"C:\Tools\ffmpeg.exe",
    )
    custom = json.loads(custom_path.read_text(encoding="utf-8"))
    assert custom["ffmpegSettings"]["customLocationEnabled"] is True
    assert custom["ffmpegSettings"]["customExecutableLocation"] == (r"C:\Tools\ffmpeg.exe")


def test_csdm_splits_a_long_multikill_round_around_each_kill(tmp_path: Path) -> None:
    module = _load_windows_worker()
    demo = tmp_path / "long-round.dem"
    demo.write_bytes(b"HL2DEMO")
    kill_ticks = [169_415, 173_562, 175_955]
    job = {
        "steam_id": "76561199540763498",
        "segments": [
            {
                "start_tick": 168_903,
                "end_tick": 176_595,
                "label": "3K",
                "score": 60.0,
            }
        ],
        "context": {
            "source": "perfectworld",
            "highlight": {"events": [{"tick": tick} for tick in kill_ticks]},
        },
    }

    path, _ = module._write_csdm_config(job, demo, tmp_path)

    sequences = json.loads(path.read_text(encoding="utf-8"))["sequences"]
    assert len(sequences) == 3
    assert sum(item["endTick"] - item["startTick"] for item in sequences) <= 60 * 64
    for tick, sequence in zip(kill_ticks, sequences, strict=True):
        assert sequence["startTick"] == tick - 4 * 64
        assert sequence["endTick"] == tick + 8 * 64
        assert sequence["startTick"] <= tick <= sequence["endTick"]


def test_csdm_rejects_an_oversized_segment_without_event_anchors(tmp_path: Path) -> None:
    module = _load_windows_worker()
    demo = tmp_path / "unanchored.dem"
    demo.write_bytes(b"HL2DEMO")
    job = {
        "steam_id": "76561199540763498",
        "segments": [
            {"start_tick": 100, "end_tick": 5_000, "label": "3K", "score": 60.0}
        ],
        "context": {"source": "perfectworld"},
    }

    with pytest.raises(module.WorkerError, match="60 second render budget"):
        module._write_csdm_config(job, demo, tmp_path)


def test_csdm_job_requires_supported_context_source(tmp_path: Path) -> None:
    module = _load_windows_worker()
    config = _worker_config(module, tmp_path)
    payload = {
        "job_id": "12345678abcdef",
        "group_id": "987654",
        "match_id": "PVP@test",
        "steam_id": "76561197960266101",
        "lease_token": "x" * 32,
        "lease_until": 1_900_000_000,
        "demo": {
            "download_path": "/internal/highlights/v1/jobs/12345678abcdef/demo",
            "size": 100,
            "sha256": "a" * 64,
        },
        "segments": [{"start_tick": 100, "end_tick": 500}],
        "context": {},
    }
    with pytest.raises(module.WorkerError, match="missing source"):
        module._validate_job(payload, config)
    payload["context"] = {"source": "faceit"}
    with pytest.raises(module.WorkerError, match="unsupported demo source"):
        module._validate_job(payload, config)
    payload["context"] = {"source": "valve"}
    assert module._validate_job(payload, config)["source"] == "valve"


def test_csdm_plugin_log_is_derived_from_configured_game(tmp_path: Path) -> None:
    module = _load_windows_worker()
    steam_inf = tmp_path / "game" / "csgo" / "steam.inf"
    steam_inf.parent.mkdir(parents=True)
    steam_inf.write_text("ClientVersion=1\n", encoding="utf-8")

    assert module._csdm_plugin_log_path(steam_inf) == (
        tmp_path / "game" / "bin" / "win64" / "csdm.log"
    )


def test_csdm_demo_hook_reloads_once_after_websocket_connects(tmp_path: Path) -> None:
    module = _load_windows_worker()
    demo = tmp_path / "match.dem"
    demo.write_bytes(b"PBDEMS2")
    logs = iter(
        (
            "Connected to WebSocket server.\n",
            "Connected to WebSocket server.\n",
            "Connected to WebSocket server.\nHooked FrameStageNotify\n"
            "[0] Demo playback started, sequences 1\n",
        )
    )
    commands: list[str] = []

    class Process:
        @staticmethod
        def poll():
            return None

    module._ensure_csdm_demo_hook(
        Process(),
        demo,
        tmp_path / "csdm.log",
        (0, 0),
        command_sender=commands.append,
        log_reader=lambda *_args: next(logs),
        monotonic=iter((0.0, 0.0, 0.5, 1.0)).__next__,
        sleep=lambda _seconds: None,
        connection_grace_seconds=0,
        timeout_seconds=10,
    )

    assert commands == [
        f'disconnect; hideconsole; playdemo "{str(demo.resolve()).replace(chr(92), "/")}"'
    ]


def test_csdm_demo_hook_does_not_reload_when_initial_hook_succeeds(tmp_path: Path) -> None:
    module = _load_windows_worker()
    demo = tmp_path / "match.dem"
    demo.write_bytes(b"PBDEMS2")
    commands: list[str] = []

    class Process:
        @staticmethod
        def poll():
            return None

    module._ensure_csdm_demo_hook(
        Process(),
        demo,
        tmp_path / "csdm.log",
        (0, 0),
        command_sender=commands.append,
        log_reader=lambda *_args: (
            "Connected to WebSocket server.\nHooked FrameStageNotify\n"
            "[0] Demo playback started, sequences 1\n"
        ),
        monotonic=lambda: 0.0,
        sleep=lambda _seconds: None,
    )

    assert commands == []


def test_local_demo_version_check_rejects_stale_demo_before_launch(tmp_path: Path) -> None:
    module = _load_windows_worker()
    csgo = tmp_path / "game" / "csgo"
    replays = csgo / "replays"
    replays.mkdir(parents=True)
    demo = replays / "match.dem"
    demo.write_bytes(b"PBDEMS2\0fixture")
    (csgo / "steam.inf").write_text("ClientVersion=2000885\n", encoding="utf-8")

    class Parser:
        def __init__(self, path: str) -> None:
            assert path == str(demo)

        def parse_header(self) -> dict[str, str]:
            return {"game_directory": "/opt/srcds/cs2/csgo_v2000510/csgo"}

    with pytest.raises(module.WorkerError, match=r"Demo 2000510.*installed game 2000885"):
        module._ensure_demo_matches_installed_game(demo, parser_factory=Parser)


def test_local_demo_version_check_accepts_current_demo(tmp_path: Path) -> None:
    module = _load_windows_worker()
    csgo = tmp_path / "csgo"
    replays = csgo / "replays"
    replays.mkdir(parents=True)
    demo = replays / "fresh.dem"
    demo.write_bytes(b"PBDEMS2\0fixture")
    (csgo / "steam.inf").write_text("ClientVersion=2000885\r\n", encoding="utf-8")

    class Parser:
        def __init__(self, _path: str) -> None:
            pass

        def parse_header(self) -> dict[str, str]:
            return {"game_directory": r"C:\server\csgo_v2000885\csgo"}

    assert module._ensure_demo_matches_installed_game(demo, parser_factory=Parser) == (
        "2000885",
        "2000885",
    )


def test_external_perfectworld_demo_uses_configured_steam_inf(tmp_path: Path) -> None:
    module = _load_windows_worker()
    demo = tmp_path / "perfectworld.dem"
    demo.write_bytes(b"PBDEMS2\0fixture")
    steam_inf = tmp_path / "steam.inf"
    steam_inf.write_text("PatchVersion=1.41.7.6\n", encoding="utf-8")

    class Parser:
        def __init__(self, _path: str) -> None:
            pass

        def parse_header(self) -> dict[str, str]:
            return {
                "game_directory": "/data/cs2-linux-server/cs2/game/csgo",
                "patch_version": "14172",
            }

    with pytest.raises(module.WorkerError, match=r"Demo 14172.*installed game 14176"):
        module._ensure_demo_matches_installed_game(
            demo,
            parser_factory=Parser,
            steam_inf_path=steam_inf,
        )


def test_long_command_renews_lease_without_using_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_windows_worker()
    created: list[tuple[list[str], dict]] = []

    class Process:
        def __init__(self) -> None:
            self.polls = iter((None, None, 0))

        def poll(self):
            return next(self.polls)

        def terminate(self) -> None:
            raise AssertionError("successful command must not be terminated")

    def popen(arguments, **kwargs):
        created.append((arguments, kwargs))
        return Process()

    clock = iter((0.0, 20.0, 40.0))
    renewals: list[bool] = []
    startups: list[object] = []
    taskkills: list[bool] = []
    monkeypatch.setenv("NIUNIU_HIGHLIGHT_WORKER_SECRET", "do-not-inherit")
    monkeypatch.setenv("HIGHLIGHT_RENDERER_SAFE_VALUE", "preserved")
    module._run_command(
        ["csdm.exe", "video", "--config-file", "video.json"],
        cwd=tmp_path,
        timeout_seconds=120,
        renew_lease=lambda: renewals.append(True),
        renew_interval_seconds=10,
        popen_factory=popen,
        monotonic=lambda: next(clock),
        sleep=lambda _seconds: None,
        process_tree_platform="nt",
        taskkill_runner=lambda *_args, **_kwargs: taskkills.append(True),
        startup_action=startups.append,
    )
    assert renewals == [True, True]
    assert created[0][0][0] == "csdm.exe"
    assert created[0][1]["shell"] is False
    assert "NIUNIU_HIGHLIGHT_WORKER_SECRET" not in created[0][1]["env"]
    assert created[0][1]["env"]["HIGHLIGHT_RENDERER_SAFE_VALUE"] == "preserved"
    assert taskkills == []
    assert len(startups) == 1


def test_packaged_csdm_cmd_uses_electron_node_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_windows_worker()
    install = tmp_path / "cs-demo-manager"
    resources = install / "resources"
    resources.mkdir(parents=True)
    cmd = install / "csdm.cmd"
    electron = install / "cs-demo-manager.exe"
    asar = resources / "app.asar"
    cli = asar / "cli.js"
    cmd.write_text("@echo off\n", encoding="utf-8")
    electron.write_bytes(b"exe")
    asar.write_bytes(b"asar")
    created: list[tuple[list[str], dict]] = []

    class Process:
        pid = 123

        def poll(self):
            return 0

        def terminate(self) -> None:
            raise AssertionError("successful command must not be terminated")

    monkeypatch.setenv("NIUNIU_HIGHLIGHT_WORKER_SECRET", "do-not-inherit")
    module._run_command(
        [str(cmd), "analyze", "match.dem", "--force"],
        cwd=tmp_path,
        timeout_seconds=30,
        popen_factory=lambda arguments, **kwargs: created.append((arguments, kwargs)) or Process(),
        monotonic=lambda: 0.0,
        sleep=lambda _seconds: None,
        process_tree_platform="nt",
    )

    assert created[0][0] == [
        str(electron.resolve()),
        str(cli.resolve()),
        "analyze",
        "match.dem",
        "--force",
    ]
    assert created[0][1]["shell"] is False
    assert created[0][1]["env"]["ELECTRON_RUN_AS_NODE"] == "1"
    assert "NIUNIU_HIGHLIGHT_WORKER_SECRET" not in created[0][1]["env"]


def test_windows_worker_rejects_arbitrary_batch_launcher(tmp_path: Path) -> None:
    module = _load_windows_worker()
    launcher = tmp_path / "renderer.cmd"
    launcher.write_text("@echo off\n", encoding="utf-8")

    with pytest.raises(module.WorkerError, match="only the packaged csdm.cmd"):
        module._run_command(
            [str(launcher), "video"],
            cwd=tmp_path,
            timeout_seconds=30,
            process_tree_platform="nt",
        )


def test_windows_command_timeout_kills_complete_process_tree(tmp_path: Path) -> None:
    module = _load_windows_worker()
    taskkills: list[tuple[list[str], dict]] = []
    waits: list[int] = []

    class Process:
        pid = 4242

        def poll(self):
            return None

        def wait(self, *, timeout: int) -> None:
            waits.append(timeout)

        def terminate(self) -> None:
            raise AssertionError("taskkill must handle a valid Windows PID")

        def kill(self) -> None:
            raise AssertionError("taskkill must handle a valid Windows PID")

    def taskkill(arguments, **kwargs):
        taskkills.append((arguments, kwargs))

    clock = iter((0.0, 6.0))
    with pytest.raises(module.WorkerError, match="configured timeout"):
        module._run_command(
            ["csdm.exe", "video", "--config-file", "video.json"],
            cwd=tmp_path,
            timeout_seconds=5,
            popen_factory=lambda *_args, **_kwargs: Process(),
            monotonic=lambda: next(clock),
            sleep=lambda _seconds: None,
            process_tree_platform="nt",
            taskkill_runner=taskkill,
        )

    assert taskkills == [
        (
            ["taskkill", "/PID", "4242", "/T", "/F"],
            {
                "shell": False,
                "stdin": module.subprocess.DEVNULL,
                "stdout": module.subprocess.DEVNULL,
                "stderr": module.subprocess.DEVNULL,
                "check": False,
                "timeout": 15,
            },
        )
    ]
    assert waits == [10]


def test_windows_command_cancellation_kills_complete_process_tree(
    tmp_path: Path,
) -> None:
    module = _load_windows_worker()
    taskkills: list[list[str]] = []

    class Process:
        pid = 777

        def poll(self):
            return None

        def wait(self, *, timeout: int) -> None:
            assert timeout == 10

    clock = iter((0.0, 1.0))
    with pytest.raises(KeyboardInterrupt):
        module._run_command(
            ["csdm.exe", "video", "--config-file", "video.json"],
            cwd=tmp_path,
            timeout_seconds=120,
            popen_factory=lambda *_args, **_kwargs: Process(),
            monotonic=lambda: next(clock),
            sleep=lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
            process_tree_platform="nt",
            taskkill_runner=lambda arguments, **_kwargs: taskkills.append(arguments),
        )

    assert taskkills == [["taskkill", "/PID", "777", "/T", "/F"]]


def test_windows_command_lease_failure_kills_complete_process_tree(
    tmp_path: Path,
) -> None:
    module = _load_windows_worker()
    taskkills: list[list[str]] = []

    class Process:
        pid = 888

        def poll(self):
            return None

        def wait(self, *, timeout: int) -> None:
            assert timeout == 10

    def fail_renewal() -> None:
        raise RuntimeError("lease rejected")

    clock = iter((0.0, 10.0))
    with pytest.raises(module.WorkerError, match="cannot renew highlight lease"):
        module._run_command(
            ["csdm.exe", "video", "--config-file", "video.json"],
            cwd=tmp_path,
            timeout_seconds=120,
            renew_lease=fail_renewal,
            renew_interval_seconds=5,
            popen_factory=lambda *_args, **_kwargs: Process(),
            monotonic=lambda: next(clock),
            sleep=lambda _seconds: None,
            process_tree_platform="nt",
            taskkill_runner=lambda arguments, **_kwargs: taskkills.append(arguments),
        )

    assert taskkills == [["taskkill", "/PID", "888", "/T", "/F"]]


def test_windows_tree_cleanup_rejects_non_integer_pid() -> None:
    module = _load_windows_worker()
    taskkills: list[bool] = []
    calls: list[object] = []

    class Process:
        pid = "4242"

        def terminate(self) -> None:
            calls.append("terminate")

        def wait(self, *, timeout: int) -> None:
            calls.append(("wait", timeout))

    module._stop_process(
        Process(),
        platform_name="nt",
        taskkill_runner=lambda *_args, **_kwargs: taskkills.append(True),
    )

    assert taskkills == []
    assert calls == ["terminate", ("wait", 10)]


def test_worker_secret_is_consumed_from_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_windows_worker()
    monkeypatch.setenv("CUSTOM_HIGHLIGHT_SECRET", "s" * 40)

    secret = module._consume_worker_secret("CUSTOM_HIGHLIGHT_SECRET")

    assert secret == b"s" * 40
    assert "CUSTOM_HIGHLIGHT_SECRET" not in module.os.environ


def test_windows_worker_removes_failed_job_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_windows_worker()
    config = _worker_config(module, tmp_path)
    raw_job = {
        "job_id": "12345678abcdef",
        "group_id": "987654",
        "match_id": "PVP@test",
        "steam_id": "76561197960266101",
        "lease_token": "x" * 32,
        "lease_until": 1_900_000_000,
        "demo": {
            "download_path": "/internal/highlights/v1/jobs/12345678abcdef/demo",
            "size": 100,
            "sha256": "a" * 64,
        },
        "segments": [{"start_tick": 100, "end_tick": 500}],
        "context": {"source": "perfectworld"},
    }

    class API:
        failed: list[tuple[str, bool]] = []

        def claim(self):
            return raw_job

        def download_demo(self, _job, destination):
            destination.write_bytes(b"HL2DEMO")

        def upload_manifest(self, _job, _manifest):
            return None

        def fail(self, _job, error, *, terminal=False):
            self.failed.append((error, terminal))

    def fail_render(*_args, **_kwargs):
        raise module.WorkerError("CS2 launch failed")

    monkeypatch.setattr(module, "_render_csdm", fail_render)
    api = API()
    with pytest.raises(module.WorkerError, match="CS2 launch failed"):
        module.process_once(api, config)

    assert api.failed == [("CS2 launch failed", False)]
    assert not (config.workspace_dir / raw_job["job_id"]).exists()


@pytest.mark.parametrize("report_fails", [False, True])
def test_demo_version_mismatch_is_reported_as_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report_fails: bool,
) -> None:
    module = _load_windows_worker()
    config = _worker_config(module, tmp_path)
    raw_job = {
        "job_id": "12345678abcdef",
        "group_id": "987654",
        "match_id": "PVP@test",
        "steam_id": "76561197960266101",
        "lease_token": "x" * 32,
        "lease_until": 1_900_000_000,
        "demo": {
            "download_path": "/internal/highlights/v1/jobs/12345678abcdef/demo",
            "size": 100,
            "sha256": "a" * 64,
        },
        "segments": [{"start_tick": 100, "end_tick": 500}],
        "context": {"source": "perfectworld"},
    }

    class API:
        failed: list[tuple[str, bool]] = []

        def claim(self):
            return raw_job

        def download_demo(self, _job, destination):
            destination.write_bytes(b"HL2DEMO")

        def fail(self, _job, error, *, terminal=False):
            self.failed.append((error, terminal))

    def mismatch(*_args, **_kwargs):
        raise module.WorkerError(
            "Demo and CS2 versions do not match (Demo 1, installed game 2)"
        )

    monkeypatch.setattr(module, "_ensure_demo_matches_installed_game", mismatch)
    api = API()
    if report_fails:
        def fail_report(*_args, **_kwargs):
            raise module.WorkerError("API unavailable")

        monkeypatch.setattr(api, "fail", fail_report)
        with pytest.raises(module.WorkerError, match="versions do not match"):
            module.process_once(api, config)
        assert api.failed == []
    else:
        assert module.process_once(api, config) is True

        assert api.failed == [
            ("Demo and CS2 versions do not match (Demo 1, installed game 2)", True)
        ]
    assert not (config.workspace_dir / raw_job["job_id"]).exists()


def test_worker_promotes_only_one_new_csdm_mp4(tmp_path: Path) -> None:
    module = _load_windows_worker()
    output_directory = tmp_path / "csdm-output"
    nested = output_directory / "video-id"
    nested.mkdir(parents=True)
    before = module._mp4_snapshot(output_directory)
    generated = nested / "sequence.mp4"
    generated.write_bytes(_mp4())
    destination = tmp_path / "highlight.mp4"
    promoted = module._promote_unique_new_mp4(
        output_directory,
        before,
        destination,
        max_video_bytes=1024,
    )
    assert promoted == destination
    assert destination.read_bytes() == _mp4()
    assert not generated.exists()

    (nested / "one.mp4").write_bytes(_mp4())
    (nested / "two.mp4").write_bytes(_mp4())
    with pytest.raises(module.WorkerError, match="exactly one"):
        module._promote_unique_new_mp4(
            output_directory,
            {},
            destination,
            max_video_bytes=1024,
        )


def test_local_demo_dry_flow_parses_highlight_candidates(tmp_path: Path) -> None:
    module = _load_windows_worker()
    steam_id = "76561197960266101"
    demo = tmp_path / "local.dem"
    demo.write_bytes(b"HL2DEMO")

    class Parser:
        def parse_event(self, name, other):
            assert name == "player_death"
            assert "total_rounds_played" in other
            return [
                {
                    "tick": 1000 + index * 64,
                    "total_rounds_played": 4,
                    "attacker_steamid": steam_id,
                    "user_steamid": f"7656119800000000{index}",
                    "headshot": index < 2,
                    "is_warmup_period": False,
                }
                for index in range(3)
            ]

    candidates = module._local_highlight_candidates(
        demo,
        steam_id,
        parser_factory=lambda _path: Parser(),
    )
    assert len(candidates) == 1
    assert candidates[0]["label"] == "Round 5 3K"
    assert candidates[0]["start_tick"] == 488
    assert candidates[0]["end_tick"] == 1768


def test_local_demo_candidate_and_render_stay_inside_one_minute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_windows_worker()
    steam_id = "76561197960266101"
    demo = tmp_path / "long-round.dem"
    demo.write_bytes(b"HL2DEMO")

    class Parser:
        def parse_event(self, _name, other):
            assert "total_rounds_played" in other
            return [
                {
                    "tick": tick,
                    "total_rounds_played": 4,
                    "attacker_steamid": steam_id,
                    "user_steamid": f"7656119800000{tick}",
                    "headshot": True,
                    "is_warmup_period": False,
                }
                for tick in (10_000, 11_500, 13_300)
            ]

    candidates = module._local_highlight_candidates(
        demo,
        steam_id,
        parser_factory=lambda _path: Parser(),
    )
    assert candidates[0]["end_tick"] - candidates[0]["start_tick"] <= 60 * 64

    config = _worker_config(module, tmp_path)
    captured: list[dict] = []

    def render(job, *_args, **_kwargs):
        captured.append(job)

    monkeypatch.setattr(module, "_render_csdm", render)
    monkeypatch.setattr(module, "_validated_video", lambda *_args, **_kwargs: ("a" * 64, 1))
    module._render_local(
        config,
        demo,
        steam_id,
        "valve",
        [
            {"start_tick": 100, "end_tick": 200, "label": "3K", "score": 60.0},
            {"start_tick": 300, "end_tick": 500, "label": "4K", "score": 90.0},
        ],
    )
    assert captured[0]["segments"] == [
        {"start_tick": 300, "end_tick": 500, "label": "4K", "score": 90.0}
    ]
