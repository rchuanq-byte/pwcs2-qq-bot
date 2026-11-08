import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/configure-community-home-https.py"
SPEC = importlib.util.spec_from_file_location("community_home_https", SCRIPT)
https = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(https)

ORIGINAL = (
    "{\n\tadmin off\n\tauto_https disable_redirects\n}\n\n"
    "https://192.0.2.10 {\n\ttls {\n\t\tissuer acme https://acme.example/directory\n\t}\n"
    + https.ORIGINAL_PROXY + "}\n"
)


def test_routes_are_narrow_and_existing_proxy_is_preserved():
    result = https.patch_config(ORIGINAL)
    assert result.count("127.0.0.1:3000") == 1
    assert result.count("127.0.0.1:80") == 1
    assert "path /niuniu /niuniu/* /cs2-training.html\n" in result
    assert "redir /niuniu /niuniu/ 308" in result
    assert "path /niuniu*" not in result
    assert "header_up X-Forwarded-For {http.request.remote.host}" in result
    assert "admin off" in result
    assert "issuer acme https://acme.example/directory" in result
    assert https.patch_config(result) == result


def test_crlf_supported():
    assert https.patch_config(ORIGINAL.replace("\n", "\r\n")) == https.patch_config(ORIGINAL)


@pytest.mark.parametrize("text", [
    "", ORIGINAL + ORIGINAL, ORIGINAL.replace("127.0.0.1:3000", "127.0.0.1:3001"),
    ORIGINAL + "# /niuniu custom", https.END + https.BEGIN + ORIGINAL,
])
def test_unknown_or_conflicting_configuration_is_rejected(text):
    with pytest.raises(ValueError):
        https.patch_config(text)


def test_user_changes_to_managed_block_are_not_overwritten():
    modified = https.patch_config(ORIGINAL).replace("127.0.0.1:80", "127.0.0.1:8080")
    with pytest.raises(ValueError):
        https.patch_config(modified)


def test_in_place_write_preserves_inode(tmp_path):
    config = tmp_path / "Caddyfile"
    config.write_bytes(ORIGINAL.encode())
    inode = config.stat().st_ino
    https.write_existing(config, https.patch_config(ORIGINAL).encode())
    assert config.stat().st_ino == inode
