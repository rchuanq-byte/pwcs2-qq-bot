from __future__ import annotations

import json
from pathlib import Path

import pytest

from deploy.configure_highlights import (
    ConfigurationError,
    existing_worker_secrets,
    merge_compose_profiles,
    prepare_data_directories,
    update_env_text,
    validate_account_steam_id,
    validate_access_token,
    validate_public_ip,
    validate_worker_id,
    validate_worker_secret,
)


def test_update_env_text_preserves_profiles_and_writes_one_worker() -> None:
    updated = update_env_text(
        "COMPOSE_PROFILES=steam-proxy\nHIGHLIGHT_ENABLED=0\nOTHER=value\n",
        access_token="a" * 32,
        worker_id="home-windows",
        worker_secret="s" * 48,
        public_ip="192.0.2.10",
        account_steam_id="76561197960266101",
    )

    assert "COMPOSE_PROFILES=steam-proxy,highlights\n" in updated
    assert updated.count("HIGHLIGHT_ENABLED=") == 1
    assert "HIGHLIGHT_ENABLED=1\n" in updated
    assert "PWCS2_PWA_DEMO_ENABLED=1\n" in updated
    assert "PERFECTWORLD_PWA_ACCESS_TOKEN=" + "a" * 32 + "\n" in updated
    assert "PWCS2_PWA_PUBLIC_IP=192.0.2.10\n" in updated
    assert "PERFECTWORLD_MY_STEAM_ID=76561197960266101\n" in updated
    assert "OTHER=value\n" in updated
    assert (
        "PWCS2_HIGHLIGHT_INTENT_DATABASE_PATH=/data/highlights/intents.db\n"
        in updated
    )
    raw = next(
        line.split("=", 1)[1]
        for line in updated.splitlines()
        if line.startswith("PWCS2_HIGHLIGHT_WORKER_SECRETS_JSON=")
    )
    assert json.loads(raw) == {"home-windows": "s" * 48}


def test_update_env_text_removes_obsolete_local_only_intent_switch() -> None:
    updated = update_env_text(
        "HIGHLIGHT_INTENT_ENABLED=0\nOTHER=value\n",
        access_token="a" * 32,
        worker_id="home-windows",
        worker_secret="s" * 48,
    )

    assert "HIGHLIGHT_INTENT_ENABLED" not in updated
    assert "OTHER=value\n" in updated


def test_existing_worker_secrets_keeps_only_valid_credentials() -> None:
    text = "PWCS2_HIGHLIGHT_WORKER_SECRETS_JSON=" + json.dumps(
        {
            "gaming-pc-01": "s" * 48,
            "bad worker": "x" * 48,
            "short-secret": "x",
        }
    )

    assert existing_worker_secrets(text) == {"gaming-pc-01": "s" * 48}


def test_profile_merge_is_idempotent() -> None:
    assert merge_compose_profiles("highlights,steam-proxy highlights") == (
        "highlights,steam-proxy"
    )


@pytest.mark.parametrize("value", ("", "short", "contains whitespace token"))
def test_access_token_validation_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ConfigurationError):
        validate_access_token(value)


def test_identity_and_secret_validation() -> None:
    assert validate_worker_id("home-windows_1") == "home-windows_1"
    assert validate_worker_secret("x" * 32) == "x" * 32
    with pytest.raises(ConfigurationError):
        validate_worker_id("bad worker")
    with pytest.raises(ConfigurationError):
        validate_worker_secret("too-short")


def test_public_ip_validation() -> None:
    assert validate_public_ip("192.0.2.10") == "192.0.2.10"
    for value in ("", "invalid", "127.0.0.1", "192.168.1.1", "::1"):
        with pytest.raises(ConfigurationError):
            validate_public_ip(value)


def test_account_steam_id_validation() -> None:
    assert validate_account_steam_id("76561197960266101") == "76561197960266101"
    for value in ("", "123", "7656119900640646x"):
        with pytest.raises(ConfigurationError):
            validate_account_steam_id(value)


def test_prepare_data_directories_creates_only_dedicated_mounts(tmp_path: Path) -> None:
    prepared = prepare_data_directories(tmp_path)

    assert {path.name for path in prepared} == {"highlights", "demos", "demo-import"}
    assert all(path.is_dir() and path.parent == (tmp_path / "data" / "bot") for path in prepared)
