#!/usr/bin/env python3
"""Add only community-home routes to the existing Caddy HTTPS virtual host."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

BEGIN = "\t# BEGIN NIUNIU COMMUNITY HTTPS\n"
END = "\t# END NIUNIU COMMUNITY HTTPS\n"
ORIGINAL_PROXY = (
    "\treverse_proxy 127.0.0.1:3000 {\n"
    "\t\theader_up X-Forwarded-For {http.request.remote.host}\n"
    "\t}\n"
)
ROUTES = BEGIN + """\t@niuniu_home path /niuniu /niuniu/* /cs2-training.html
\thandle @niuniu_home {
\t\tredir /niuniu /niuniu/ 308
\t\treverse_proxy 127.0.0.1:80
\t}
\thandle {
\t\treverse_proxy 127.0.0.1:3000 {
\t\t\theader_up X-Forwarded-For {http.request.remote.host}
\t\t}
\t}
""" + END


def patch_config(text: str) -> str:
    text = text.replace("\r\n", "\n")
    if BEGIN in text or END in text:
        if text.count(BEGIN) != 1 or text.count(END) != 1:
            raise ValueError("ambiguous managed routes")
        start, end = text.index(BEGIN), text.index(END)
        if end < start:
            raise ValueError("invalid managed routes")
        if text[start:end + len(END)] != ROUTES:
            raise ValueError("managed routes were modified; review required")
        return text
    if (text.count("https://192.0.2.10 {") != 1
            or text.count(ORIGINAL_PROXY) != 1
            or "/niuniu" in text or "\thandle " in text):
        raise ValueError("unrecognized Caddyfile; review required")
    return text.replace(ORIGINAL_PROXY, ROUTES, 1)


def write_existing(path: Path, content: bytes) -> None:
    # Caddy uses a read-only single-file bind mount: retain the existing inode.
    if path.is_symlink() or not path.is_file():
        raise ValueError("expected regular live Caddyfile")
    with path.open("r+b") as stream:
        stream.write(content)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())


def docker(container: str, *args: str) -> str:
    return subprocess.check_output(
        ["docker", "exec", container, *args], text=True, timeout=15,
    )


def reload_config(container: str) -> None:
    docker(container, "caddy", "validate", "--config", "/etc/caddy/Caddyfile",
           "--adapter", "caddyfile")
    # Supported by Caddy >= 2.11 with caddy run + config file, even with admin off.
    subprocess.run(["docker", "kill", "--signal=USR1", container],
                   check=True, timeout=10, stdout=subprocess.DEVNULL)


def verify_https(expected: bytes) -> None:
    for _ in range(8):
        try:
            with urllib.request.urlopen("https://192.0.2.10/niuniu/", timeout=4) as response:
                if expected in response.read(20000):
                    return
        except OSError:
            pass
        time.sleep(0.5)
    raise RuntimeError("HTTPS homepage did not become ready")


def configure(live: Path, restart_path: Path, backup_root: Path, container: str) -> Path:
    version = docker(container, "caddy", "version").split()[0]
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", version)
    if not match or tuple(map(int, match.groups())) < (2, 11, 0):
        raise ValueError("Caddy version does not support this reload strategy")
    command = json.loads(subprocess.check_output(
        ["docker", "inspect", container, "--format", "{{json .Config.Cmd}}"],
        text=True, timeout=10,
    ))
    if command != ["caddy", "run", "--config", "/etc/caddy/Caddyfile",
                   "--adapter", "caddyfile"]:
        raise ValueError("unexpected Caddy launch command")
    metadata = backup_root.stat()
    if (backup_root.is_symlink() or not backup_root.is_dir()
            or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077):
        raise ValueError("backup directory must be private and owned by current user")
    if live.is_symlink() or not live.is_file():
        raise ValueError("live file must be a regular file")
    if live.stat().st_ino != int(docker(container, "stat", "-c", "%i",
                                       "/etc/caddy/Caddyfile").strip()):
        raise ValueError("live path does not match the mounted inode")
    old = live.read_bytes()
    if old.decode() != docker(container, "cat", "/etc/caddy/Caddyfile"):
        raise ValueError("live mounted config mismatch")
    new = patch_config(old.decode()).encode()
    releases = Path.home() / "lianxu/releases"
    resolved = restart_path.resolve()
    if not resolved.is_relative_to(releases.resolve()) or resolved.name != "Caddyfile":
        raise ValueError("restart path must remain within the application's releases")
    if resolved.is_symlink() or (resolved.exists() and not resolved.is_file()):
        raise ValueError("unexpected restart config")
    prior_restart = resolved.read_bytes() if resolved.exists() else None
    if prior_restart is not None and prior_restart not in (old, new):
        raise ValueError("restart config contains unrelated changes")
    backup = backup_root / ("community-https-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ"))
    backup.mkdir(mode=0o700)
    (backup / "live.Caddyfile").write_bytes(old)
    if prior_restart is not None:
        (backup / "restart.Caddyfile").write_bytes(prior_restart)
    reload_attempted = False
    try:
        write_existing(live, new)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        if restart_path.resolve() != resolved:
            raise ValueError("application release changed during update")
        if resolved != live.resolve():
            if resolved.exists():
                write_existing(resolved, new)
            else:
                with resolved.open("xb") as stream:
                    stream.write(new)
        reload_attempted = True
        reload_config(container)
        verify_https(b"<!-- NIUNIU_COMMUNITY_HOME_V1 -->")
    except Exception:
        write_existing(live, old)
        if resolved != live.resolve():
            if prior_restart is None:
                # Only remove the exact file this operation created.
                if resolved.is_file() and resolved.read_bytes() == new:
                    resolved.unlink()
            elif resolved.exists():
                write_existing(resolved, prior_restart)
        if reload_attempted:
            reload_config(container)
        raise
    return backup


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-config", type=Path, required=True)
    parser.add_argument("--restart-config", type=Path,
                        default=Path.home() / "lianxu/current/deploy/ip-https/Caddyfile")
    parser.add_argument("--backup-root", type=Path, default=Path.home() / "qqbot-backups")
    parser.add_argument("--container", default="lianxu-https-caddy-1")
    args = parser.parse_args()
    backup = configure(args.live_config, args.restart_config, args.backup_root, args.container)
    print(f"OK community_home_https; backup={backup}")


if __name__ == "__main__":
    main()
