#!/usr/bin/env python3
"""Export an allowlisted public snapshot; never import or initialize the bot.

Run as a short-lived timer job with read access to private inputs. Only the
output belongs in a public web root. ``--catalog`` accepts community.py and
counts its literal BADGES tuple without importing or executing that source.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import os
import re
import sqlite3
import stat
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SQLITE_BUSY_SECONDS = 0.05
SQLITE_BUDGET_SECONDS = 0.25
SQLITE_PROGRESS_STEPS = 1000
SQLITE_MAX_PROGRESS_CALLS = 2500
HEALTH_MAX_BYTES = 8192
CATALOG_MAX_BYTES = 2 * 1024 * 1024
OFFLINE_QQ_STATES = frozenset({
    "starting", "stopped", "disconnected", "reconnecting", "login_required",
    "degraded", "unresponsive",
})
OFFLINE_CONTAINER_STATES = frozenset({
    "created", "paused", "restarting", "removing", "exited", "dead",
})


def iso_utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")


def _bounded_read(path: Path, limit: int) -> bytes:
    # Avoid blocking on a FIFO/device accidentally supplied as an input.
    if not path.is_file():
        raise ValueError("input must be a regular file")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("input too large")
    return data


def read_bot_status(path: Path, *, kind: str, now: float) -> dict[str, str | None]:
    result: dict[str, str | None] = {"status": "unknown", "checked_at": None}
    if kind not in {"transport", "guard"}:
        return result
    try:
        data = json.loads(_bounded_read(path, HEALTH_MAX_BYTES))
    except (OSError, ValueError, RecursionError):
        return result
    if not isinstance(data, dict) or type(data.get("version")) is not int:
        return result
    if data["version"] != 1:
        return result
    checked = data.get("checked_at")
    if (
        isinstance(checked, bool)
        or not isinstance(checked, int | float)
        or not 0 < checked <= now
    ):
        return result
    try:
        if not math.isfinite(checked):
            return result
        result["checked_at"] = iso_utc(checked)
    except (ValueError, OverflowError, OSError):
        return result
    if now - checked > (150 if kind == "guard" else 95):
        return result

    if kind == "guard":
        if data.get("mode") != "observe_only":
            return result
        container = data.get("container_status")
        if isinstance(container, str) and container in OFFLINE_CONTAINER_STATES:
            result["status"] = "offline"
            return result
        alarms = data.get("alarms", [])
        if (
            container != "running"
            or not isinstance(alarms, list)
            or "runtime_probe_unavailable" in alarms
        ):
            return result
        state = data.get("qq_state")
        if state == "online":
            result["status"] = "online"
        elif isinstance(state, str) and state in OFFLINE_QQ_STATES:
            result["status"] = "offline"
    else:
        if type(data.get("connected")) is not bool or type(data.get("online")) is not bool:
            return result
        state = data.get("state")
        if state == "online":
            result["status"] = (
                "online" if data["connected"] is True and data["online"] is True else "offline"
            )
        elif isinstance(state, str) and state in OFFLINE_QQ_STATES:
            result["status"] = "offline"
    return result


def count_titles(path: Path | None) -> int | None:
    if path is None:
        return None
    try:
        tree = ast.parse(_bounded_read(path, CATALOG_MAX_BYTES))
        values = []
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            else:
                continue
            if any(isinstance(target, ast.Name) and target.id == "BADGES" for target in targets):
                values.append(ast.literal_eval(node.value))
        if len(values) != 1 or not isinstance(values[0], tuple | list):
            return None
        badges = values[0]
        if any(not isinstance(badge, str) or not badge.strip() for badge in badges):
            return None
        return len(set(badges))
    except (OSError, ValueError, SyntaxError, TypeError, RecursionError):
        return None


def database_counts(path: Path, group_id: str) -> dict[str, int | None]:
    result: dict[str, int | None] = {"bound_players": None, "reported_matches": None}
    connection: sqlite3.Connection | None = None
    try:
        if not path.is_file():
            return result
        database = path.resolve()
        with database.open("rb") as stream:
            header = stream.read(100)
        if header[:16] != b"SQLite format 3\0":
            return result
        # mode=ro alone can CREATE missing WAL sidecars in a writable directory.
        # Never use immutable=1: it silently omits committed live WAL records.
        # A service should additionally mount the input directory read-only to
        # close the race with a writer removing sidecars after this preflight.
        if header[18:20] == b"\x02\x02" and any(
            not Path(str(database) + suffix).is_file() for suffix in ("-wal", "-shm")
        ):
            return result
        # Never use the application's repositories: their constructors migrate.
        connection = sqlite3.connect(
            database.as_uri() + "?mode=ro", uri=True,
            timeout=SQLITE_BUSY_SECONDS, isolation_level=None,
        )
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("PRAGMA temp_store = MEMORY")
        deadline = time.monotonic() + SQLITE_BUDGET_SECONDS
        progress_calls = 0

        def stop_over_budget() -> bool:
            nonlocal progress_calls
            progress_calls += 1
            return (
                progress_calls > SQLITE_MAX_PROGRESS_CALLS
                or time.monotonic() >= deadline
            )

        connection.set_progress_handler(stop_over_budget, SQLITE_PROGRESS_STEPS)
        connection.execute("BEGIN")  # Consistent read-only view for both totals.
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' AND name IN "
                "('bindings', 'group_match_source_notifications', 'group_match_notifications')"
            )
        }
        if "bindings" in tables:
            try:
                result["bound_players"] = connection.execute(
                    "SELECT COUNT(*) FROM bindings WHERE group_id = ?", (group_id,)
                ).fetchone()[0]
            except sqlite3.Error:
                pass
        selects = []
        parameters = []
        if "group_match_source_notifications" in tables:
            selects.append(
                "SELECT DISTINCT data_source, match_id FROM group_match_source_notifications "
                "WHERE group_id = ?"
            )
            parameters.append(group_id)
        if "group_match_notifications" in tables:
            # data_source=3 is the legacy Perfect World PvP source.
            selects.append(
                "SELECT DISTINCT 3 AS data_source, match_id FROM group_match_notifications "
                "WHERE group_id = ?"
            )
            parameters.append(group_id)
        if selects:
            try:
                result["reported_matches"] = connection.execute(
                    "SELECT COUNT(*) FROM (" + " UNION ".join(selects) + ")", parameters,
                ).fetchone()[0]
            except sqlite3.Error:
                pass
    except (OSError, ValueError, sqlite3.Error):
        pass
    finally:
        if connection is not None:
            connection.close()
    return result


def build_snapshot(
    *, database: Path, health: Path, group_id: str, catalog: Path | None = None,
    health_kind: str = "transport", now: float | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9]{1,20}", group_id):
        raise ValueError("group id must contain ASCII digits")
    checked = time.time() if now is None else now
    return {
        "version": 1,
        "updated_at": iso_utc(checked),
        "bot": read_bot_status(health, kind=health_kind, now=checked),
        "stats": {
            **database_counts(database, group_id),
            "available_titles": count_titles(catalog),
        },
    }


def _validate_output(path: Path, protected: tuple[Path, ...]) -> None:
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError("output symlinks are not allowed")
    if path.exists() and not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("output must be a regular file")
    resolved = path.resolve()
    for source in protected:
        if resolved == source.resolve() or (
            path.exists() and source.exists() and path.samefile(source)
        ):
            raise ValueError("output must not replace a private input")


def write_snapshot(
    path: Path, value: dict[str, Any], *, database: Path,
    health: Path | None = None, catalog: Path | None = None,
) -> None:
    protected = (
        database,
        *(Path(str(database.resolve()) + suffix) for suffix in ("-wal", "-shm", "-journal")),
        *(source for source in (health, catalog) if source is not None),
    )
    _validate_output(path, protected)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".community-home-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        _validate_output(path, protected)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--health", required=True, type=Path)
    parser.add_argument("--health-kind", choices=("transport", "guard"), default="transport")
    parser.add_argument("--group-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--catalog", type=Path)
    args = parser.parse_args(argv)
    try:
        snapshot = build_snapshot(
            database=args.database, health=args.health, group_id=args.group_id,
            catalog=args.catalog, health_kind=args.health_kind,
        )
        write_snapshot(
            args.output, snapshot, database=args.database, health=args.health, catalog=args.catalog,
        )
    except (OSError, ValueError, OverflowError, RecursionError):
        print("ERROR community_snapshot_failed")
        return 1
    print("OK community_snapshot_written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
