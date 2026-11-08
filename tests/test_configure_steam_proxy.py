from __future__ import annotations

import importlib.util
import stat
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "deploy" / "configure_steam_proxy.py"
SPEC = importlib.util.spec_from_file_location("configure_steam_proxy", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "http://provider.example/sub?token=secret",
        "https:///missing-host",
        "https://user:password@provider.example/sub",
        "https://provider.example/sub#secret",
        "https://provider.example/sub\nINJECTED",
        "https://provider.example:99999/sub",
    ],
)
def test_validate_subscription_url_rejects_unsafe_values_without_echoing(
    value: str,
) -> None:
    with pytest.raises(MODULE.ConfigurationError) as raised:
        MODULE.validate_subscription_url(value)

    if value:
        assert value not in str(raised.value)
    assert "secret" not in str(raised.value)


def test_validate_subscription_url_accepts_secret_query() -> None:
    value = "https://provider.example/api/v1/client/subscribe?token=very-secret"
    assert MODULE.validate_subscription_url(value) == value


def test_render_config_routes_only_steam_and_quotes_secrets() -> None:
    rendered = MODULE.render_config(
        'https://provider.example/sub?token=a"b',
        "(?i)港|hk",
        "steam_user",
        "safe-password",
    )

    assert 'url: "https://provider.example/sub?token=a\\"b"' in rendered
    assert 'filter: "(?i)\\u6e2f|hk"' in rendered
    assert '  - "steam_user:safe-password"' in rendered
    assert "DOMAIN,api.steampowered.com,STEAM-HK" in rendered
    assert "MATCH,REJECT" in rendered
    assert "tun:" not in rendered


def test_render_file_config_uses_private_provider_and_locked_rules() -> None:
    rendered = MODULE.render_file_config(
        "(?i)港|hk",
        "steam_user",
        "safe-password",
    )

    assert "type: file" in rendered
    assert "path: ./providers/steam-user.yaml" in rendered
    assert "DOMAIN,api.steampowered.com,STEAM-HK" in rendered
    assert "MATCH,REJECT" in rendered
    assert "expected-status: 404" in rendered
    assert "exclude-type: direct|dns|wireguard|tailscale|ssh|openvpn" in rendered
    assert "url:" not in rendered.split("health-check:", 1)[0]
    assert "tun:" not in rendered


def test_read_proxy_config_file_accepts_full_yaml(tmp_path: Path) -> None:
    source = tmp_path / "subscription.yaml"
    source.write_text(
        "mixed-port: 7890\n"
        "tun:\n"
        "  enable: true\n"
        "proxies:\n"
        "  - name: 香港 01\n"
        "    type: socks5\n"
        "    server: example.test\n"
        "    port: 1080\n",
        encoding="utf-8",
    )

    imported = MODULE.read_proxy_config_file(source)

    assert "proxies:" in imported
    assert "tun:" in imported
    assert "\r" not in imported


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("proxy-providers:\n  remote:\n    type: http\n", "没有内联 proxies"),
        ("\ufeffrules:\n  - MATCH,DIRECT\n", "没有内联 proxies"),
    ],
)
def test_read_proxy_config_file_rejects_yaml_without_inline_nodes(
    tmp_path: Path,
    content: str,
    message: str,
) -> None:
    source = tmp_path / "subscription.yaml"
    source.write_text(content, encoding="utf-8")

    with pytest.raises(MODULE.ConfigurationError, match=message):
        MODULE.read_proxy_config_file(source)


def test_read_proxy_config_file_rejects_binary(tmp_path: Path) -> None:
    source = tmp_path / "subscription.yaml"
    source.write_bytes(b"proxies:\x00secret")

    with pytest.raises(MODULE.ConfigurationError, match="文本 YAML"):
        MODULE.read_proxy_config_file(source)


def test_update_env_text_preserves_secrets_and_enables_profile_once() -> None:
    result = MODULE.update_env_text(
        "PERFECTWORLD_TOKEN=keep\n"
        "COMPOSE_PROFILES=other,steam-proxy\n"
        "STEAM_PRESENCE_PROXY_URL=old\n"
        "STEAM_PRESENCE_PROXY_URL=duplicate\n"
        "STEAM_MIHOMO_IMAGE=custom\n",
        proxy_url="http://user:password@steam-mihomo:7890",
    )

    assert "PERFECTWORLD_TOKEN=keep" in result
    assert "COMPOSE_PROFILES=other,steam-proxy" in result
    assert result.count("STEAM_PRESENCE_PROXY_URL=") == 1
    assert "STEAM_PRESENCE_PROXY_URL=http://user:password@steam-mihomo:7890" in result
    assert f"STEAM_MIHOMO_IMAGE={MODULE.PROXY_IMAGE}" in result


def test_rotate_authentication_config_changes_only_mixed_port_credential() -> None:
    original = MODULE.render_file_config(
        "(?i)港|hk",
        "old_user",
        "old-password",
    )

    rotated = MODULE.rotate_authentication_config(
        original,
        "new_user:new-password",
    )

    assert '  - "new_user:new-password"' in rotated
    assert "old_user:old-password" not in rotated
    assert rotated.replace("new_user:new-password", "old_user:old-password") == original


def test_rotate_authentication_config_fails_closed_on_unknown_shape() -> None:
    with pytest.raises(MODULE.ConfigurationError, match="authentication"):
        MODULE.rotate_authentication_config("mixed-port: 7890\n", "user:password")


def test_write_text_atomic_uses_private_permissions(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    MODULE.write_text_atomic(target, "secret\n")

    assert target.read_text(encoding="utf-8") == "secret\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
