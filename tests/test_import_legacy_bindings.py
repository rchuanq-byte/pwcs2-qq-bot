from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "deploy" / "import_legacy_bindings.py"
SPEC = importlib.util.spec_from_file_location("import_legacy_bindings", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def create_legacy(path: Path, rows: list[tuple[int, str | None]]) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, steam_id TEXT)")
        connection.executemany("INSERT INTO users VALUES (?, ?)", rows)


def create_target(path: Path, rows: list[tuple[str, str, str]]) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE bindings (
                group_id TEXT NOT NULL,
                qq_id TEXT NOT NULL,
                steam_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (group_id, qq_id),
                UNIQUE (group_id, steam_id)
            )
            """
        )
        connection.executemany(
            "INSERT INTO bindings VALUES (?, ?, ?, 'old', 'old')",
            rows,
        )


def test_plan_and_apply_preserve_current_conflicts(tmp_path: Path) -> None:
    legacy_path = tmp_path / "legacy.db"
    target_path = tmp_path / "target.db"
    create_legacy(
        legacy_path,
        [
            (100001, "76561199000000001"),
            (100002, "76561199000000002"),
            (100003, "76561199000000003"),
            (100004, "76561199000000004"),
            (100005, None),
        ],
    )
    create_target(
        target_path,
        [
            ("900001", "100001", "76561199000000001"),
            ("900001", "100003", "76561199000000999"),
            ("900001", "199999", "76561199000000004"),
        ],
    )

    legacy = MODULE.load_legacy_bindings(legacy_path)
    with sqlite3.connect(target_path) as connection:
        plan = MODULE.build_plan(connection, legacy, None)
        backup = MODULE.apply_plan(connection, plan, tmp_path / "backups")

    assert legacy.empty_rows == 1
    assert plan.unchanged_rows == 1
    assert plan.qq_conflicts == 1
    assert plan.steam_conflicts == 1
    assert plan.inserts == (("100002", "76561199000000002"),)
    assert backup is not None and backup.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(target_path) as connection:
        rows = connection.execute(
            "SELECT qq_id, steam_id FROM bindings WHERE group_id = '900001' ORDER BY qq_id"
        ).fetchall()
    assert rows == [
        ("100001", "76561199000000001"),
        ("100002", "76561199000000002"),
        ("100003", "76561199000000999"),
        ("199999", "76561199000000004"),
    ]


def test_auto_group_requires_exactly_one_existing_group(tmp_path: Path) -> None:
    legacy_path = tmp_path / "legacy.db"
    target_path = tmp_path / "target.db"
    create_legacy(legacy_path, [(100001, "76561199000000001")])
    create_target(
        target_path,
        [
            ("900001", "100001", "76561199000000001"),
            ("900002", "100002", "76561199000000002"),
        ],
    )

    legacy = MODULE.load_legacy_bindings(legacy_path)
    with sqlite3.connect(target_path) as connection:
        with pytest.raises(MODULE.MigrationError, match="恰好一个群"):
            MODULE.build_plan(connection, legacy, None)
