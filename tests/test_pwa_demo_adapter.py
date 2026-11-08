from __future__ import annotations

import asyncio
import bz2
import io
import time
import zipfile
from pathlib import Path

import httpx
import pytest

from pwcs2_bot.demo_source import (
    DemoRequest,
    DemoSourceError,
    DemoSourceManager,
    PwaDemoProvider,
)
from pwcs2_bot.pwa_demo_adapter import (
    PWA_DEMO_ACCOUNT_STEAM_ID_ENV,
    PWA_DEMO_ENABLED_ENV,
    PWA_DEMO_PUBLIC_IP_ENV,
    PWA_DEMO_TOKEN_ENV,
    PwaDemoAdapterSettings,
    PwaDownloadAdapter,
    create_pwa_demo_provider,
    create_pwa_demo_provider_from_env,
)

_STEAM_ID = "76561197960266101"
_TARGET_STEAM_ID = "76561199415617880"
_TOKEN = "secret-pwa-token-value"
_DEMO = b"PBDEMS2\x00" + (b"demo-content" * 32)


class _FakeFunctions:
    def __init__(self, *, error: Exception | None = None, delay: float = 0) -> None:
        self.error = error
        self.delay = delay
        self.calls: list[tuple[str, str]] = []
        self.header_steam_ids: list[str] = []
        self.header_public_ips: list[str | None] = []

    def get_demo_url(self, match_id: str, access_token: str) -> str:
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        self.calls.append((match_id, access_token))
        return f"https://pwaweblogin.wmpvp.com/csgo/demo/{match_id}.dem?access_token={access_token}"

    def build_download_headers(
        self,
        steam_id: str,
        public_ip: str | None = None,
    ) -> dict[str, str]:
        self.header_steam_ids.append(steam_id)
        self.header_public_ips.append(public_ip)
        return {"X-PWA-SteamId": steam_id, "X-PWA-Signature": "secret-header"}


def _zip_bytes(name: str, payload: bytes) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, payload)
    return output.getvalue()


def _client_factory(payload: bytes, *, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=payload, request=request)

    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


def _adapter(
    functions: _FakeFunctions,
    payload: bytes,
    *,
    max_download_bytes: int = 1024 * 1024,
    max_demo_bytes: int = 1024 * 1024,
) -> PwaDownloadAdapter:
    return PwaDownloadAdapter(
        PwaDemoAdapterSettings(
            access_token=_TOKEN,
            max_download_bytes=max_download_bytes,
            max_demo_bytes=max_demo_bytes,
        ),
        functions=functions,
        client_factory=_client_factory(payload),
    )


def test_environment_source_is_disabled_by_default_without_importing_dependency() -> None:
    assert create_pwa_demo_provider_from_env({}) is None
    assert create_pwa_demo_provider_from_env({PWA_DEMO_ENABLED_ENV: "false"}) is None


def test_environment_source_requires_secret_only_when_enabled() -> None:
    with pytest.raises(DemoSourceError, match="access token"):
        create_pwa_demo_provider_from_env({PWA_DEMO_ENABLED_ENV: "true"})
    provider_without_override = create_pwa_demo_provider_from_env(
        {PWA_DEMO_ENABLED_ENV: "true", PWA_DEMO_TOKEN_ENV: _TOKEN}
    )
    assert provider_without_override is not None

    assert provider_without_override.name == "pwa"
    assert _TOKEN not in repr(provider_without_override)


def test_controller_factory_accepts_token_without_account_override() -> None:
    provider = create_pwa_demo_provider(_TOKEN)
    assert provider.name == "pwa"
    assert _TOKEN not in repr(provider)


def test_environment_source_accepts_explicit_public_ipv4() -> None:
    provider = create_pwa_demo_provider_from_env(
        {
            PWA_DEMO_ENABLED_ENV: "true",
            PWA_DEMO_TOKEN_ENV: _TOKEN,
            PWA_DEMO_PUBLIC_IP_ENV: "192.0.2.10",
            PWA_DEMO_ACCOUNT_STEAM_ID_ENV: _STEAM_ID,
        }
    )
    assert provider is not None


@pytest.mark.parametrize("value", ("not-an-ip", "127.0.0.1", "192.168.1.1", "::1"))
def test_settings_reject_non_public_ipv4(value: str) -> None:
    with pytest.raises(DemoSourceError, match="public IP"):
        PwaDemoAdapterSettings(access_token=_TOKEN, public_ip=value)


def test_settings_reject_invalid_account_steam_id() -> None:
    with pytest.raises(DemoSourceError, match="account SteamID64"):
        PwaDemoAdapterSettings(access_token=_TOKEN, account_steam_id="123")


