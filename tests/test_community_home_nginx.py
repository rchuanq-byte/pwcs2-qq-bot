import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/configure-community-home-nginx.py"
SPEC = importlib.util.spec_from_file_location("community_home_nginx", SCRIPT)
nginx = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nginx)
ORIGINAL = (
    "http {\n  server {\n    root /srv;\n"
    "    location / {\n      try_files $uri =404;\n    }\n  }\n}\n"
)


def test_patch_scoped_idempotent_and_preserves_original_routes():
    result = nginx.patch_config(ORIGINAL)
    assert nginx.patch_config(result) == result
    assert 'add_header Cache-Control "no-store" always;' in result
    assert "location ^~ /niuniu/" in result
    assert "    location / {\n      try_files $uri =404;" in result
    assert "frame-ancestors 'none'" in result


@pytest.mark.parametrize("text", ["", ORIGINAL + ORIGINAL, ORIGINAL + "# /niuniu/custom"])
def test_refuses_unknown_or_conflicting_config(text):
    with pytest.raises(ValueError):
        nginx.patch_config(text)


def test_crlf_input_is_supported_and_output_idempotent():
    result = nginx.patch_config(ORIGINAL.replace("\n", "\r\n"))
    assert "\r" not in result
    assert nginx.patch_config(result) == result


def test_reversed_markers_are_rejected():
    with pytest.raises(ValueError):
        nginx.patch_config(nginx.END + nginx.BEGIN + ORIGINAL)


def test_validate_failure_restores_original_without_reloading(tmp_path, monkeypatch):
    config = tmp_path / "nginx.conf"
    config.write_text(ORIGINAL)
    before = config.read_bytes()
    inode = config.stat().st_ino
    calls = []
    def reject(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(nginx.subprocess, "run", reject)
    with pytest.raises(subprocess.SubprocessError):
        nginx.configure(config, tmp_path, "assets")
    assert config.read_bytes() == before
    assert config.stat().st_ino == inode
    assert len(calls) == 1 and calls[0][-1] == "-t"
    assert next(tmp_path.glob("nginx-before-community-*.conf")).read_bytes() == before


def test_success_preserves_inode_and_only_reloads_static_server(tmp_path, monkeypatch):
    config = tmp_path / "nginx.conf"
    config.write_text(ORIGINAL)
    inode = config.stat().st_ino
    calls = []
    monkeypatch.setattr(nginx.subprocess, "run", lambda command, **kwargs: calls.append(command))
    assert nginx.configure(config, tmp_path, "assets") is True
    assert config.stat().st_ino == inode
    assert calls == [["docker", "exec", "assets", "nginx", "-t"],
                     ["docker", "exec", "assets", "nginx", "-s", "reload"]]
    calls.clear()
    assert nginx.configure(config, tmp_path, "assets") is False
    assert not calls


def test_reload_timeout_restores_disk_and_running_config(tmp_path, monkeypatch):
    config = tmp_path / "nginx.conf"
    config.write_bytes(ORIGINAL.encode())
    before = config.read_bytes()
    calls = []

    def timeout_once(command, **kwargs):
        calls.append(command)
        if len(calls) == 2:
            raise subprocess.TimeoutExpired(command, 10)

    monkeypatch.setattr(nginx.subprocess, "run", timeout_once)
    with pytest.raises(subprocess.TimeoutExpired):
        nginx.configure(config, tmp_path, "assets")
    assert config.read_bytes() == before
    assert [command[-1] for command in calls] == ["-t", "reload", "-t", "reload"]
