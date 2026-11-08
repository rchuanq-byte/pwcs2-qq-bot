#!/usr/bin/env python3
"""Safely import legacy global QQ-to-Steam bindings into one current QQ group."""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

QQ_ID_RE = re.compile(r"^[1-9]\d{4,11}$")
STEAM_ID_RE = re.compile(r"^7656119\d{10}$")
REQUIRED_LEGACY_COLUMNS = {"user_id", "steam_id"}
REQUIRED_TARGET_COLUMNS = {
    "group_id",
    "qq_id",
    "steam_id",
    "created_at",
    "updated_at",
}


class MigrationError(RuntimeError):
    """A safe-to-display migration failure."""


@dataclass(frozen=True, slots=True)
class LegacyBindings:
    total_rows: int
    empty_rows: int
    invalid_rows: int
    bindings: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    group_id: str
    existing_rows: int
    unchanged_rows: int
    qq_conflicts: int
    steam_conflicts: int
    inserts: tuple[tuple[str, str], ...]


def connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise MigrationError(f"数据库文件不存在：{path}")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    connection.execute("PRAGMA query_only = ON")
    connection.row_factory = sqlite3.Row
    return connection


def require_integrity(connection: sqlite3.Connection, label: str) -> None:
    result = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise MigrationError(f"{label}完整性检查失败")


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    escaped = table.replace('"', '""')
    return {
        str(row[1]) for row in connection.execute(f'PRAGMA table_info("{escaped}")')
    }


def load_legacy_bindings(path: Path) -> LegacyBindings:
    connection = connect_readonly(path)
    try:
        require_integrity(connection, "旧数据库")
        columns = table_columns(connection, "users")
        if not REQUIRED_LEGACY_COLUMNS.issubset(columns):
            raise MigrationError("旧数据库 users 表缺少 user_id 或 steam_id 字段")
        rows = connection.execute("SELECT user_id, steam_id FROM users").fetchall()
    finally:
        connection.close()

    empty_rows = 0
    invalid_rows = 0
    by_qq: dict[str, str] = {}
    by_steam: dict[str, str] = {}
    for row in rows:
        qq_id = str(row["user_id"] or "").strip()
        steam_id = str(row["steam_id"] or "").strip()
        if not steam_id:
            empty_rows += 1
            continue
        if not QQ_ID_RE.fullmatch(qq_id) or not STEAM_ID_RE.fullmatch(steam_id):
            invalid_rows += 1
            continue
        if previous := by_qq.get(qq_id):
            if previous != steam_id:
                raise MigrationError("旧数据库存在同一 QQ 对应多个 SteamID 的冲突")
            continue
        if previous_qq := by_steam.get(steam_id):
            if previous_qq != qq_id:
                raise MigrationError("旧数据库存在同一 SteamID 对应多个 QQ 的冲突")
            continue
        by_qq[qq_id] = steam_id
        by_steam[steam_id] = qq_id

    return LegacyBindings(
        total_rows=len(rows),
        empty_rows=empty_rows,
        invalid_rows=invalid_rows,
        bindings=tuple(sorted(by_qq.items())),
    )


def select_group_id(connection: sqlite3.Connection, requested: str | None) -> str:
    if requested:
        group_id = requested.strip()
        if not QQ_ID_RE.fullmatch(group_id):
            raise MigrationError("--group-id 格式不正确")
        return group_id

    groups = [
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT group_id FROM bindings ORDER BY group_id"
        )
    ]
    if len(groups) != 1:
        raise MigrationError("当前数据库不是恰好一个群，请显式提供 --group-id")
    return groups[0]


