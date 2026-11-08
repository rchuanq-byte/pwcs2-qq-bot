from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from pwcs2_bot.channel import PWCS2Config
from pwcs2_bot.demo_source import (
    DemoRequest,
    DemoSourceError,
    DemoSourceManager,
    PwaDemoProvider,
)

_DEMO_BYTES = b"PBDEMS2\x00" + (b"demo-payload" * 16)


def _write_demo(path: Path, data: bytes = _DEMO_BYTES) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.mark.asyncio
async def test_manual_import_validates_and_deduplicates_demo(tmp_path: Path) -> None:
    import_root = tmp_path / "incoming"
    storage = tmp_path / "managed"
    source = _write_demo(import_root / "nested" / "match.dem")
    request = DemoRequest(
        match_id="perfect-match-123456",
        steam_id="76561197960266101",
    )
    manager = DemoSourceManager(storage, import_root)

    first = await manager.import_local(source, request)
    second = await manager.import_local("nested/match.dem", request)

    assert first == second
    assert first.provider == "manual"
    assert first.platform == "perfectworld"
    assert first.size_bytes == len(_DEMO_BYTES)
    assert first.sha256 == hashlib.sha256(_DEMO_BYTES).hexdigest()
    assert first.path.read_bytes() == _DEMO_BYTES
    assert first.path.parent == storage / "perfectworld"
    assert request.match_id not in first.path.name


@pytest.mark.asyncio
async def test_manual_import_rejects_files_outside_configured_root(tmp_path: Path) -> None:
    import_root = tmp_path / "incoming"
    import_root.mkdir()
    source = _write_demo(tmp_path / "outside.dem")
    manager = DemoSourceManager(tmp_path / "managed", import_root)

    with pytest.raises(DemoSourceError, match="outside the allowed directory"):
        await manager.import_local(source, DemoRequest("match-1"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "content", "error"),
    (
        ("match.txt", _DEMO_BYTES, r"\.dem extension"),
        ("match.dem", b"not-a-counter-strike-demo", "invalid file header"),
    ),
)
async def test_manual_import_rejects_invalid_files(
    tmp_path: Path,
    name: str,
    content: bytes,
    error: str,
) -> None:
    import_root = tmp_path / "incoming"
    source = _write_demo(import_root / name, content)
    manager = DemoSourceManager(tmp_path / "managed", import_root)

    with pytest.raises(DemoSourceError, match=error):
        await manager.import_local(source, DemoRequest("match-1"))


@pytest.mark.asyncio
async def test_manual_import_enforces_streaming_size_limit(tmp_path: Path) -> None:
    import_root = tmp_path / "incoming"
    source = _write_demo(import_root / "large.dem", b"PBDEMS2\x00" + (b"x" * 64))
    manager = DemoSourceManager(tmp_path / "managed", import_root, max_file_bytes=16)

    with pytest.raises(DemoSourceError, match="size limit"):
        await manager.import_local(source, DemoRequest("match-1"))
    assert not tuple((tmp_path / "managed").rglob("*.part"))


@pytest.mark.asyncio
async def test_pwa_adapter_is_injected_and_keeps_credentials_out_of_artifact(
    tmp_path: Path,
) -> None:
    secret = "access-token-secret"

    async def fetcher(request: DemoRequest, staging_directory: Path) -> Path:
        assert request.steam_id == "76561197960266101"
        return _write_demo(staging_directory / "download.dem")

    provider = PwaDemoProvider(fetcher)
    manager = DemoSourceManager(
        tmp_path / "managed",
        tmp_path / "incoming",
        providers=(provider,),
    )

    artifact = await manager.acquire(
        DemoRequest("match-1", steam_id="76561197960266101"),
        provider_name="pwa",
    )

    assert artifact is not None
    assert artifact.provider == "pwa"
    assert secret not in repr(provider)
    assert secret not in repr(artifact)
    assert not any((tmp_path / "managed" / ".staging").iterdir())


@pytest.mark.asyncio
async def test_provider_exception_does_not_expose_url_or_token(tmp_path: Path) -> None:
    secret = "signed.example.test/demo?token=super-secret"

    async def failing_fetcher(request: DemoRequest, staging_directory: Path) -> Path:
        del request, staging_directory
        raise RuntimeError(secret)

    manager = DemoSourceManager(
        tmp_path / "managed",
        tmp_path / "incoming",
        providers=(PwaDemoProvider(failing_fetcher),),
    )

    with pytest.raises(DemoSourceError) as raised:
        await manager.acquire(DemoRequest("match-1", steam_id="76561197960266101"))
    assert str(raised.value) == "pwa Demo provider failed"
    assert secret not in str(raised.value)


