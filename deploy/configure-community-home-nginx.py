#!/usr/bin/env python3
"""Configure only /niuniu/ on the existing static server, with rollback."""

from __future__ import annotations

import argparse
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

BEGIN = "    # BEGIN NIUNIU COMMUNITY HOME\n"
END = "    # END NIUNIU COMMUNITY HOME\n"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
)
BLOCK = (BEGIN + """    location = /niuniu {
      return 302 /niuniu/;
    }
    location ^~ /niuniu/ {
      index index.html;
      try_files $uri $uri/ =404;
      add_header Cache-Control "no-store" always;
      add_header X-Content-Type-Options nosniff always;
      add_header Referrer-Policy no-referrer always;
      add_header X-Frame-Options DENY always;
      add_header Content-Security-Policy "__CONTENT_POLICY__" always;
    }
""" + END).replace("__CONTENT_POLICY__", CSP)


def patch_config(text: str) -> str:
    text = text.replace("\r\n", "\n")
    if BEGIN in text or END in text:
        if text.count(BEGIN) != 1 or text.count(END) != 1:
            raise ValueError("ambiguous managed configuration")
        start = text.index(BEGIN)
        end_start = text.index(END)
        if end_start < start:
            raise ValueError("invalid managed configuration")
        finish = end_start + len(END)
        return text[:start] + BLOCK + text[finish:]
    anchor = "    location / {\n"
    if text.count(anchor) != 1 or "/niuniu" in text:
        raise ValueError("unrecognized existing configuration; manual review required")
    return text.replace(anchor, BLOCK + "\n" + anchor, 1)


def overwrite_in_place(path: Path, content: bytes) -> None:
    # Preserve the inode: Nginx has a single-file read-only bind mount.
    if path.is_symlink() or not path.is_file():
        raise ValueError("expected a regular config file")
    with path.open("r+b") as stream:
        stream.seek(0)
        stream.write(content)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())


def configure(config: Path, backup_root: Path, container: str) -> bool:
    if config.is_symlink() or not config.is_file() or config.stat().st_size > 128 * 1024:
        raise ValueError("unexpected nginx config")
    if backup_root.is_symlink() or not backup_root.is_dir():
        raise ValueError("expected private backup directory")
    if os.name == "posix":
        metadata = backup_root.stat()
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
            raise ValueError("backup directory must be owned by current user and private")
    old = config.read_bytes()
    new = patch_config(old.decode("utf-8")).encode("utf-8")
    if old == new:
        return False
    backup = backup_root / ("nginx-before-community-" + datetime.now(UTC).strftime(
        "%Y%m%dT%H%M%S%fZ") + ".conf")
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(old)
    reload_attempted = False
    try:
        overwrite_in_place(config, new)
        subprocess.run(["docker", "exec", container, "nginx", "-t"], check=True, timeout=10)
        reload_attempted = True
        subprocess.run(["docker", "exec", container, "nginx", "-s", "reload"],
                       check=True, timeout=10)
    except Exception:
        overwrite_in_place(config, old)
        if reload_attempted:
            # A timed-out exec may already have delivered the reload signal.
            subprocess.run(["docker", "exec", container, "nginx", "-t"],
                           check=True, timeout=10)
            subprocess.run(["docker", "exec", container, "nginx", "-s", "reload"],
                           check=True, timeout=10)
        raise
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path.home() / "teamspeak3/nginx.conf")
    parser.add_argument("--backup-root", type=Path, default=Path.home() / "qqbot-backups")
    parser.add_argument("--container", default="teamspeak3-assets-1")
    args = parser.parse_args()
    try:
        changed = configure(args.config, args.backup_root, args.container)
    except (OSError, ValueError, subprocess.SubprocessError):
        print("FAIL community_home_nginx; check diagnostics and private config backup")
        return 1
    print("OK community_home_nginx_" + ("reloaded" if changed else "unchanged"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