def build_plan(
    connection: sqlite3.Connection,
    legacy: LegacyBindings,
    requested_group_id: str | None,
) -> MigrationPlan:
    require_integrity(connection, "当前数据库")
    if not REQUIRED_TARGET_COLUMNS.issubset(table_columns(connection, "bindings")):
        raise MigrationError("当前数据库 bindings 表结构不兼容")

    group_id = select_group_id(connection, requested_group_id)
    current_rows = connection.execute(
        "SELECT qq_id, steam_id FROM bindings WHERE group_id = ?",
        (group_id,),
    ).fetchall()
    by_qq = {str(row[0]): str(row[1]) for row in current_rows}
    by_steam = {str(row[1]): str(row[0]) for row in current_rows}
    unchanged_rows = 0
    qq_conflicts = 0
    steam_conflicts = 0
    inserts: list[tuple[str, str]] = []
    for qq_id, steam_id in legacy.bindings:
        if existing_steam := by_qq.get(qq_id):
            if existing_steam == steam_id:
                unchanged_rows += 1
            else:
                qq_conflicts += 1
            continue
        if steam_id in by_steam:
            steam_conflicts += 1
            continue
        inserts.append((qq_id, steam_id))
        by_qq[qq_id] = steam_id
        by_steam[steam_id] = qq_id

    return MigrationPlan(
        group_id=group_id,
        existing_rows=len(current_rows),
        unchanged_rows=unchanged_rows,
        qq_conflicts=qq_conflicts,
        steam_conflicts=steam_conflicts,
        inserts=tuple(inserts),
    )


def backup_database(connection: sqlite3.Connection, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    path = directory / f"bindings-before-legacy-import-{timestamp}.db"
    backup = sqlite3.connect(path)
    try:
        connection.backup(backup)
        require_integrity(backup, "迁移备份")
    finally:
        backup.close()
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def apply_plan(
    connection: sqlite3.Connection,
    plan: MigrationPlan,
    backup_dir: Path,
) -> Path | None:
    if not plan.inserts:
        return None
    backup_path = backup_database(connection, backup_dir)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany(
            """
            INSERT INTO bindings (group_id, qq_id, steam_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (plan.group_id, qq_id, steam_id, now, now)
                for qq_id, steam_id in plan.inserts
            ],
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    require_integrity(connection, "迁移后数据库")
    return backup_path


def print_report(legacy: LegacyBindings, plan: MigrationPlan, mode: str) -> None:
    print(f"legacy_rows={legacy.total_rows}")
    print(f"legacy_valid_bindings={len(legacy.bindings)}")
    print(f"legacy_empty_bindings={legacy.empty_rows}")
    print(f"legacy_invalid_bindings={legacy.invalid_rows}")
    print(f"existing_bindings_in_target_group={plan.existing_rows}")
    print(f"unchanged_bindings={plan.unchanged_rows}")
    print(f"qq_conflicts_skipped={plan.qq_conflicts}")
    print(f"steam_conflicts_skipped={plan.steam_conflicts}")
    print(f"bindings_to_insert={len(plan.inserts)}")
    print(f"mode={mode}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("legacy_db", type=Path)
    parser.add_argument("--target-db", type=Path, default=Path("data/bot/bindings.db"))
    parser.add_argument("--group-id")
    parser.add_argument("--backup-dir", type=Path, default=Path("backups"))
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    legacy = load_legacy_bindings(args.legacy_db)
    if args.apply:
        connection = sqlite3.connect(args.target_db, timeout=10)
    else:
        connection = connect_readonly(args.target_db)
    try:
        plan = build_plan(connection, legacy, args.group_id)
        print_report(legacy, plan, "apply" if args.apply else "dry-run")
        if not args.apply:
            print("OK 预览完成，未修改当前数据库")
            return 0
        backup_path = apply_plan(connection, plan, args.backup_dir)
    finally:
        connection.close()

    if backup_path is None:
        print("OK 无需导入，当前绑定已包含所有可迁移记录")
    else:
        print(f"backup={backup_path}")
        print(f"OK 已导入 {len(plan.inserts)} 条旧绑定")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (MigrationError, OSError, sqlite3.Error) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        raise SystemExit(1) from None
