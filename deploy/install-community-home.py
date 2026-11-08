#!/usr/bin/env python3
"""Install only the public community-home assets; never restart any service."""

from __future__ import annotations

import argparse
import hashlib
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ASSET_NAMES = frozenset({
    "index.html", "style.css", "app.js", "favicon.svg",
    "scripture.html", "scripture.css", "scripture.js", "scripture.md",
})
REQUIRED_NAMES = frozenset({"index.html", "style.css", "app.js"})
MAX_TOTAL_BYTES = 2 * 1024 * 1024
OWNERSHIP_MARKER = b"<!-- NIUNIU_COMMUNITY_HOME_V1 -->"
REPO = Path(__file__).resolve().parent.parent


def regular_directory(path: Path) -> Path:
    path = path.absolute()
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("symbolic-link directories are not permitted")
    if not path.is_dir():
        raise ValueError("expected an existing directory")
    return path.resolve()


def asset_contents(source: Path) -> dict[str, bytes]:
    source = regular_directory(source)
    names = {path.name for path in source.iterdir()}
    if not REQUIRED_NAMES <= names or not names <= ASSET_NAMES:
        raise ValueError("unexpected or missing public asset")
    contents = {}
    for name in sorted(names):
        path = source / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("assets must be regular files")
        if not 0 < path.stat().st_size <= MAX_TOTAL_BYTES:
            raise ValueError("invalid asset size")
        raw = path.read_bytes()
        raw.decode("utf-8")
        contents[name] = raw
    if sum(map(len, contents.values())) > MAX_TOTAL_BYTES:
        raise ValueError("public assets exceed size budget")
    html = contents["index.html"].lower()
    if b"<!doctype html>" not in html or b"</html>" not in html:
        raise ValueError("invalid homepage HTML")
    if OWNERSHIP_MARKER not in contents["index.html"]:
        raise ValueError("missing community-home ownership marker")
    return contents


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("refusing to replace a non-regular destination")
    descriptor, temporary = tempfile.mkstemp(prefix=".niuniu-", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def install(source: Path, web_root: Path, backup_root: Path) -> dict[str, object]:
    contents = asset_contents(source)
    web_root = regular_directory(web_root)
    backup_root = regular_directory(backup_root)
    if backup_root.is_relative_to(web_root) or web_root.is_relative_to(backup_root):
        raise ValueError("backups and public files must be separate")
    destination = web_root / "niuniu"
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise ValueError("unsafe community-home destination")
    if destination.exists():
        existing = {path.name for path in destination.iterdir()}
        if not existing <= ASSET_NAMES | {"snapshot.json"}:
            raise ValueError("destination contains files not owned by this installer")
        for path in destination.iterdir():
            if path.is_symlink() or not path.is_file():
                raise ValueError("destination contains a non-regular file")
        if existing & ASSET_NAMES:
            index = destination / "index.html"
            if not index.is_file() or index.stat().st_size > MAX_TOTAL_BYTES:
                raise ValueError("destination belongs to an unrecognized website")
            if OWNERSHIP_MARKER not in index.read_bytes():
                raise ValueError("destination belongs to an unrecognized website")
    if destination.resolve() == source.resolve():
        raise ValueError("source and destination must be different")

    # Backups are private and outside Nginx's root. The database and health
    # file are never copied by this installer, nor is the live snapshot changed.
    old = {}
    for name in contents:
        path = destination / name
        if path.exists():
            if path.stat().st_size > MAX_TOTAL_BYTES:
                raise ValueError("existing asset exceeds backup budget")
            old[name] = path.read_bytes()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = backup_root / ("community-home-" + stamp)
    backup.mkdir(mode=0o700)
    os.chmod(backup, 0o700)
    for name, raw in old.items():
        atomic_write(backup / name, raw, 0o600)
    destination.mkdir(mode=0o755, exist_ok=True)
    changed = []
    try:
        # Publish the entry point last. All assets retain stable relative URLs.
        for name in sorted(contents, key=lambda item: item == "index.html"):
            target = destination / name
            if target.exists() and target.read_bytes() == contents[name]:
                continue
            atomic_write(target, contents[name])
            changed.append(name)
        for name, raw in contents.items():
            installed_hash = hashlib.sha256((destination / name).read_bytes()).digest()
            if installed_hash != hashlib.sha256(raw).digest():
                raise ValueError("installed asset verification failed")
    except Exception:
        for name in changed:
            if name in old:
                atomic_write(destination / name, old[name])
            else:
                (destination / name).unlink(missing_ok=True)
        raise
    return {"files": len(contents), "bytes": sum(map(len, contents.values())), "backup": backup}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path,
                        default=REPO / "src/pwcs2_bot/assets/community_home")
    parser.add_argument("--web-root", type=Path, default=Path.home() / "teamspeak3/assets")
    parser.add_argument("--backup-root", type=Path, default=Path.home() / "qqbot-backups")
    args = parser.parse_args()
    try:
        result = install(args.source, args.web_root, args.backup_root)
    except (OSError, ValueError):
        print("FAIL community_home_install; existing services were not restarted")
        return 1
    print(f"OK community_home_installed files={result['files']} bytes={result['bytes']}")
    print(f"Private backup: {result['backup']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