@pytest.mark.asyncio
async def test_credential_account_steam_id_is_used_for_download_headers(
    tmp_path: Path,
) -> None:
    functions = _FakeFunctions()
    adapter = PwaDownloadAdapter(
        PwaDemoAdapterSettings(
            access_token=_TOKEN,
            public_ip="192.0.2.10",
            account_steam_id=_STEAM_ID,
            max_download_bytes=1024 * 1024,
            max_demo_bytes=1024 * 1024,
        ),
        functions=functions,
        client_factory=_client_factory(_DEMO),
    )
    staging = tmp_path / "staging"
    staging.mkdir()

    candidate = await adapter.fetch(
        DemoRequest("match-1", steam_id=_TARGET_STEAM_ID),
        staging,
    )

    assert candidate is not None
    assert functions.header_steam_ids == [_STEAM_ID]
    assert functions.header_public_ips == ["192.0.2.10"]


@pytest.mark.asyncio
async def test_namespaced_pvp_match_id_is_normalized_for_download(tmp_path: Path) -> None:
    functions = _FakeFunctions()
    staging = tmp_path / "staging"
    staging.mkdir()

    candidate = await _adapter(functions, _DEMO).fetch(
        DemoRequest("PVP@9212010326355278220", steam_id=_STEAM_ID),
        staging,
    )

    assert candidate is not None
    assert functions.calls == [("9212010326355278220", _TOKEN)]


@pytest.mark.asyncio
async def test_pwa_zip_download_is_ingested_by_demo_source_manager(tmp_path: Path) -> None:
    functions = _FakeFunctions()
    adapter = _adapter(functions, _zip_bytes("nested/match.dem", _DEMO))
    manager = DemoSourceManager(
        tmp_path / "managed",
        tmp_path / "manual",
        max_file_bytes=1024 * 1024,
        # Exercise the exact injected boundary used by the controller.
        providers=(PwaDemoProvider(adapter.fetch),),
    )

    artifact = await manager.acquire(
        DemoRequest("match-123", steam_id=_STEAM_ID),
        provider_name="pwa",
    )

    assert artifact is not None
    assert artifact.path.read_bytes() == _DEMO
    assert functions.calls == [("match-123", _TOKEN)]
    assert _TOKEN not in repr(adapter)
    assert not any((tmp_path / "managed" / ".staging").iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    (
        _DEMO,
        bz2.compress(_DEMO),
    ),
)
async def test_direct_and_bz2_payloads_are_supported(tmp_path: Path, payload: bytes) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    candidate = await _adapter(_FakeFunctions(), payload).fetch(
        DemoRequest("match-1", steam_id=_STEAM_ID),
        staging,
    )
    assert candidate is not None
    assert candidate.read_bytes() == _DEMO


@pytest.mark.asyncio
async def test_archive_path_traversal_is_rejected(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    adapter = _adapter(_FakeFunctions(), _zip_bytes("../escape.dem", _DEMO))
    with pytest.raises(DemoSourceError, match="unsafe path"):
        await adapter.fetch(DemoRequest("match-1", steam_id=_STEAM_ID), staging)
    assert not (tmp_path / "escape.dem").exists()


@pytest.mark.asyncio
async def test_download_and_unpacked_limits_are_enforced(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    with pytest.raises(DemoSourceError, match="download exceeds"):
        await _adapter(
            _FakeFunctions(),
            _DEMO,
            max_download_bytes=16,
        ).fetch(DemoRequest("match-1", steam_id=_STEAM_ID), staging)

    with pytest.raises(DemoSourceError, match="configured size"):
        await _adapter(
            _FakeFunctions(),
            _zip_bytes("match.dem", _DEMO),
            max_demo_bytes=16,
        ).fetch(DemoRequest("match-2", steam_id=_STEAM_ID), staging)


@pytest.mark.asyncio
async def test_upstream_error_cannot_expose_token_url_or_headers(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    secret_error = RuntimeError(f"{_TOKEN} https://signed.invalid X-PWA-Signature")
    adapter = _adapter(_FakeFunctions(error=secret_error), _DEMO)

    with pytest.raises(DemoSourceError) as raised:
        await adapter.fetch(DemoRequest("match-1", steam_id=_STEAM_ID), staging)
    assert str(raised.value) == "PWA Demo signing failed"
    assert _TOKEN not in str(raised.value)
    assert "signed.invalid" not in str(raised.value)


@pytest.mark.asyncio
async def test_sync_signing_and_http_work_do_not_block_event_loop(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    adapter = _adapter(_FakeFunctions(delay=0.15), _DEMO)

    task = asyncio.create_task(adapter.fetch(DemoRequest("match-1", steam_id=_STEAM_ID), staging))
    await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.05)
    assert not task.done()
    assert await task is not None
