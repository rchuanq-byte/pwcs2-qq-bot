"""Private rollback snapshot of configuration and consistent SQLite copies."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def fingerprints(connection):
    result = {}
    tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    for (table,) in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        columns = [r[1] for r in connection.execute(f"PRAGMA table_info({quoted})")]
        if not any(
            word in table
            for word in ("binding", "wallet", "inventory", "group_ai_memor", "card_lock")
        ):
            continue
        order = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
        digest, count = hashlib.sha256(), 0
        for row in connection.execute(f"SELECT * FROM {quoted} ORDER BY {order}"):
            digest.update(json.dumps(row, ensure_ascii=False, default=str).encode())
            count += 1
        result[table] = {"rows": count, "sha256": digest.hexdigest()}
    return result


def snapshot(root, target):
    if not (root / "compose.yaml").is_file():
        raise ValueError("Not the QQ bot workspace")
    target.mkdir(parents=True, mode=0o700, exist_ok=False)
    os.chmod(target, 0o700)
    config_files = (
        ".env",
        "config/config.json",
        "compose.override.yaml",
        *(
            str(path.relative_to(root))
            for path in sorted((root / "data/napcat/config").glob("onebot11_*.json"))
        ),
    )
    for relative in config_files:
        source = root / relative
        if source.exists():
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            destination.chmod(0o600)
    manifest = {"created_at": datetime.now(UTC).isoformat(), "databases": {}}
    for source in (root / "data/bot").rglob("*.db"):
        relative = source.relative_to(root / "data/bot")
        if len(relative.parts) > 2:
            continue
        destination = target / "databases" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as live:
            with sqlite3.connect(destination) as backup:
                live.backup(backup)
                if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("SQLite backup integrity failed")
                manifest["databases"][str(relative)] = fingerprints(backup)
        destination.chmod(0o600)
    framework = root / "data/astrbot"
    manifest["framework_databases"] = []
    if framework.is_dir():
        for source in framework.rglob("*"):
            if not source.is_file() or source.suffix not in {".db", ".json"}:
                continue
            if any(
                part in {"temp", "logs", "__pycache__"}
                for part in source.relative_to(framework).parts
            ):
                continue
            destination = target / "astrbot" / source.relative_to(framework)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.suffix == ".db":
                with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as live:
                    with sqlite3.connect(destination) as backup:
                        live.backup(backup)
                        if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                            raise ValueError("Framework SQLite backup integrity failed")
                manifest["framework_databases"].append(str(source.relative_to(framework)))
            else:
                shutil.copyfile(source, destination)
            destination.chmod(0o600)
    file = target / "manifest.json"
    file.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    file.chmod(0o600)
    print(f"OK private backup; verified SQLite databases={len(manifest['databases'])}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    os.umask(0o077)
    snapshot(args.root.resolve(), args.destination.resolve())