@pytest.mark.asyncio
async def test_provider_cannot_return_a_path_outside_its_staging_directory(
    tmp_path: Path,
) -> None:
    outside = _write_demo(tmp_path / "outside.dem")

    async def escaping_fetcher(request: DemoRequest, staging_directory: Path) -> Path:
        del request, staging_directory
        return outside

    manager = DemoSourceManager(
        tmp_path / "managed",
        tmp_path / "incoming",
        providers=(PwaDemoProvider(escaping_fetcher),),
    )

    with pytest.raises(DemoSourceError, match="outside the allowed directory"):
        await manager.acquire(DemoRequest("match-1", steam_id="76561197960266101"))


@pytest.mark.asyncio
async def test_pwa_provider_requires_steam_id(tmp_path: Path) -> None:
    async def unused_fetcher(request: DemoRequest, staging_directory: Path) -> None:
        del request, staging_directory

    manager = DemoSourceManager(
        tmp_path / "managed",
        tmp_path / "incoming",
        providers=(PwaDemoProvider(unused_fetcher),),
    )

    with pytest.raises(DemoSourceError, match="pwa Demo provider failed"):
        await manager.acquire(DemoRequest("match-1"))


def test_request_and_manager_configuration_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="SteamID64"):
        DemoRequest("match-1", steam_id="123")
    with pytest.raises(ValueError, match="control"):
        DemoRequest("match\nsecret")
    with pytest.raises(ValueError, match="separate"):
        DemoSourceManager(tmp_path / "same", tmp_path / "same")


@pytest.mark.asyncio
async def test_unknown_provider_is_rejected_without_creating_staging(tmp_path: Path) -> None:
    manager = DemoSourceManager(tmp_path / "managed", tmp_path / "incoming")
    with pytest.raises(DemoSourceError, match="not configured"):
        await manager.acquire(DemoRequest("match-1"), provider_name="pwa")
    assert not (tmp_path / "managed").exists()


def test_channel_demo_source_configuration_defaults_and_aliases() -> None:
    defaults = PWCS2Config()
    assert defaults.demo_sources_enabled is False
    assert defaults.demo_storage_path == "/data/demos"
    assert defaults.demo_import_root == "/data/demo-import"
    assert defaults.demo_max_file_bytes == 512 * 1024 * 1024

    configured = PWCS2Config.model_validate(
        {
            "demoSourcesEnabled": True,
            "demoStoragePath": "/safe/demos",
            "demoImportRoot": "/safe/incoming",
            "demoMaxFileBytes": 1024 * 1024,
        }
    )
    assert configured.demo_sources_enabled is True
    assert configured.demo_storage_path == "/safe/demos"
    assert configured.demo_import_root == "/safe/incoming"
    assert configured.demo_max_file_bytes == 1024 * 1024

    with pytest.raises(ValidationError):
        PWCS2Config.model_validate({"demoMaxFileBytes": 1024})


def test_storage_usage_counts_managed_and_partial_files(tmp_path: Path) -> None:
    storage = tmp_path / "managed"
    manager = DemoSourceManager(storage, tmp_path / "incoming")
    (storage / "perfectworld").mkdir(parents=True)
    (storage / "perfectworld" / "managed.dem").write_bytes(b"demo")
    staging = storage / ".staging" / ("a" * 32)
    staging.mkdir(parents=True)
    (staging / ".partial.download").write_bytes(b"partial")

    usage = manager.storage_usage()

    assert usage.file_count == 2
    assert usage.demo_count == 1
    assert usage.total_bytes == len(b"demo") + len(b"partial")


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and modes only")
async def test_pwa_acquisition_keeps_only_storage_mount_root_group_shared(
    tmp_path: Path,
) -> None:
    async def fetcher(_request: DemoRequest, staging_directory: Path) -> Path:
        return _write_demo(staging_directory / "download.dem")

    gid = os.getgid()
    storage = tmp_path / "managed"
    manager = DemoSourceManager(
        storage,
        tmp_path / "incoming",
        providers=(PwaDemoProvider(fetcher),),
        shared_root_gid=gid,
    )

    await manager.acquire(
        DemoRequest("match-1", steam_id="76561197960266101"),
        provider_name="pwa",
    )

    assert storage.stat().st_gid == gid
    assert stat.S_IMODE(storage.stat().st_mode) == 0o770
    assert stat.S_IMODE((storage / ".staging").stat().st_mode) == 0o700


def test_stale_provider_staging_is_removed_after_grace_period(tmp_path: Path) -> None:
    storage = tmp_path / "managed"
    manager = DemoSourceManager(storage, tmp_path / "incoming")
    stale = storage / ".staging" / ("b" * 32)
    stale.mkdir(parents=True)
    partial = stale / ".partial.download"
    partial.write_bytes(b"partial")
    os.utime(partial, (100, 100))
    os.utime(stale, (100, 100))

    removed = manager.cleanup_stale_staging(
        older_than_seconds=6 * 60 * 60,
        now=100 + 6 * 60 * 60 + 1,
    )

    assert removed == (stale.resolve(),)
    assert not stale.exists()
