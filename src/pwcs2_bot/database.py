"""SQLite persistence for QQ-to-Steam bindings."""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4


class BindingConflictError(ValueError):
    """Raised when a Steam account is already bound in the same QQ group."""


class ValorantBindingConflictError(ValueError):
    """Raised when a Riot ID is already bound in the same QQ group."""


@dataclass(frozen=True, slots=True)
class Binding:
    group_id: str
    qq_id: str
    steam_id: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ValorantBinding:
    group_id: str
    qq_id: str
    riot_id: str
    riot_id_key: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ValorantCacheEntry:
    riot_id: str
    riot_id_key: str
    payload: dict[str, Any]
    fetched_at: str


@dataclass(frozen=True, slots=True)
class GroupMemberMemory:
    group_id: str
    qq_id: str
    nickname: str
    steam_id: str
    stats: dict[str, Any]
    last_seen_at: str
    stats_updated_at: str | None


@dataclass(frozen=True, slots=True)
class GroupAIMemory:
    memory_id: int
    group_id: str
    content: str
    created_by_qq: str
    created_by_nickname: str
    created_at: str


@dataclass(frozen=True, slots=True)
class GroupMemberCompliance:
    group_id: str
    qq_id: str
    first_noncompliant_date: str
    last_checked_date: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SkinWatchItem:
    item_id: int
    group_id: str
    market_hash_name: str
    added_by_qq: str
    created_at: str


@dataclass(frozen=True, slots=True)
class PartyMember:
    qq_id: str
    nickname: str
    joined_at: str


@dataclass(frozen=True, slots=True)
class GameParty:
    party_id: int
    group_id: str
    name: str
    owner_qq: str
    capacity: int
    scheduled_for: str
    created_at: str
    updated_at: str
    members: tuple[PartyMember, ...]


@dataclass(frozen=True, slots=True)
class DailyAdminRun:
    group_id: str
    duty_date: str
    source_date: str
    previous_qq_id: str | None
    winner_qq_id: str | None
    winner_nickname: str
    match_count: int
    message_count: int
    match_score: float
    message_score: float
    total_score: float
    ranking: tuple[dict[str, Any], ...]
    status: str
    announcement_sent: bool


@dataclass(frozen=True, slots=True)
class AdminBanGuardEvent:
    event_id: int
    group_id: str
    operator_qq_id: str
    target_qq_id: str
    duration_seconds: int
    unmuted: bool
    admin_revoked: bool
    handled_at: str


@dataclass(frozen=True, slots=True)
class StreamLiveNotification:
    group_id: str
    platform: str
    room_id: str
    session_key: str
    snapshot: dict[str, Any]
    created_at: str
    claimed_at: str
    attempt_count: int


@dataclass(frozen=True, slots=True)
class HighlightRenderJob:
    """One leased request for a renderer worker.

    ``payload`` is the versioned manifest produced by ``demo_highlights``.  A
    lease token makes completion/retry compare-and-swap safe when a stale job is
    reclaimed by another worker.
    """

    job_id: int
    dedupe_key: str
    group_id: str
    match_id: str
    steam_id: str
    payload: dict[str, Any]
    priority: int
    status: str
    created_at: str
    available_at: str
    claimed_at: str
    lease_token: str
    worker_id: str
    attempt_count: int
    output: dict[str, Any] | None = None
    last_error: str = ""
    completed_at: str = ""


class BindingRepository:
    """Small synchronous repository; each operation owns its SQLite connection."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @staticmethod
    def _migrate_game_party_schema(connection: sqlite3.Connection) -> None:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'game_parties'"
        ).fetchone()
        if table is None:
            return
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(game_parties)").fetchall()
        }
        if "party_id" in columns:
            return

        connection.execute(
            """
            CREATE TABLE game_parties_new (
                party_id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                name TEXT NOT NULL COLLATE NOCASE,
                owner_qq TEXT NOT NULL,
                capacity INTEGER NOT NULL,
                scheduled_for TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (group_id, name)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO game_parties_new
                (group_id, name, owner_qq, capacity, scheduled_for,
                 created_at, updated_at)
            SELECT group_id, '默认车队', owner_qq, capacity, scheduled_for,
                   created_at, updated_at
            FROM game_parties
            """
        )
        connection.execute(
            """
            CREATE TABLE game_party_members_new (
                party_id INTEGER NOT NULL,
                qq_id TEXT NOT NULL,
                nickname TEXT NOT NULL,
                joined_at TEXT NOT NULL,
                PRIMARY KEY (party_id, qq_id),
                FOREIGN KEY (party_id) REFERENCES game_parties_new(party_id)
                    ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            INSERT INTO game_party_members_new
                (party_id, qq_id, nickname, joined_at)
            SELECT parties.party_id, members.qq_id, members.nickname,
                   members.joined_at
            FROM game_party_members AS members
            JOIN game_parties_new AS parties
              ON parties.group_id = members.group_id
            """
        )
        connection.execute("DROP TABLE game_party_members")
        connection.execute("DROP TABLE game_parties")
        connection.execute("ALTER TABLE game_parties_new RENAME TO game_parties")
        connection.execute(
            "ALTER TABLE game_party_members_new RENAME TO game_party_members"
        )

    @staticmethod
    def _migrate_stream_live_notification_schema(
        connection: sqlite3.Connection,
    ) -> None:
        """Upgrade the live-notification outbox without losing queued events."""
        row = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'stream_live_notifications'"
        ).fetchone()
        if row is None:
            return
        columns = {
            str(item["name"])
            for item in connection.execute(
                "PRAGMA table_info(stream_live_notifications)"
            ).fetchall()
        }
        table_sql = str(row["sql"] or "").casefold()
        required = {"attempt_count", "next_attempt_at", "last_error"}
        if required.issubset(columns) and "'failed'" in table_sql:
            return

        connection.execute(
            "ALTER TABLE stream_live_notifications "
            "RENAME TO stream_live_notifications_legacy"
        )
        connection.execute(
            """
            CREATE TABLE stream_live_notifications (
                group_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                room_id TEXT NOT NULL,
                session_key TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                status TEXT NOT NULL
                    CHECK (status IN ('pending', 'sending', 'sent', 'failed')),
                created_at TEXT NOT NULL,
                claimed_at TEXT,
                sent_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (group_id, platform, room_id, session_key)
            )
            """
        )
        attempt_count = "attempt_count" if "attempt_count" in columns else "0"
        next_attempt_at = (
            "COALESCE(next_attempt_at, '')" if "next_attempt_at" in columns else "''"
        )
        last_error = "COALESCE(last_error, '')" if "last_error" in columns else "''"
        connection.execute(
            f"""
            INSERT INTO stream_live_notifications
                (group_id, platform, room_id, session_key, snapshot_json,
                 status, created_at, claimed_at, sent_at, attempt_count,
                 next_attempt_at, last_error)
            SELECT group_id, platform, room_id, session_key, snapshot_json,
                   CASE WHEN status IN ('pending', 'sending', 'sent', 'failed')
                        THEN status ELSE 'pending' END,
                   created_at, claimed_at, sent_at, {attempt_count},
                   {next_attempt_at}, {last_error}
            FROM stream_live_notifications_legacy
            """
        )
        connection.execute("DROP TABLE stream_live_notifications_legacy")

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            self._migrate_game_party_schema(connection)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bindings (
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS valorant_bindings (
                    group_id TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    riot_id TEXT NOT NULL,
                    riot_id_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, qq_id),
                    UNIQUE (group_id, riot_id_key)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS valorant_stats_cache (
                    riot_id_key TEXT PRIMARY KEY,
                    riot_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS match_tracking (
                    group_id TEXT NOT NULL,
                    steam_id TEXT NOT NULL,
                    initialized_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, steam_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS match_source_tracking (
                    group_id TEXT NOT NULL,
                    steam_id TEXT NOT NULL,
                    data_source INTEGER NOT NULL,
                    initialized_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, steam_id, data_source)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS match_source_notifications (
                    group_id TEXT NOT NULL,
                    steam_id TEXT NOT NULL,
                    data_source INTEGER NOT NULL,
                    match_id TEXT NOT NULL,
                    notified_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, steam_id, data_source, match_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS group_match_source_notifications (
                    group_id TEXT NOT NULL,
                    data_source INTEGER NOT NULL,
                    match_id TEXT NOT NULL,
                    notified_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, data_source, match_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS match_notifications (
                    group_id TEXT NOT NULL,
                    steam_id TEXT NOT NULL,
                    match_id TEXT NOT NULL,
                    notified_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, steam_id, match_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS group_match_notifications (
                    group_id TEXT NOT NULL,
                    match_id TEXT NOT NULL,
                    notified_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, match_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS stream_live_states (
                    platform TEXT NOT NULL,
                    room_id TEXT NOT NULL,
                    confirmed_live INTEGER NOT NULL,
                    confirmed_session_key TEXT NOT NULL,
                    candidate_live INTEGER,
                    candidate_session_key TEXT NOT NULL DEFAULT '',
                    candidate_count INTEGER NOT NULL DEFAULT 0,
                    last_snapshot_json TEXT NOT NULL,
                    last_success_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (platform, room_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS stream_live_notifications (
                    group_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    room_id TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('pending', 'sending', 'sent', 'failed')),
                    created_at TEXT NOT NULL,
                    claimed_at TEXT,
                    sent_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (group_id, platform, room_id, session_key)
                )
                """
            )
            self._migrate_stream_live_notification_schema(connection)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS highlight_render_jobs (
                    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    group_id TEXT NOT NULL,
                    match_id TEXT NOT NULL,
                    steam_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL
                        CHECK (status IN ('pending', 'running', 'completed', 'failed')),
                    available_at TEXT NOT NULL DEFAULT '',
                    claimed_at TEXT,
                    lease_token TEXT NOT NULL DEFAULT '',
                    worker_id TEXT NOT NULL DEFAULT '',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    output_json TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_highlight_render_jobs_due
                ON highlight_render_jobs
                    (status, available_at, priority DESC, created_at, job_id)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_summary_runs (
                    group_id TEXT NOT NULL,
                    summary_date TEXT NOT NULL,
                    was_sent INTEGER NOT NULL,
                    completed_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, summary_date)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS group_level_reminder_runs (
                    group_id TEXT NOT NULL,
                    reminder_date TEXT NOT NULL,
                    target_count INTEGER NOT NULL,
                    completed_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, reminder_date)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS group_member_compliance (
                    group_id TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    first_noncompliant_date TEXT NOT NULL,
                    last_checked_date TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    PRIMARY KEY (group_id, qq_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS unbound_message_usage (
                    group_id TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    usage_date TEXT NOT NULL,
                    message_count INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, qq_id, usage_date)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS content_relay_deliveries (
                    target_group_id TEXT NOT NULL,
                    source_group_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    sent_date TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY (target_group_id, content_hash)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS group_member_memory (
                    group_id TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    stats_json TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    stats_updated_at TEXT,
                    PRIMARY KEY (group_id, qq_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS group_ai_memories (
                    memory_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_by_qq TEXT NOT NULL,
                    created_by_nickname TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_group_ai_memories_group
                ON group_ai_memories (group_id, memory_id)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_word_counts (
                    group_id TEXT NOT NULL,
                    count_date TEXT NOT NULL,
                    word TEXT NOT NULL,
                    word_count INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, count_date, word)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_word_cloud_activity (
                    group_id TEXT NOT NULL,
                    count_date TEXT NOT NULL,
                    message_count INTEGER NOT NULL,
                    token_count INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, count_date)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_word_cloud_runs (
                    group_id TEXT NOT NULL,
                    cloud_date TEXT NOT NULL,
                    was_sent INTEGER NOT NULL,
                    completed_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, cloud_date)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_member_activity (
                    group_id TEXT NOT NULL,
                    activity_date TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    message_count INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, activity_date, qq_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_admin_runs (
                    group_id TEXT NOT NULL,
                    duty_date TEXT NOT NULL,
                    source_date TEXT NOT NULL,
                    previous_qq_id TEXT,
                    winner_qq_id TEXT,
                    winner_nickname TEXT NOT NULL,
                    match_count INTEGER NOT NULL,
                    message_count INTEGER NOT NULL,
                    match_score REAL NOT NULL,
                    message_score REAL NOT NULL,
                    total_score REAL NOT NULL,
                    ranking_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    announcement_sent INTEGER NOT NULL,
                    completed_at TEXT,
                    PRIMARY KEY (group_id, duty_date)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_admin_state (
                    group_id TEXT PRIMARY KEY,
                    qq_id TEXT NOT NULL,
                    duty_date TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS group_card_locks (
                    group_id TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    card TEXT NOT NULL,
                    updated_by_qq TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, qq_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_ban_guard_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    operator_qq_id TEXT NOT NULL,
                    target_qq_id TEXT NOT NULL,
                    duration_seconds INTEGER NOT NULL,
                    unmuted INTEGER NOT NULL,
                    admin_revoked INTEGER NOT NULL,
                    handled_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS skin_price_watchlist (
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    market_hash_name TEXT NOT NULL COLLATE NOCASE,
                    added_by_qq TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (group_id, market_hash_name)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS skin_price_snapshots (
                    group_id TEXT NOT NULL,
                    market_hash_name TEXT NOT NULL COLLATE NOCASE,
                    snapshot_date TEXT NOT NULL,
                    sell_price_cny REAL NOT NULL,
                    buy_price_cny REAL,
                    source_price_usd REAL,
                    usd_cny_rate REAL NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, market_hash_name, snapshot_date)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS skin_price_report_runs (
                    group_id TEXT NOT NULL,
                    report_date TEXT NOT NULL,
                    was_sent INTEGER NOT NULL,
                    completed_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, report_date)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS game_parties (
                    party_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    name TEXT NOT NULL COLLATE NOCASE,
                    owner_qq TEXT NOT NULL,
                    capacity INTEGER NOT NULL,
                    scheduled_for TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (group_id, name)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS game_party_members (
                    party_id INTEGER NOT NULL,
                    qq_id TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    joined_at TEXT NOT NULL,
                    PRIMARY KEY (party_id, qq_id),
                    FOREIGN KEY (party_id) REFERENCES game_parties(party_id)
                        ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_game_parties_group
                ON game_parties (group_id, created_at, party_id)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS match_commentary_preferences (
                    group_id TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, qq_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS weekly_chemistry_runs (
                    group_id TEXT NOT NULL,
                    week_start TEXT NOT NULL,
                    was_sent INTEGER NOT NULL,
                    completed_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, week_start)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_content_relay_target_date
                ON content_relay_deliveries (target_group_id, sent_date)
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO group_match_notifications
                    (group_id, match_id, notified_at)
                SELECT group_id, match_id, MIN(notified_at)
                FROM match_notifications
                GROUP BY group_id, match_id
                """
            )

    def bind(self, group_id: str, qq_id: str, steam_id: str) -> Binding:
        """Create or replace one QQ member's binding within a group."""
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            previous = connection.execute(
                "SELECT steam_id FROM bindings WHERE group_id = ? AND qq_id = ?",
                (group_id, qq_id),
            ).fetchone()
            owner = connection.execute(
                "SELECT qq_id FROM bindings WHERE group_id = ? AND steam_id = ?",
                (group_id, steam_id),
            ).fetchone()
            if owner is not None and owner["qq_id"] != qq_id:
                raise BindingConflictError("该 Steam 账号已被本群其他成员绑定")

            connection.execute(
                """
                INSERT INTO bindings (group_id, qq_id, steam_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(group_id, qq_id) DO UPDATE SET
                    steam_id = excluded.steam_id,
                    updated_at = excluded.updated_at
                """,
                (group_id, qq_id, steam_id, now, now),
            )
            if previous is None or previous["steam_id"] != steam_id:
                connection.execute(
                    "DELETE FROM match_tracking WHERE group_id = ? AND steam_id = ?",
                    (group_id, steam_id),
                )
                connection.execute(
                    "DELETE FROM match_source_tracking "
                    "WHERE group_id = ? AND steam_id = ?",
                    (group_id, steam_id),
                )
                connection.execute(
                    "DELETE FROM match_notifications WHERE group_id = ? AND steam_id = ?",
                    (group_id, steam_id),
                )
                connection.execute(
                    "DELETE FROM match_source_notifications "
                    "WHERE group_id = ? AND steam_id = ?",
                    (group_id, steam_id),
                )
                connection.execute(
                    """
                    UPDATE group_member_memory
                    SET stats_json = '{}', stats_updated_at = NULL
                    WHERE group_id = ? AND qq_id = ?
                    """,
                    (group_id, qq_id),
                )
                if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'steam_presence_states'"
                ).fetchone():
                    previous_steam_id = (
                        str(previous["steam_id"]) if previous is not None else steam_id
                    )
                    connection.execute(
                        "DELETE FROM steam_presence_states "
                        "WHERE group_id = ? AND steam_id IN (?, ?)",
                        (group_id, previous_steam_id, steam_id),
                    )
                if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'watch_stage_states'"
                ).fetchone():
                    previous_steam_id = (
                        str(previous["steam_id"]) if previous is not None else steam_id
                    )
                    connection.execute(
                        "DELETE FROM watch_stage_states "
                        "WHERE group_id = ? AND steam_id IN (?, ?)",
                        (group_id, previous_steam_id, steam_id),
                    )
            row = connection.execute(
                "SELECT * FROM bindings WHERE group_id = ? AND qq_id = ?",
                (group_id, qq_id),
            ).fetchone()
        assert row is not None
        return self._from_row(row)

    @staticmethod
    def _valorant_key(riot_id: str) -> str:
        return unicodedata.normalize("NFKC", riot_id).strip().casefold()

    def bind_valorant(self, group_id: str, qq_id: str, riot_id: str) -> ValorantBinding:
        """Create or replace one QQ member's independent VALORANT binding."""
        now = datetime.now(UTC).isoformat(timespec="seconds")
        riot_id_key = self._valorant_key(riot_id)
        with self._connect() as connection:
            owner = connection.execute(
                """
                SELECT qq_id FROM valorant_bindings
                WHERE group_id = ? AND riot_id_key = ?
                """,
                (group_id, riot_id_key),
            ).fetchone()
            if owner is not None and str(owner["qq_id"]) != qq_id:
                raise ValorantBindingConflictError("该瓦 ID 已被本群其他成员绑定")
            connection.execute(
                """
                INSERT INTO valorant_bindings
                    (group_id, qq_id, riot_id, riot_id_key, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_id, qq_id) DO UPDATE SET
                    riot_id = excluded.riot_id,
                    riot_id_key = excluded.riot_id_key,
                    updated_at = excluded.updated_at
                """,
                (group_id, qq_id, riot_id, riot_id_key, now, now),
            )
            row = connection.execute(
                """
                SELECT * FROM valorant_bindings
                WHERE group_id = ? AND qq_id = ?
                """,
                (group_id, qq_id),
            ).fetchone()
        assert row is not None
        return self._valorant_binding_from_row(row)

    def get_valorant_by_qq(
        self,
        group_id: str,
        qq_id: str,
    ) -> ValorantBinding | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM valorant_bindings
                WHERE group_id = ? AND qq_id = ?
                """,
                (group_id, qq_id),
            ).fetchone()
        return self._valorant_binding_from_row(row) if row is not None else None

    def get_valorant_by_id(
        self,
        group_id: str,
        riot_id: str,
    ) -> ValorantBinding | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM valorant_bindings
                WHERE group_id = ? AND riot_id_key = ?
                """,
                (group_id, self._valorant_key(riot_id)),
            ).fetchone()
        return self._valorant_binding_from_row(row) if row is not None else None

    def unbind_valorant(self, group_id: str, qq_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM valorant_bindings
                WHERE group_id = ? AND qq_id = ?
                """,
                (group_id, qq_id),
            )
        return cursor.rowcount > 0

    def list_valorant_bindings(
        self,
        group_id: str | None = None,
    ) -> tuple[ValorantBinding, ...]:
        with self._connect() as connection:
            if group_id is None:
                rows = connection.execute(
                    """
                    SELECT * FROM valorant_bindings
                    ORDER BY group_id, riot_id_key
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM valorant_bindings
                    WHERE group_id = ?
                    ORDER BY riot_id_key
                    """,
                    (group_id,),
                ).fetchall()
        return tuple(self._valorant_binding_from_row(row) for row in rows)

    def list_binding_group_ids(self) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT group_id FROM bindings
                UNION
                SELECT group_id FROM valorant_bindings
                UNION
                SELECT group_id FROM group_ai_memories
                ORDER BY group_id
                """
            ).fetchall()
        return tuple(str(row["group_id"]) for row in rows)

    def list_bound_qq_ids(self, group_id: str) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT qq_id FROM bindings WHERE group_id = ?
                UNION
                SELECT qq_id FROM valorant_bindings WHERE group_id = ?
                ORDER BY qq_id
                """,
                (group_id, group_id),
            ).fetchall()
        return tuple(str(row["qq_id"]) for row in rows)

    def save_valorant_cache(
        self,
        riot_id: str,
        payload: dict[str, Any],
        *,
        fetched_at: str,
    ) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 256 * 1024:
            raise ValueError("VALORANT cache payload is too large")
        riot_id_key = self._valorant_key(riot_id)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO valorant_stats_cache
                    (riot_id_key, riot_id, payload_json, fetched_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(riot_id_key) DO UPDATE SET
                    riot_id = excluded.riot_id,
                    payload_json = excluded.payload_json,
                    fetched_at = excluded.fetched_at
                """,
                (riot_id_key, riot_id, encoded, fetched_at),
            )
            connection.execute(
                """
                DELETE FROM valorant_stats_cache
                WHERE datetime(fetched_at) < datetime('now', '-30 days')
                """
            )

    def get_valorant_cache(self, riot_id: str) -> ValorantCacheEntry | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM valorant_stats_cache
                WHERE riot_id_key = ?
                """,
                (self._valorant_key(riot_id),),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return ValorantCacheEntry(
            riot_id=str(row["riot_id"]),
            riot_id_key=str(row["riot_id_key"]),
            payload=payload,
            fetched_at=str(row["fetched_at"]),
        )

    def get_by_qq(self, group_id: str, qq_id: str) -> Binding | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM bindings WHERE group_id = ? AND qq_id = ?",
                (group_id, qq_id),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def get_by_steam(self, group_id: str, steam_id: str) -> Binding | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM bindings WHERE group_id = ? AND steam_id = ?",
                (group_id, steam_id),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def upsert_group_member_identity(
        self,
        group_id: str,
        qq_id: str,
        nickname: str,
    ) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        normalized_nickname = " ".join(nickname.split()).strip()[:80]
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO group_member_memory
                    (group_id, qq_id, nickname, stats_json, last_seen_at)
                VALUES (?, ?, ?, '{}', ?)
                ON CONFLICT(group_id, qq_id) DO UPDATE SET
                    nickname = CASE
                        WHEN excluded.nickname <> '' THEN excluded.nickname
                        ELSE group_member_memory.nickname
                    END,
                    last_seen_at = excluded.last_seen_at
                """,
                (group_id, qq_id, normalized_nickname, now),
            )

    def update_group_member_stats(
        self,
        group_id: str,
        qq_id: str,
        stats: dict[str, Any],
    ) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT stats_json FROM group_member_memory
                WHERE group_id = ? AND qq_id = ?
                """,
                (group_id, qq_id),
            ).fetchone()
            existing: dict[str, Any] = {}
            if row is not None:
                try:
                    payload = json.loads(str(row["stats_json"]))
                    if isinstance(payload, dict):
                        existing = payload
                except json.JSONDecodeError:
                    pass
            for key, value in stats.items():
                previous_value = existing.get(key)
                if isinstance(previous_value, dict) and isinstance(value, dict):
                    previous_value.update(value)
                else:
                    existing[key] = value
            serialized = json.dumps(
                existing,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                """
                INSERT INTO group_member_memory
                    (group_id, qq_id, nickname, stats_json, last_seen_at,
                     stats_updated_at)
                VALUES (?, ?, '', ?, ?, ?)
                ON CONFLICT(group_id, qq_id) DO UPDATE SET
                    stats_json = excluded.stats_json,
                    stats_updated_at = excluded.stats_updated_at
                """,
                (group_id, qq_id, serialized, now, now),
            )

    def list_group_member_memories(
        self,
        group_id: str,
    ) -> tuple[GroupMemberMemory, ...]:
        with self._connect() as connection:
            memory_rows = connection.execute(
                """
                SELECT * FROM group_member_memory
                WHERE group_id = ?
                ORDER BY qq_id
                """,
                (group_id,),
            ).fetchall()
            binding_rows = connection.execute(
                """
                SELECT qq_id, steam_id FROM bindings
                WHERE group_id = ?
                """,
                (group_id,),
            ).fetchall()
        bindings = {str(row["qq_id"]): str(row["steam_id"]) for row in binding_rows}
        memories: dict[str, GroupMemberMemory] = {}
        for row in memory_rows:
            qq_id = str(row["qq_id"])
            try:
                stats = json.loads(str(row["stats_json"]))
            except json.JSONDecodeError:
                stats = {}
            if not isinstance(stats, dict):
                stats = {}
            memories[qq_id] = GroupMemberMemory(
                group_id=group_id,
                qq_id=qq_id,
                nickname=str(row["nickname"]),
                steam_id=bindings.get(qq_id, ""),
                stats=stats,
                last_seen_at=str(row["last_seen_at"]),
                stats_updated_at=(
                    str(row["stats_updated_at"])
                    if row["stats_updated_at"] is not None
                    else None
                ),
            )
        now = datetime.now(UTC).isoformat(timespec="seconds")
        for qq_id, steam_id in bindings.items():
            memories.setdefault(
                qq_id,
                GroupMemberMemory(
                    group_id=group_id,
                    qq_id=qq_id,
                    nickname="",
                    steam_id=steam_id,
                    stats={},
                    last_seen_at=now,
                    stats_updated_at=None,
                ),
            )
        return tuple(memories[qq_id] for qq_id in sorted(memories, key=str))

    def remove_group_member_memory(self, group_id: str, qq_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM group_member_memory WHERE group_id = ? AND qq_id = ?",
                (group_id, qq_id),
            )

    def add_group_ai_memory(
        self,
        group_id: str,
        content: str,
        *,
        created_by_qq: str,
        created_by_nickname: str,
    ) -> GroupAIMemory:
        normalized_content = " ".join(content.split()).strip()
        if not normalized_content:
            raise ValueError("memory content must not be empty")
        normalized_nickname = " ".join(created_by_nickname.split()).strip()[:80]
        created_at = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO group_ai_memories
                    (group_id, content, created_by_qq, created_by_nickname, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    normalized_content,
                    created_by_qq,
                    normalized_nickname,
                    created_at,
                ),
            )
            memory_id = int(cursor.lastrowid)
        return GroupAIMemory(
            memory_id=memory_id,
            group_id=group_id,
            content=normalized_content,
            created_by_qq=created_by_qq,
            created_by_nickname=normalized_nickname,
            created_at=created_at,
        )

    def list_group_ai_memories(self, group_id: str) -> tuple[GroupAIMemory, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM group_ai_memories
                WHERE group_id = ?
                ORDER BY memory_id DESC
                """,
                (group_id,),
            ).fetchall()
        return tuple(self._group_ai_memory_from_row(row) for row in rows)

    def get_group_ai_memory(
        self,
        group_id: str,
        memory_id: int,
    ) -> GroupAIMemory | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM group_ai_memories
                WHERE group_id = ? AND memory_id = ?
                """,
                (group_id, memory_id),
            ).fetchone()
        return self._group_ai_memory_from_row(row) if row is not None else None

    def delete_group_ai_memory(self, group_id: str, memory_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM group_ai_memories
                WHERE group_id = ? AND memory_id = ?
                """,
                (group_id, memory_id),
            )
        return cursor.rowcount > 0

    def delete_group_ai_memories_by_author(self, group_id: str, qq_id: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM group_ai_memories
                WHERE group_id = ? AND created_by_qq = ?
                """,
                (group_id, qq_id),
            )
        return max(0, cursor.rowcount)

    @staticmethod
    def _group_ai_memory_from_row(row: sqlite3.Row) -> GroupAIMemory:
        return GroupAIMemory(
            memory_id=int(row["memory_id"]),
            group_id=str(row["group_id"]),
            content=str(row["content"]),
            created_by_qq=str(row["created_by_qq"]),
            created_by_nickname=str(row["created_by_nickname"]),
            created_at=str(row["created_at"]),
        )

    def record_daily_word_counts(
        self,
        group_id: str,
        count_date: str,
        counts: dict[str, int],
    ) -> None:
        normalized = {
            word: int(count)
            for word, count in counts.items()
            if word and int(count) > 0
        }
        if not normalized:
            return
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO daily_word_counts
                    (group_id, count_date, word, word_count, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(group_id, count_date, word) DO UPDATE SET
                    word_count = daily_word_counts.word_count + excluded.word_count,
                    updated_at = excluded.updated_at
                """,
                (
                    (group_id, count_date, word, count, now)
                    for word, count in normalized.items()
                ),
            )
            connection.execute(
                """
                INSERT INTO daily_word_cloud_activity
                    (group_id, count_date, message_count, token_count, updated_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(group_id, count_date) DO UPDATE SET
                    message_count = daily_word_cloud_activity.message_count + 1,
                    token_count = daily_word_cloud_activity.token_count + excluded.token_count,
                    updated_at = excluded.updated_at
                """,
                (group_id, count_date, sum(normalized.values()), now),
            )

    def get_daily_word_counts(
        self,
        group_id: str,
        count_date: str,
        *,
        limit: int = 100,
    ) -> tuple[tuple[str, int], ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT word, word_count FROM daily_word_counts
                WHERE group_id = ? AND count_date = ?
                ORDER BY word_count DESC, word ASC
                LIMIT ?
                """,
                (group_id, count_date, limit),
            ).fetchall()
        return tuple((str(row["word"]), int(row["word_count"])) for row in rows)

    def get_daily_word_cloud_activity(
        self,
        group_id: str,
        count_date: str,
    ) -> tuple[int, int]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT message_count, token_count FROM daily_word_cloud_activity
                WHERE group_id = ? AND count_date = ?
                """,
                (group_id, count_date),
            ).fetchone()
        if row is None:
            return 0, 0
        return int(row["message_count"]), int(row["token_count"])

    def list_daily_word_cloud_groups(self, count_date: str) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT group_id FROM daily_word_cloud_activity
                WHERE count_date = ?
                ORDER BY group_id
                """,
                (count_date,),
            ).fetchall()
        return tuple(str(row["group_id"]) for row in rows)

    def is_daily_word_cloud_processed(self, group_id: str, cloud_date: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM daily_word_cloud_runs
                WHERE group_id = ? AND cloud_date = ?
                """,
                (group_id, cloud_date),
            ).fetchone()
        return row is not None

    def mark_daily_word_cloud_processed(
        self,
        group_id: str,
        cloud_date: str,
        *,
        was_sent: bool,
        retention_days: int = 14,
    ) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO daily_word_cloud_runs
                    (group_id, cloud_date, was_sent, completed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(group_id, cloud_date) DO UPDATE SET
                    was_sent = excluded.was_sent,
                    completed_at = excluded.completed_at
                """,
                (group_id, cloud_date, int(was_sent), now),
            )
            rows = connection.execute(
                """
                SELECT DISTINCT count_date FROM daily_word_cloud_activity
                ORDER BY count_date DESC
                """
            ).fetchall()
            retained_dates = {str(row["count_date"]) for row in rows[:retention_days]}
            for row in rows[retention_days:]:
                old_date = str(row["count_date"])
                if old_date in retained_dates:
                    continue
                connection.execute(
                    "DELETE FROM daily_word_counts WHERE count_date = ?",
                    (old_date,),
                )
                connection.execute(
                    "DELETE FROM daily_word_cloud_activity WHERE count_date = ?",
                    (old_date,),
                )

    def record_daily_member_message(
        self,
        group_id: str,
        activity_date: str,
        qq_id: str,
    ) -> None:
        """Increment one member's daily message count without retaining message text."""
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO daily_member_activity
                    (group_id, activity_date, qq_id, message_count, updated_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(group_id, activity_date, qq_id) DO UPDATE SET
                    message_count = daily_member_activity.message_count + 1,
                    updated_at = excluded.updated_at
                """,
                (group_id, activity_date, qq_id, now),
            )

    def get_daily_member_message_counts(
        self,
        group_id: str,
        activity_date: str,
    ) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT qq_id, message_count FROM daily_member_activity
                WHERE group_id = ? AND activity_date = ?
                """,
                (group_id, activity_date),
            ).fetchall()
        return {str(row["qq_id"]): int(row["message_count"]) for row in rows}

    def list_daily_member_activity_groups(self, activity_date: str) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT group_id FROM daily_member_activity
                WHERE activity_date = ? ORDER BY group_id
                """,
                (activity_date,),
            ).fetchall()
        return tuple(str(row["group_id"]) for row in rows)

    def get_daily_admin(self, group_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT qq_id FROM daily_admin_state WHERE group_id = ?",
                (group_id,),
            ).fetchone()
        return None if row is None else str(row["qq_id"])

    def clear_daily_admin_if(self, group_id: str, qq_id: str) -> bool:
        """Forget a bot-tracked administrator only when it is the named member."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM daily_admin_state
                WHERE group_id = ? AND qq_id = ?
                """,
                (group_id, qq_id),
            )
        return cursor.rowcount > 0

    def record_admin_ban_guard_event(
        self,
        group_id: str,
        operator_qq_id: str,
        target_qq_id: str,
        duration_seconds: int,
        *,
        unmuted: bool,
        admin_revoked: bool,
    ) -> None:
        handled_at = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO admin_ban_guard_events
                    (group_id, operator_qq_id, target_qq_id, duration_seconds,
                     unmuted, admin_revoked, handled_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    operator_qq_id,
                    target_qq_id,
                    duration_seconds,
                    int(unmuted),
                    int(admin_revoked),
                    handled_at,
                ),
            )

    def get_latest_admin_ban_guard_event(
        self,
        group_id: str,
    ) -> AdminBanGuardEvent | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM admin_ban_guard_events
                WHERE group_id = ? ORDER BY event_id DESC LIMIT 1
                """,
                (group_id,),
            ).fetchone()
        if row is None:
            return None
        return AdminBanGuardEvent(
            event_id=int(row["event_id"]),
            group_id=str(row["group_id"]),
            operator_qq_id=str(row["operator_qq_id"]),
            target_qq_id=str(row["target_qq_id"]),
            duration_seconds=int(row["duration_seconds"]),
            unmuted=bool(row["unmuted"]),
            admin_revoked=bool(row["admin_revoked"]),
            handled_at=str(row["handled_at"]),
        )

    def get_daily_admin_run(
        self,
        group_id: str,
        duty_date: str,
    ) -> DailyAdminRun | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM daily_admin_runs
                WHERE group_id = ? AND duty_date = ?
                """,
                (group_id, duty_date),
            ).fetchone()
        if row is None:
            return None
        try:
            raw_ranking = json.loads(str(row["ranking_json"]))
        except json.JSONDecodeError:
            raw_ranking = []
        ranking = tuple(item for item in raw_ranking if isinstance(item, dict))
        return DailyAdminRun(
            group_id=str(row["group_id"]),
            duty_date=str(row["duty_date"]),
            source_date=str(row["source_date"]),
            previous_qq_id=(
                str(row["previous_qq_id"])
                if row["previous_qq_id"] is not None
                else None
            ),
            winner_qq_id=(
                str(row["winner_qq_id"])
                if row["winner_qq_id"] is not None
                else None
            ),
            winner_nickname=str(row["winner_nickname"]),
            match_count=int(row["match_count"]),
            message_count=int(row["message_count"]),
            match_score=float(row["match_score"]),
            message_score=float(row["message_score"]),
            total_score=float(row["total_score"]),
            ranking=ranking,
            status=str(row["status"]),
            announcement_sent=bool(row["announcement_sent"]),
        )

    def plan_daily_admin(self, run: DailyAdminRun) -> None:
        """Persist an assignment plan before touching external QQ permissions."""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO daily_admin_runs
                    (group_id, duty_date, source_date, previous_qq_id,
                     winner_qq_id, winner_nickname, match_count, message_count,
                     match_score, message_score, total_score, ranking_json,
                     status, announcement_sent, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', 0, NULL)
                """,
                (
                    run.group_id,
                    run.duty_date,
                    run.source_date,
                    run.previous_qq_id,
                    run.winner_qq_id,
                    run.winner_nickname,
                    run.match_count,
                    run.message_count,
                    run.match_score,
                    run.message_score,
                    run.total_score,
                    json.dumps(run.ranking, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    def complete_daily_admin_assignment(
        self,
        group_id: str,
        duty_date: str,
        winner_qq_id: str | None,
    ) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            if winner_qq_id is None:
                connection.execute(
                    "DELETE FROM daily_admin_state WHERE group_id = ?",
                    (group_id,),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO daily_admin_state (group_id, qq_id, duty_date, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(group_id) DO UPDATE SET
                        qq_id = excluded.qq_id,
                        duty_date = excluded.duty_date,
                        updated_at = excluded.updated_at
                    """,
                    (group_id, winner_qq_id, duty_date, now),
                )
            connection.execute(
                """
                UPDATE daily_admin_runs
                SET status = 'assigned', completed_at = ?
                WHERE group_id = ? AND duty_date = ?
                """,
                (now, group_id, duty_date),
            )

    def mark_daily_admin_announced(self, group_id: str, duty_date: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE daily_admin_runs SET announcement_sent = 1
                WHERE group_id = ? AND duty_date = ?
                """,
                (group_id, duty_date),
            )
            connection.execute(
                """
                DELETE FROM daily_member_activity
                WHERE activity_date < date(?, '-90 days')
                """,
                (duty_date,),
            )

    def get_group_card_lock(self, group_id: str, qq_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT card FROM group_card_locks
                WHERE group_id = ? AND qq_id = ?
                """,
                (group_id, qq_id),
            ).fetchone()
        return None if row is None else str(row["card"])

    def list_group_card_lock_group_ids(self) -> tuple[str, ...]:
        """Return groups that already have at least one persisted card lock."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT group_id FROM group_card_locks ORDER BY group_id"
            ).fetchall()
        return tuple(str(row["group_id"]) for row in rows)

    def set_group_card_lock(
        self,
        group_id: str,
        qq_id: str,
        card: str,
        *,
        updated_by_qq: str,
    ) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO group_card_locks
                    (group_id, qq_id, card, updated_by_qq, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(group_id, qq_id) DO UPDATE SET
                    card = excluded.card,
                    updated_by_qq = excluded.updated_by_qq,
                    updated_at = excluded.updated_at
                """,
                (group_id, qq_id, card, updated_by_qq, now),
            )

    def ensure_group_card_locks(
        self,
        group_id: str,
        cards: dict[str, str],
    ) -> None:
        """Store a first-seen baseline without overwriting administrator choices."""
        normalized = {
            str(qq_id): str(card)
            for qq_id, card in cards.items()
            if str(qq_id).isdecimal()
        }
        if not normalized:
            return
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO group_card_locks
                    (group_id, qq_id, card, updated_by_qq, updated_at)
                VALUES (?, ?, ?, 'baseline', ?)
                """,
                (
                    (group_id, qq_id, card, now)
                    for qq_id, card in normalized.items()
                ),
            )

    def clear_group_card_lock(self, group_id: str, qq_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM group_card_locks
                WHERE group_id = ? AND qq_id = ?
                """,
                (group_id, qq_id),
            )

    def list_bindings(self) -> tuple[Binding, ...]:
        """Return every binding in a stable order for background polling."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM bindings ORDER BY group_id, steam_id"
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def list_group_bindings(self, group_id: str) -> tuple[Binding, ...]:
        """Return one group's bindings in a stable order."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM bindings
                WHERE group_id = ?
                ORDER BY steam_id
                """,
                (group_id,),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def remove_members_not_in_group(
        self,
        group_id: str,
        member_qq_ids: set[str],
    ) -> tuple[Binding, ...]:
        """Delete bindings whose QQ accounts are absent from a verified member list."""
        normalized_ids = {str(value) for value in member_qq_ids}
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM bindings WHERE group_id = ? ORDER BY qq_id",
                (group_id,),
            ).fetchall()
            stale = tuple(
                self._from_row(row)
                for row in rows
                if str(row["qq_id"]) not in normalized_ids
            )
            valorant_rows = connection.execute(
                "SELECT qq_id FROM valorant_bindings WHERE group_id = ?",
                (group_id,),
            ).fetchall()
            for row in valorant_rows:
                if str(row["qq_id"]) not in normalized_ids:
                    connection.execute(
                        """
                        DELETE FROM valorant_bindings
                        WHERE group_id = ? AND qq_id = ?
                        """,
                        (group_id, str(row["qq_id"])),
                    )
            has_presence_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'steam_presence_states'"
            ).fetchone()
            has_watch_stage_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'watch_stage_states'"
            ).fetchone()
            for binding in stale:
                connection.execute(
                    "DELETE FROM bindings WHERE group_id = ? AND qq_id = ?",
                    (group_id, binding.qq_id),
                )
                connection.execute(
                    "DELETE FROM match_tracking WHERE group_id = ? AND steam_id = ?",
                    (group_id, binding.steam_id),
                )
                connection.execute(
                    "DELETE FROM match_source_tracking "
                    "WHERE group_id = ? AND steam_id = ?",
                    (group_id, binding.steam_id),
                )
                connection.execute(
                    "DELETE FROM match_notifications WHERE group_id = ? AND steam_id = ?",
                    (group_id, binding.steam_id),
                )
                connection.execute(
                    "DELETE FROM match_source_notifications "
                    "WHERE group_id = ? AND steam_id = ?",
                    (group_id, binding.steam_id),
                )
                if has_presence_table:
                    connection.execute(
                        "DELETE FROM steam_presence_states "
                        "WHERE group_id = ? AND steam_id = ?",
                        (group_id, binding.steam_id),
                    )
                if has_watch_stage_table:
                    connection.execute(
                        "DELETE FROM watch_stage_states "
                        "WHERE group_id = ? AND steam_id = ?",
                        (group_id, binding.steam_id),
                    )
            memory_rows = connection.execute(
                "SELECT qq_id FROM group_member_memory WHERE group_id = ?",
                (group_id,),
            ).fetchall()
            for row in memory_rows:
                if str(row["qq_id"]) not in normalized_ids:
                    connection.execute(
                        """
                        DELETE FROM group_member_memory
                        WHERE group_id = ? AND qq_id = ?
                        """,
                        (group_id, str(row["qq_id"])),
                    )
            ai_memory_author_rows = connection.execute(
                """
                SELECT DISTINCT created_by_qq FROM group_ai_memories
                WHERE group_id = ?
                """,
                (group_id,),
            ).fetchall()
            for row in ai_memory_author_rows:
                if str(row["created_by_qq"]) not in normalized_ids:
                    connection.execute(
                        """
                        DELETE FROM group_ai_memories
                        WHERE group_id = ? AND created_by_qq = ?
                        """,
                        (group_id, str(row["created_by_qq"])),
                    )
            compliance_rows = connection.execute(
                "SELECT qq_id FROM group_member_compliance WHERE group_id = ?",
                (group_id,),
            ).fetchall()
            for row in compliance_rows:
                if str(row["qq_id"]) not in normalized_ids:
                    connection.execute(
                        """
                        DELETE FROM group_member_compliance
                        WHERE group_id = ? AND qq_id = ?
                        """,
                        (group_id, str(row["qq_id"])),
                    )
            preference_rows = connection.execute(
                "SELECT qq_id FROM match_commentary_preferences WHERE group_id = ?",
                (group_id,),
            ).fetchall()
            for row in preference_rows:
                if str(row["qq_id"]) not in normalized_ids:
                    connection.execute(
                        """
                        DELETE FROM match_commentary_preferences
                        WHERE group_id = ? AND qq_id = ?
                        """,
                        (group_id, str(row["qq_id"])),
                    )
            party_rows = connection.execute(
                """
                SELECT members.party_id, members.qq_id
                FROM game_party_members AS members
                JOIN game_parties AS parties
                  ON parties.party_id = members.party_id
                WHERE parties.group_id = ?
                """,
                (group_id,),
            ).fetchall()
            for row in party_rows:
                if str(row["qq_id"]) not in normalized_ids:
                    connection.execute(
                        """
                        DELETE FROM game_party_members
                        WHERE party_id = ? AND qq_id = ?
                        """,
                        (int(row["party_id"]), str(row["qq_id"])),
                    )
            parties = connection.execute(
                "SELECT party_id, owner_qq FROM game_parties WHERE group_id = ?",
                (group_id,),
            ).fetchall()
            for party in parties:
                party_id = int(party["party_id"])
                next_member = connection.execute(
                    """
                    SELECT qq_id FROM game_party_members
                    WHERE party_id = ?
                    ORDER BY joined_at, CAST(qq_id AS INTEGER) LIMIT 1
                    """,
                    (party_id,),
                ).fetchone()
                if next_member is None:
                    connection.execute(
                        "DELETE FROM game_parties WHERE party_id = ?", (party_id,)
                    )
                elif str(party["owner_qq"]) not in normalized_ids:
                    connection.execute(
                        """
                        UPDATE game_parties SET owner_qq = ?, updated_at = ?
                        WHERE party_id = ?
                        """,
                        (
                            str(next_member["qq_id"]),
                            datetime.now(UTC).isoformat(timespec="seconds"),
                            party_id,
                        ),
                    )
        return stale

    def initialize_match_tracking(
        self,
        group_id: str,
        steam_id: str,
        existing_match_ids: list[str] | tuple[str, ...],
    ) -> bool:
        """Create a baseline once and mark its existing matches without notifying.

        Returns ``True`` only when this call created the baseline.
        """
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO match_tracking
                    (group_id, steam_id, initialized_at)
                VALUES (?, ?, ?)
                """,
                (group_id, steam_id, now),
            )
            if cursor.rowcount == 0:
                return False
            connection.executemany(
                """
                INSERT OR IGNORE INTO match_notifications
                    (group_id, steam_id, match_id, notified_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    (group_id, steam_id, match_id, now)
                    for match_id in existing_match_ids
                    if match_id
                ),
            )
            connection.executemany(
                """
                INSERT OR IGNORE INTO group_match_notifications
                    (group_id, match_id, notified_at)
                VALUES (?, ?, ?)
                """,
                (
                    (group_id, match_id, now)
                    for match_id in existing_match_ids
                    if match_id
                ),
            )
        return True

    def initialize_match_source_tracking(
        self,
        group_id: str,
        steam_id: str,
        data_source: int,
        existing_match_ids: list[str] | tuple[str, ...],
    ) -> bool:
        """Create an independent first-seen baseline for one match source."""
        if data_source == 3:
            return self.initialize_match_tracking(
                group_id,
                steam_id,
                existing_match_ids,
            )
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO match_source_tracking
                    (group_id, steam_id, data_source, initialized_at)
                VALUES (?, ?, ?, ?)
                """,
                (group_id, steam_id, data_source, now),
            )
            if cursor.rowcount == 0:
                return False
            connection.executemany(
                """
                INSERT OR IGNORE INTO match_source_notifications
                    (group_id, steam_id, data_source, match_id, notified_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    (group_id, steam_id, data_source, match_id, now)
                    for match_id in existing_match_ids
                    if match_id
                ),
            )
            connection.executemany(
                """
                INSERT OR IGNORE INTO group_match_source_notifications
                    (group_id, data_source, match_id, notified_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    (group_id, data_source, match_id, now)
                    for match_id in existing_match_ids
                    if match_id
                ),
            )
        return True

    def is_match_notified(self, group_id: str, steam_id: str, match_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM match_notifications
                WHERE group_id = ? AND steam_id = ? AND match_id = ?
                """,
                (group_id, steam_id, match_id),
            ).fetchone()
        return row is not None

    def is_source_match_notified(
        self,
        group_id: str,
        steam_id: str,
        data_source: int,
        match_id: str,
    ) -> bool:
        if data_source == 3:
            return self.is_match_notified(group_id, steam_id, match_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM match_source_notifications
                WHERE group_id = ? AND steam_id = ?
                    AND data_source = ? AND match_id = ?
                """,
                (group_id, steam_id, data_source, match_id),
            ).fetchone()
        return row is not None

    def match_tracking_initialized_at(self, group_id: str, steam_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT initialized_at FROM match_tracking
                WHERE group_id = ? AND steam_id = ?
                """,
                (group_id, steam_id),
            ).fetchone()
        return None if row is None else str(row["initialized_at"])

    def match_source_tracking_initialized_at(
        self,
        group_id: str,
        steam_id: str,
        data_source: int,
    ) -> str | None:
        if data_source == 3:
            return self.match_tracking_initialized_at(group_id, steam_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT initialized_at FROM match_source_tracking
                WHERE group_id = ? AND steam_id = ? AND data_source = ?
                """,
                (group_id, steam_id, data_source),
            ).fetchone()
        return None if row is None else str(row["initialized_at"])

    def is_group_match_notified(self, group_id: str, match_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM group_match_notifications
                WHERE group_id = ? AND match_id = ?
                """,
                (group_id, match_id),
            ).fetchone()
        return row is not None

    def is_group_source_match_notified(
        self,
        group_id: str,
        data_source: int,
        match_id: str,
    ) -> bool:
        if data_source == 3:
            return self.is_group_match_notified(group_id, match_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM group_match_source_notifications
                WHERE group_id = ? AND data_source = ? AND match_id = ?
                """,
                (group_id, data_source, match_id),
            ).fetchone()
        return row is not None

    def mark_match_notified(self, group_id: str, steam_id: str, match_id: str) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO match_notifications
                    (group_id, steam_id, match_id, notified_at)
                VALUES (?, ?, ?, ?)
                """,
                (group_id, steam_id, match_id, now),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO group_match_notifications
                    (group_id, match_id, notified_at)
                VALUES (?, ?, ?)
                """,
                (group_id, match_id, now),
            )

    def mark_source_match_notified(
        self,
        group_id: str,
        steam_id: str,
        data_source: int,
        match_id: str,
    ) -> None:
        if data_source == 3:
            self.mark_match_notified(group_id, steam_id, match_id)
            return
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO match_source_notifications
                    (group_id, steam_id, data_source, match_id, notified_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (group_id, steam_id, data_source, match_id, now),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO group_match_source_notifications
                    (group_id, data_source, match_id, notified_at)
                VALUES (?, ?, ?, ?)
                """,
                (group_id, data_source, match_id, now),
            )

    def record_stream_live_observation(
        self,
        platform: str,
        room_id: str,
        *,
        is_live: bool,
        session_key: str,
        snapshot: dict[str, Any],
        group_ids: tuple[str, ...] | list[str],
        confirmation_polls: int = 2,
    ) -> bool:
        """Persist one trusted observation and enqueue a confirmed live edge.

        The first successful observation establishes a baseline and never sends a
        notification.  Network and parsing failures must be filtered by the caller
        and must not reach this method.
        """
        platform = platform.strip().casefold()
        room_id = room_id.strip()
        session_key = session_key.strip()
        confirmation_polls = max(1, int(confirmation_polls))
        now = datetime.now(UTC).isoformat(timespec="seconds")
        snapshot_json = json.dumps(
            snapshot,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        normalized_groups = tuple(dict.fromkeys(str(item).strip() for item in group_ids))

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT confirmed_live, confirmed_session_key,
                       candidate_live, candidate_session_key, candidate_count
                FROM stream_live_states
                WHERE platform = ? AND room_id = ?
                """,
                (platform, room_id),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO stream_live_states
                        (platform, room_id, confirmed_live, confirmed_session_key,
                         candidate_live, candidate_session_key, candidate_count,
                         last_snapshot_json, last_success_at, updated_at)
                    VALUES (?, ?, ?, ?, NULL, '', 0, ?, ?, ?)
                    """,
                    (
                        platform,
                        room_id,
                        int(is_live),
                        session_key if is_live else "",
                        snapshot_json,
                        now,
                        now,
                    ),
                )
                return False

            confirmed_live = bool(row["confirmed_live"])
            confirmed_session = str(row["confirmed_session_key"] or "")
            session_changed = bool(
                is_live
                and confirmed_live
                and session_key
                and confirmed_session
                and session_key != confirmed_session
            )
            needs_transition = is_live != confirmed_live or session_changed
            if not needs_transition:
                connection.execute(
                    """
                    UPDATE stream_live_states
                    SET candidate_live = NULL,
                        candidate_session_key = '',
                        candidate_count = 0,
                        last_snapshot_json = ?,
                        last_success_at = ?,
                        updated_at = ?
                    WHERE platform = ? AND room_id = ?
                    """,
                    (snapshot_json, now, now, platform, room_id),
                )
                return False

            candidate_session = session_key if is_live else ""
            same_candidate = (
                row["candidate_live"] is not None
                and bool(row["candidate_live"]) == is_live
                and str(row["candidate_session_key"] or "") == candidate_session
            )
            candidate_count = int(row["candidate_count"] or 0) + 1 if same_candidate else 1
            if candidate_count < confirmation_polls:
                connection.execute(
                    """
                    UPDATE stream_live_states
                    SET candidate_live = ?,
                        candidate_session_key = ?,
                        candidate_count = ?,
                        last_snapshot_json = ?,
                        last_success_at = ?,
                        updated_at = ?
                    WHERE platform = ? AND room_id = ?
                    """,
                    (
                        int(is_live),
                        candidate_session,
                        candidate_count,
                        snapshot_json,
                        now,
                        now,
                        platform,
                        room_id,
                    ),
                )
                return False

            event_session = session_key or f"observed:{now}"
            connection.execute(
                """
                UPDATE stream_live_states
                SET confirmed_live = ?,
                    confirmed_session_key = ?,
                    candidate_live = NULL,
                    candidate_session_key = '',
                    candidate_count = 0,
                    last_snapshot_json = ?,
                    last_success_at = ?,
                    updated_at = ?
                WHERE platform = ? AND room_id = ?
                """,
                (
                    int(is_live),
                    event_session if is_live else "",
                    snapshot_json,
                    now,
                    now,
                    platform,
                    room_id,
                ),
            )
            if not is_live:
                return False
            connection.executemany(
                """
                INSERT OR IGNORE INTO stream_live_notifications
                    (group_id, platform, room_id, session_key, snapshot_json,
                     status, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    (
                        group_id,
                        platform,
                        room_id,
                        event_session,
                        snapshot_json,
                        now,
                    )
                    for group_id in normalized_groups
                    if group_id
                ),
            )
        return True

    def claim_stream_live_notifications(
        self,
        *,
        limit: int = 20,
        lease_seconds: int = 300,
    ) -> tuple[StreamLiveNotification, ...]:
        """Atomically lease due notifications, including abandoned claims."""
        now_value = datetime.now(UTC)
        now = now_value.isoformat(timespec="seconds")
        stale_before = (now_value - timedelta(seconds=max(1, lease_seconds))).isoformat(
            timespec="seconds"
        )
        claimed: list[StreamLiveNotification] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT group_id, platform, room_id, session_key,
                       snapshot_json, created_at, status, claimed_at,
                       attempt_count
                FROM stream_live_notifications
                WHERE (status = 'pending'
                       AND (next_attempt_at = '' OR next_attempt_at <= ?))
                   OR (status = 'sending'
                       AND (claimed_at IS NULL OR claimed_at < ?))
                ORDER BY created_at, group_id, platform, room_id
                LIMIT ?
                """,
                (now, stale_before, max(1, int(limit))),
            ).fetchall()
            for row in rows:
                # Validate the durable payload before changing its lease state.
                try:
                    payload = json.loads(str(row["snapshot_json"]))
                except (TypeError, ValueError):
                    connection.execute(
                        """
                        UPDATE stream_live_notifications
                        SET status = 'failed', claimed_at = NULL,
                            last_error = 'invalid durable snapshot payload'
                        WHERE group_id = ? AND platform = ? AND room_id = ?
                          AND session_key = ? AND status = ?
                          AND ((? IS NULL AND claimed_at IS NULL)
                               OR claimed_at = ?)
                        """,
                        (
                            str(row["group_id"]),
                            str(row["platform"]),
                            str(row["room_id"]),
                            str(row["session_key"]),
                            str(row["status"]),
                            row["claimed_at"],
                            row["claimed_at"],
                        ),
                    )
                    continue
                if not isinstance(payload, dict):
                    connection.execute(
                        """
                        UPDATE stream_live_notifications
                        SET status = 'failed', claimed_at = NULL,
                            last_error = 'snapshot payload is not an object'
                        WHERE group_id = ? AND platform = ? AND room_id = ?
                          AND session_key = ? AND status = ?
                          AND ((? IS NULL AND claimed_at IS NULL)
                               OR claimed_at = ?)
                        """,
                        (
                            str(row["group_id"]),
                            str(row["platform"]),
                            str(row["room_id"]),
                            str(row["session_key"]),
                            str(row["status"]),
                            row["claimed_at"],
                            row["claimed_at"],
                        ),
                    )
                    continue
                was_status = str(row["status"])
                was_claimed_at = row["claimed_at"]
                cursor = connection.execute(
                    """
                    UPDATE stream_live_notifications
                    SET status = 'sending', claimed_at = ?,
                        attempt_count = attempt_count + 1,
                        next_attempt_at = '', last_error = ''
                    WHERE group_id = ? AND platform = ? AND room_id = ?
                      AND session_key = ? AND status = ?
                      AND ((? IS NULL AND claimed_at IS NULL) OR claimed_at = ?)
                    """,
                    (
                        now,
                        str(row["group_id"]),
                        str(row["platform"]),
                        str(row["room_id"]),
                        str(row["session_key"]),
                        was_status,
                        was_claimed_at,
                        was_claimed_at,
                    ),
                )
                if cursor.rowcount != 1:
                    continue
                claimed.append(
                    StreamLiveNotification(
                        group_id=str(row["group_id"]),
                        platform=str(row["platform"]),
                        room_id=str(row["room_id"]),
                        session_key=str(row["session_key"]),
                        snapshot=payload,
                        created_at=str(row["created_at"]),
                        claimed_at=now,
                        attempt_count=int(row["attempt_count"] or 0) + 1,
                    )
                )
        return tuple(claimed)

    def mark_stream_live_notification_sent(
        self,
        notification: StreamLiveNotification,
    ) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE stream_live_notifications
                SET status = 'sent', sent_at = ?, claimed_at = NULL,
                    next_attempt_at = '', last_error = ''
                WHERE group_id = ? AND platform = ? AND room_id = ?
                  AND session_key = ? AND status = 'sending'
                  AND claimed_at = ?
                """,
                (
                    now,
                    notification.group_id,
                    notification.platform,
                    notification.room_id,
                    notification.session_key,
                    notification.claimed_at,
                ),
            )

    def retry_stream_live_notification(
        self,
        notification: StreamLiveNotification,
        error: str,
        *,
        base_delay_seconds: int = 60,
        max_delay_seconds: int = 900,
        max_attempts: int = 5,
    ) -> bool:
        """Release a failed lease with bounded exponential backoff.

        Returns ``True`` when another attempt is scheduled and ``False`` when
        the event reached the terminal failure limit (or lost its lease).
        """
        now_value = datetime.now(UTC)
        attempts = max(1, notification.attempt_count)
        terminal = attempts >= max(1, int(max_attempts))
        delay = min(
            max(0, int(max_delay_seconds)),
            max(0, int(base_delay_seconds)) * (2 ** min(attempts - 1, 10)),
        )
        next_attempt_at = (
            ""
            if terminal
            else (now_value + timedelta(seconds=delay)).isoformat(timespec="seconds")
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE stream_live_notifications
                SET status = ?, claimed_at = NULL, next_attempt_at = ?,
                    last_error = ?
                WHERE group_id = ? AND platform = ? AND room_id = ?
                  AND session_key = ? AND status = 'sending'
                  AND claimed_at = ?
                """,
                (
                    "failed" if terminal else "pending",
                    next_attempt_at,
                    str(error)[:500],
                    notification.group_id,
                    notification.platform,
                    notification.room_id,
                    notification.session_key,
                    notification.claimed_at,
                ),
            )
        return cursor.rowcount == 1 and not terminal

    def stream_live_state(self, platform: str, room_id: str) -> dict[str, Any] | None:
        """Return a safe state snapshot for diagnostics and tests."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT confirmed_live, confirmed_session_key,
                       candidate_live, candidate_session_key, candidate_count,
                       last_snapshot_json, last_success_at, updated_at
                FROM stream_live_states
                WHERE platform = ? AND room_id = ?
                """,
                (platform.strip().casefold(), room_id.strip()),
            ).fetchone()
        if row is None:
            return None
        return {
            "confirmed_live": bool(row["confirmed_live"]),
            "confirmed_session_key": str(row["confirmed_session_key"] or ""),
            "candidate_live": (
                None if row["candidate_live"] is None else bool(row["candidate_live"])
            ),
            "candidate_session_key": str(row["candidate_session_key"] or ""),
            "candidate_count": int(row["candidate_count"] or 0),
            "last_snapshot": json.loads(str(row["last_snapshot_json"])),
            "last_success_at": str(row["last_success_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def enqueue_highlight_render_job(
        self,
        group_id: str,
        match_id: str,
        steam_id: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str = "",
        priority: int = 0,
    ) -> tuple[HighlightRenderJob, bool]:
        """Persist one renderer intent and return ``(job, was_created)``.

        By default the group and the analyzer's stable ``highlight_id`` form
        the idempotency key.  Repeated ingestion of the same demo therefore
        cannot enqueue duplicate videos.
        """
        normalized_group = str(group_id).strip()
        normalized_match = str(match_id).strip()
        normalized_steam = str(steam_id).strip()
        if not normalized_group or not normalized_match or not normalized_steam:
            raise ValueError("group_id, match_id and steam_id are required")
        if not isinstance(payload, dict):
            raise TypeError("highlight payload must be a JSON object")
        highlight_id = str(payload.get("highlight_id") or "").strip()
        normalized_dedupe = str(dedupe_key).strip() or (
            f"{normalized_group}:{highlight_id}" if highlight_id else ""
        )
        if not normalized_dedupe:
            raise ValueError("dedupe_key or payload.highlight_id is required")
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        decoded = json.loads(payload_json)
        if not isinstance(decoded, dict):
            raise TypeError("highlight payload must be a JSON object")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        safe_priority = max(-1000, min(1000, int(priority)))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO highlight_render_jobs
                    (dedupe_key, group_id, match_id, steam_id, payload_json,
                     priority, status, available_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', '', ?, ?)
                """,
                (
                    normalized_dedupe,
                    normalized_group,
                    normalized_match,
                    normalized_steam,
                    payload_json,
                    safe_priority,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM highlight_render_jobs WHERE dedupe_key = ?",
                (normalized_dedupe,),
            ).fetchone()
        if row is None:  # pragma: no cover - SQLite should make this impossible
            raise RuntimeError("highlight job insert did not produce a row")
        return self._highlight_render_job_from_row(row), cursor.rowcount == 1

    def get_highlight_render_job(self, dedupe_key: str) -> HighlightRenderJob | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM highlight_render_jobs WHERE dedupe_key = ?",
                (str(dedupe_key).strip(),),
            ).fetchone()
        return None if row is None else self._highlight_render_job_from_row(row)

    def claim_highlight_render_jobs(
        self,
        worker_id: str,
        *,
        limit: int = 1,
        lease_seconds: int = 1800,
        max_attempts: int = 12,
    ) -> tuple[HighlightRenderJob, ...]:
        """Atomically lease due jobs and recover abandoned renderer leases."""
        normalized_worker = str(worker_id).strip()
        if not normalized_worker:
            raise ValueError("worker_id is required")
        now_value = datetime.now(UTC)
        now = now_value.isoformat(timespec="seconds")
        stale_before = (now_value - timedelta(seconds=max(1, lease_seconds))).isoformat(
            timespec="seconds"
        )
        attempt_limit = max(1, int(max_attempts))
        claimed: list[HighlightRenderJob] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE highlight_render_jobs
                SET status = 'failed', claimed_at = NULL, lease_token = '',
                    worker_id = '', available_at = '',
                    last_error = 'highlight controller lease expired after maximum attempts',
                    updated_at = ?
                WHERE status = 'running' AND attempt_count >= ?
                  AND (claimed_at IS NULL OR claimed_at < ?)
                """,
                (now, attempt_limit, stale_before),
            )
            connection.execute(
                """
                UPDATE highlight_render_jobs
                SET status = 'failed', available_at = '',
                    last_error = 'highlight controller maximum attempts exhausted',
                    updated_at = ?
                WHERE status = 'pending' AND attempt_count >= ?
                """,
                (now, attempt_limit),
            )
            rows = connection.execute(
                """
                SELECT * FROM highlight_render_jobs
                WHERE attempt_count < ? AND (
                    (status = 'pending'
                     AND (available_at = '' OR available_at <= ?))
                    OR (status = 'running'
                        AND (claimed_at IS NULL OR claimed_at < ?))
                )
                ORDER BY priority DESC, created_at, job_id
                LIMIT ?
                """,
                (attempt_limit, now, stale_before, max(1, min(int(limit), 100))),
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(str(row["payload_json"]))
                except (TypeError, ValueError):
                    payload = None
                if not isinstance(payload, dict):
                    connection.execute(
                        """
                        UPDATE highlight_render_jobs
                        SET status = 'failed', claimed_at = NULL,
                            lease_token = '', worker_id = '',
                            last_error = 'invalid durable highlight payload',
                            updated_at = ?
                        WHERE job_id = ?
                        """,
                        (now, int(row["job_id"])),
                    )
                    continue
                previous_status = str(row["status"])
                previous_token = str(row["lease_token"] or "")
                lease_token = uuid4().hex
                cursor = connection.execute(
                    """
                    UPDATE highlight_render_jobs
                    SET status = 'running', available_at = '', claimed_at = ?,
                        lease_token = ?, worker_id = ?,
                        attempt_count = attempt_count + 1,
                        last_error = '', updated_at = ?
                    WHERE job_id = ? AND status = ? AND lease_token = ?
                    """,
                    (
                        now,
                        lease_token,
                        normalized_worker,
                        now,
                        int(row["job_id"]),
                        previous_status,
                        previous_token,
                    ),
                )
                if cursor.rowcount != 1:
                    continue
                updated = connection.execute(
                    "SELECT * FROM highlight_render_jobs WHERE job_id = ?",
                    (int(row["job_id"]),),
                ).fetchone()
                if updated is not None:
                    claimed.append(self._highlight_render_job_from_row(updated))
        return tuple(claimed)

    def renew_highlight_render_job_lease(self, job: HighlightRenderJob) -> bool:
        """Heartbeat an active long-running render without changing its token."""
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE highlight_render_jobs
                SET claimed_at = ?, updated_at = ?
                WHERE job_id = ? AND status = 'running'
                  AND lease_token = ? AND worker_id = ?
                """,
                (now, now, job.job_id, job.lease_token, job.worker_id),
            )
        return cursor.rowcount == 1

    def complete_highlight_render_job(
        self,
        job: HighlightRenderJob,
        output: dict[str, Any],
    ) -> bool:
        if not isinstance(output, dict):
            raise TypeError("render output must be a JSON object")
        output_json = json.dumps(
            output,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE highlight_render_jobs
                SET status = 'completed', output_json = ?, completed_at = ?,
                    claimed_at = NULL, lease_token = '', worker_id = '',
                    available_at = '', last_error = '', updated_at = ?
                WHERE job_id = ? AND status = 'running'
                  AND lease_token = ? AND worker_id = ?
                """,
                (
                    output_json,
                    now,
                    now,
                    job.job_id,
                    job.lease_token,
                    job.worker_id,
                ),
            )
        return cursor.rowcount == 1

    def retry_highlight_render_job(
        self,
        job: HighlightRenderJob,
        error: str,
        *,
        base_delay_seconds: int = 60,
        max_delay_seconds: int = 3600,
        max_attempts: int = 3,
    ) -> bool:
        """Release a render lease with bounded exponential backoff."""
        now_value = datetime.now(UTC)
        attempts = max(1, int(job.attempt_count))
        terminal = attempts >= max(1, int(max_attempts))
        delay = min(
            max(0, int(max_delay_seconds)),
            max(0, int(base_delay_seconds)) * (2 ** min(attempts - 1, 10)),
        )
        available_at = (
            ""
            if terminal
            else (now_value + timedelta(seconds=delay)).isoformat(timespec="seconds")
        )
        now = now_value.isoformat(timespec="seconds")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE highlight_render_jobs
                SET status = ?, available_at = ?, claimed_at = NULL,
                    lease_token = '', worker_id = '', last_error = ?, updated_at = ?
                WHERE job_id = ? AND status = 'running'
                  AND lease_token = ? AND worker_id = ?
                """,
                (
                    "failed" if terminal else "pending",
                    available_at,
                    str(error)[:1000],
                    now,
                    job.job_id,
                    job.lease_token,
                    job.worker_id,
                ),
            )
        return cursor.rowcount == 1 and not terminal

    @staticmethod
    def _highlight_render_job_from_row(row: sqlite3.Row) -> HighlightRenderJob:
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise ValueError("highlight payload is not an object")
        raw_output = row["output_json"]
        output = None if raw_output is None else json.loads(str(raw_output))
        if output is not None and not isinstance(output, dict):
            raise ValueError("highlight output is not an object")
        return HighlightRenderJob(
            job_id=int(row["job_id"]),
            dedupe_key=str(row["dedupe_key"]),
            group_id=str(row["group_id"]),
            match_id=str(row["match_id"]),
            steam_id=str(row["steam_id"]),
            payload=payload,
            priority=int(row["priority"]),
            status=str(row["status"]),
            created_at=str(row["created_at"]),
            available_at=str(row["available_at"] or ""),
            claimed_at=str(row["claimed_at"] or ""),
            lease_token=str(row["lease_token"] or ""),
            worker_id=str(row["worker_id"] or ""),
            attempt_count=int(row["attempt_count"] or 0),
            output=output,
            last_error=str(row["last_error"] or ""),
            completed_at=str(row["completed_at"] or ""),
        )

    def is_daily_summary_processed(self, group_id: str, summary_date: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM daily_summary_runs
                WHERE group_id = ? AND summary_date = ?
                """,
                (group_id, summary_date),
            ).fetchone()
        return row is not None

    def mark_daily_summary_processed(
        self,
        group_id: str,
        summary_date: str,
        *,
        was_sent: bool,
    ) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO daily_summary_runs
                    (group_id, summary_date, was_sent, completed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(group_id, summary_date) DO UPDATE SET
                    was_sent = excluded.was_sent,
                    completed_at = excluded.completed_at
                """,
                (group_id, summary_date, int(was_sent), now),
            )

    def is_group_level_reminder_processed(
        self,
        group_id: str,
        reminder_date: str,
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM group_level_reminder_runs
                WHERE group_id = ? AND reminder_date = ?
                """,
                (group_id, reminder_date),
            ).fetchone()
        return row is not None

    def mark_group_level_reminder_processed(
        self,
        group_id: str,
        reminder_date: str,
        *,
        target_count: int,
    ) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO group_level_reminder_runs
                    (group_id, reminder_date, target_count, completed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(group_id, reminder_date) DO UPDATE SET
                    target_count = excluded.target_count,
                    completed_at = excluded.completed_at
                """,
                (group_id, reminder_date, target_count, now),
            )

    def reconcile_group_member_compliance(
        self,
        group_id: str,
        noncompliant_members: dict[str, tuple[str, ...]],
        *,
        check_date: str,
    ) -> tuple[GroupMemberCompliance, ...]:
        """Persist current violations and reset members who became compliant."""
        normalized = {
            str(qq_id): tuple(sorted({str(reason) for reason in reasons if reason}))
            for qq_id, reasons in noncompliant_members.items()
            if str(qq_id).isdecimal() and reasons
        }
        with self._connect() as connection:
            existing_rows = connection.execute(
                """
                SELECT qq_id FROM group_member_compliance
                WHERE group_id = ?
                """,
                (group_id,),
            ).fetchall()
            for row in existing_rows:
                qq_id = str(row["qq_id"])
                if qq_id not in normalized:
                    connection.execute(
                        """
                        DELETE FROM group_member_compliance
                        WHERE group_id = ? AND qq_id = ?
                        """,
                        (group_id, qq_id),
                    )
            for qq_id, reasons in normalized.items():
                connection.execute(
                    """
                    INSERT INTO group_member_compliance
                        (group_id, qq_id, first_noncompliant_date,
                         last_checked_date, reasons_json)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(group_id, qq_id) DO UPDATE SET
                        first_noncompliant_date = CASE
                            WHEN julianday(excluded.last_checked_date)
                               - julianday(group_member_compliance.last_checked_date) > 1
                            THEN excluded.first_noncompliant_date
                            ELSE group_member_compliance.first_noncompliant_date
                        END,
                        last_checked_date = excluded.last_checked_date,
                        reasons_json = excluded.reasons_json
                    """,
                    (group_id, qq_id, check_date, check_date, json.dumps(reasons)),
                )
            rows = connection.execute(
                """
                SELECT * FROM group_member_compliance
                WHERE group_id = ?
                ORDER BY CAST(qq_id AS INTEGER)
                """,
                (group_id,),
            ).fetchall()
            connection.execute(
                """
                DELETE FROM group_member_compliance
                WHERE last_checked_date < date(?, '-45 days')
                """,
                (check_date,),
            )
        return tuple(self._compliance_from_row(row) for row in rows)

    def clear_group_member_compliance(self, group_id: str, qq_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM group_member_compliance
                WHERE group_id = ? AND qq_id = ?
                """,
                (group_id, qq_id),
            )

    def get_group_member_compliance(
        self,
        group_id: str,
        qq_id: str,
    ) -> GroupMemberCompliance | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM group_member_compliance
                WHERE group_id = ? AND qq_id = ?
                """,
                (group_id, qq_id),
            ).fetchone()
        return self._compliance_from_row(row) if row is not None else None

    def list_group_member_compliance_groups(self) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT group_id FROM group_member_compliance
                ORDER BY group_id
                """
            ).fetchall()
        return tuple(str(row["group_id"]) for row in rows)

    def increment_unbound_message_count(
        self,
        group_id: str,
        qq_id: str,
        usage_date: str,
    ) -> int:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM unbound_message_usage WHERE usage_date < ?",
                (usage_date,),
            )
            connection.execute(
                """
                INSERT INTO unbound_message_usage
                    (group_id, qq_id, usage_date, message_count, updated_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(group_id, qq_id, usage_date) DO UPDATE SET
                    message_count = unbound_message_usage.message_count + 1,
                    updated_at = excluded.updated_at
                """,
                (group_id, qq_id, usage_date, now),
            )
            row = connection.execute(
                """
                SELECT message_count FROM unbound_message_usage
                WHERE group_id = ? AND qq_id = ? AND usage_date = ?
                """,
                (group_id, qq_id, usage_date),
            ).fetchone()
        assert row is not None
        return int(row["message_count"])

    def get_unbound_message_count(
        self,
        group_id: str,
        qq_id: str,
        usage_date: str,
    ) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT message_count FROM unbound_message_usage
                WHERE group_id = ? AND qq_id = ? AND usage_date = ?
                """,
                (group_id, qq_id, usage_date),
            ).fetchone()
        return int(row["message_count"]) if row is not None else 0

    def clear_unbound_message_usage(self, group_id: str, qq_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM unbound_message_usage WHERE group_id = ? AND qq_id = ?",
                (group_id, qq_id),
            )

    def is_content_relay_sent(self, target_group_id: str, content_hash: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM content_relay_deliveries
                WHERE target_group_id = ? AND content_hash = ?
                """,
                (target_group_id, content_hash),
            ).fetchone()
        return row is not None

    def count_content_relays(self, target_group_id: str, sent_date: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS relay_count FROM content_relay_deliveries
                WHERE target_group_id = ? AND sent_date = ?
                """,
                (target_group_id, sent_date),
            ).fetchone()
        assert row is not None
        return int(row["relay_count"])

    def count_content_relays_from_source(
        self,
        target_group_id: str,
        source_group_id: str,
        sent_date: str,
    ) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS relay_count FROM content_relay_deliveries
                WHERE target_group_id = ? AND source_group_id = ? AND sent_date = ?
                """,
                (target_group_id, source_group_id, sent_date),
            ).fetchone()
        assert row is not None
        return int(row["relay_count"])

    def last_content_relay_sent_at(self, target_group_id: str) -> datetime | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT sent_at FROM content_relay_deliveries
                WHERE target_group_id = ?
                ORDER BY sent_at DESC
                LIMIT 1
                """,
                (target_group_id,),
            ).fetchone()
        if row is None:
            return None
        parsed = datetime.fromisoformat(str(row["sent_at"]))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    def mark_content_relay_sent(
        self,
        *,
        target_group_id: str,
        source_group_id: str,
        content_hash: str,
        sent_date: str,
        sent_at: datetime,
    ) -> None:
        timestamp = sent_at.astimezone(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO content_relay_deliveries
                    (target_group_id, source_group_id, content_hash, sent_date, sent_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    target_group_id,
                    source_group_id,
                    content_hash,
                    sent_date,
                    timestamp,
                ),
            )

    def add_skin_watch(
        self,
        group_id: str,
        market_hash_name: str,
        added_by_qq: str,
        *,
        max_items: int,
    ) -> tuple[SkinWatchItem, bool]:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT * FROM skin_price_watchlist
                WHERE group_id = ? AND market_hash_name = ? COLLATE NOCASE
                """,
                (group_id, market_hash_name),
            ).fetchone()
            if existing is not None:
                return self._skin_watch_from_row(existing), False
            count = connection.execute(
                "SELECT COUNT(*) AS item_count FROM skin_price_watchlist WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            assert count is not None
            if int(count["item_count"]) >= max_items:
                raise ValueError(f"本群最多关注 {max_items} 件饰品")
            cursor = connection.execute(
                """
                INSERT INTO skin_price_watchlist
                    (group_id, market_hash_name, added_by_qq, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (group_id, market_hash_name, added_by_qq, now),
            )
            item_id = int(cursor.lastrowid)
        return SkinWatchItem(item_id, group_id, market_hash_name, added_by_qq, now), True

    def list_skin_watches(self, group_id: str) -> list[SkinWatchItem]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM skin_price_watchlist
                WHERE group_id = ?
                ORDER BY item_id ASC
                """,
                (group_id,),
            ).fetchall()
        return [self._skin_watch_from_row(row) for row in rows]

    def list_skin_watch_groups(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT group_id FROM skin_price_watchlist ORDER BY group_id"
            ).fetchall()
        return [str(row["group_id"]) for row in rows]

    def delete_skin_watch(self, group_id: str, item_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM skin_price_watchlist WHERE group_id = ? AND item_id = ?",
                (group_id, item_id),
            )
        return cursor.rowcount > 0

    def save_skin_price_snapshot(
        self,
        *,
        group_id: str,
        market_hash_name: str,
        snapshot_date: str,
        sell_price_cny: float,
        buy_price_cny: float | None,
        source_price_usd: float | None,
        usd_cny_rate: float,
    ) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO skin_price_snapshots
                    (group_id, market_hash_name, snapshot_date, sell_price_cny,
                     buy_price_cny, source_price_usd, usd_cny_rate, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_id, market_hash_name, snapshot_date) DO UPDATE SET
                    sell_price_cny = excluded.sell_price_cny,
                    buy_price_cny = excluded.buy_price_cny,
                    source_price_usd = excluded.source_price_usd,
                    usd_cny_rate = excluded.usd_cny_rate,
                    fetched_at = excluded.fetched_at
                """,
                (
                    group_id,
                    market_hash_name,
                    snapshot_date,
                    sell_price_cny,
                    buy_price_cny,
                    source_price_usd,
                    usd_cny_rate,
                    now,
                ),
            )

    def latest_skin_sell_price(self, group_id: str, market_hash_name: str) -> float | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT sell_price_cny FROM skin_price_snapshots
                WHERE group_id = ? AND market_hash_name = ? COLLATE NOCASE
                ORDER BY snapshot_date DESC
                LIMIT 1
                """,
                (group_id, market_hash_name),
            ).fetchone()
        return None if row is None else float(row["sell_price_cny"])

    def skin_sell_price_before(
        self,
        group_id: str,
        market_hash_name: str,
        snapshot_date: str,
    ) -> float | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT sell_price_cny FROM skin_price_snapshots
                WHERE group_id = ? AND market_hash_name = ? COLLATE NOCASE
                  AND snapshot_date < ?
                ORDER BY snapshot_date DESC
                LIMIT 1
                """,
                (group_id, market_hash_name, snapshot_date),
            ).fetchone()
        return None if row is None else float(row["sell_price_cny"])

    def is_skin_price_report_processed(self, group_id: str, report_date: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM skin_price_report_runs
                WHERE group_id = ? AND report_date = ?
                """,
                (group_id, report_date),
            ).fetchone()
        return row is not None

    def mark_skin_price_report_processed(
        self,
        group_id: str,
        report_date: str,
        *,
        was_sent: bool,
        retention_days: int,
    ) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO skin_price_report_runs
                    (group_id, report_date, was_sent, completed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(group_id, report_date) DO UPDATE SET
                    was_sent = excluded.was_sent,
                    completed_at = excluded.completed_at
                """,
                (group_id, report_date, int(was_sent), now),
            )
            connection.execute(
                """
                DELETE FROM skin_price_snapshots
                WHERE snapshot_date < date(?, ?)
                """,
                (report_date, f"-{retention_days} days"),
            )
            connection.execute(
                """
                DELETE FROM skin_price_report_runs
                WHERE report_date < date(?, ?)
                """,
                (report_date, f"-{retention_days} days"),
            )

    def delete_parties_updated_before(self, cutoff: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM game_parties WHERE updated_at < ?",
                (cutoff,),
            )
        return cursor.rowcount

    def get_party(self, group_id: str) -> GameParty | None:
        parties = self.list_parties(group_id)
        return parties[0] if parties else None

    def get_party_by_id(self, group_id: str, party_id: int) -> GameParty | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM game_parties WHERE group_id = ? AND party_id = ?",
                (group_id, party_id),
            ).fetchone()
            if row is None:
                return None
            member_rows = connection.execute(
                """
                SELECT qq_id, nickname, joined_at FROM game_party_members
                WHERE party_id = ?
                ORDER BY joined_at, CAST(qq_id AS INTEGER)
                """,
                (party_id,),
            ).fetchall()
        return self._party_from_rows(row, member_rows)

    def list_parties(self, group_id: str) -> tuple[GameParty, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM game_parties
                WHERE group_id = ?
                ORDER BY created_at, party_id
                """,
                (group_id,),
            ).fetchall()
            parties: list[GameParty] = []
            for row in rows:
                member_rows = connection.execute(
                    """
                    SELECT qq_id, nickname, joined_at FROM game_party_members
                    WHERE party_id = ?
                    ORDER BY joined_at, CAST(qq_id AS INTEGER)
                    """,
                    (int(row["party_id"]),),
                ).fetchall()
                parties.append(self._party_from_rows(row, member_rows))
        return tuple(parties)

    def create_party(
        self,
        group_id: str,
        owner_qq: str,
        owner_nickname: str,
        *,
        name: str = "开黑车队",
        capacity: int,
        scheduled_for: str,
    ) -> GameParty:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        nickname = " ".join(owner_nickname.split()).strip()[:80] or f"QQ {owner_qq}"
        normalized_name = " ".join(name.split()).strip()[:40] or "开黑车队"
        with self._connect() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO game_parties
                        (group_id, name, owner_qq, capacity, scheduled_for,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        group_id,
                        normalized_name,
                        owner_qq,
                        capacity,
                        scheduled_for,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"本群已经有一辆名为“{normalized_name}”的车") from exc
            party_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO game_party_members (party_id, qq_id, nickname, joined_at)
                VALUES (?, ?, ?, ?)
                """,
                (party_id, owner_qq, nickname, now),
            )
        party = self.get_party_by_id(group_id, party_id)
        assert party is not None
        return party

    def join_party(
        self,
        group_id: str,
        qq_id: str,
        nickname: str,
        *,
        party_id: int | None = None,
    ) -> tuple[GameParty, bool]:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        display_name = " ".join(nickname.split()).strip()[:80] or f"QQ {qq_id}"
        with self._connect() as connection:
            party_rows = connection.execute(
                """
                SELECT party_id, capacity FROM game_parties
                WHERE group_id = ?
                ORDER BY created_at, party_id
                """,
                (group_id,),
            ).fetchall()
            if not party_rows:
                raise ValueError("本群还没有车队，请先发送 #开黑")
            if party_id is None:
                if len(party_rows) != 1:
                    raise ValueError("本群有多辆车，请在 #上车 后填写车队编号或车名")
                party = party_rows[0]
                party_id = int(party["party_id"])
            else:
                party = next(
                    (row for row in party_rows if int(row["party_id"]) == party_id),
                    None,
                )
                if party is None:
                    raise ValueError("没有找到这辆车")
            existing = connection.execute(
                """
                SELECT 1 FROM game_party_members
                WHERE party_id = ? AND qq_id = ?
                """,
                (party_id, qq_id),
            ).fetchone()
            if existing is not None:
                connection.execute(
                    """
                    UPDATE game_party_members SET nickname = ?
                    WHERE party_id = ? AND qq_id = ?
                    """,
                    (display_name, party_id, qq_id),
                )
                joined = False
            else:
                count = connection.execute(
                    "SELECT COUNT(*) AS n FROM game_party_members WHERE party_id = ?",
                    (party_id,),
                ).fetchone()
                assert count is not None
                if int(count["n"]) >= int(party["capacity"]):
                    raise ValueError("车已经满员了")
                connection.execute(
                    """
                    INSERT INTO game_party_members
                        (party_id, qq_id, nickname, joined_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (party_id, qq_id, display_name, now),
                )
                connection.execute(
                    "UPDATE game_parties SET updated_at = ? WHERE party_id = ?",
                    (now, party_id),
                )
                joined = True
        result = self.get_party_by_id(group_id, party_id)
        assert result is not None
        return result, joined

    def leave_party(
        self,
        group_id: str,
        qq_id: str,
        *,
        party_id: int | None = None,
    ) -> GameParty | None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            memberships = connection.execute(
                """
                SELECT parties.party_id, parties.owner_qq
                FROM game_parties AS parties
                JOIN game_party_members AS members
                  ON members.party_id = parties.party_id
                WHERE parties.group_id = ? AND members.qq_id = ?
                ORDER BY parties.created_at, parties.party_id
                """,
                (group_id, qq_id),
            ).fetchall()
            if not memberships:
                if self.get_party(group_id) is None:
                    raise ValueError("本群当前没有车队")
                raise ValueError("你还没有上车")
            if party_id is None:
                if len(memberships) != 1:
                    raise ValueError("你在多辆车上，请在 #下车 后填写车队编号或车名")
                party = memberships[0]
                party_id = int(party["party_id"])
            else:
                party = next(
                    (row for row in memberships if int(row["party_id"]) == party_id),
                    None,
                )
                if party is None:
                    raise ValueError("你不在这辆车上")
            cursor = connection.execute(
                "DELETE FROM game_party_members WHERE party_id = ? AND qq_id = ?",
                (party_id, qq_id),
            )
            if cursor.rowcount == 0:
                raise ValueError("你还没有上车")
            next_member = connection.execute(
                """
                SELECT qq_id FROM game_party_members
                WHERE party_id = ?
                ORDER BY joined_at, CAST(qq_id AS INTEGER) LIMIT 1
                """,
                (party_id,),
            ).fetchone()
            if next_member is None:
                connection.execute(
                    "DELETE FROM game_parties WHERE party_id = ?", (party_id,)
                )
                return None
            new_owner = (
                str(next_member["qq_id"])
                if str(party["owner_qq"]) == qq_id
                else str(party["owner_qq"])
            )
            connection.execute(
                """
                UPDATE game_parties SET owner_qq = ?, updated_at = ?
                WHERE party_id = ?
                """,
                (new_owner, now, party_id),
            )
        return self.get_party_by_id(group_id, party_id)

    def remove_member_from_all_parties(self, group_id: str, qq_id: str) -> int:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            memberships = connection.execute(
                """
                SELECT parties.party_id, parties.owner_qq
                FROM game_parties AS parties
                JOIN game_party_members AS members
                  ON members.party_id = parties.party_id
                WHERE parties.group_id = ? AND members.qq_id = ?
                """,
                (group_id, qq_id),
            ).fetchall()
            for party in memberships:
                party_id = int(party["party_id"])
                connection.execute(
                    "DELETE FROM game_party_members WHERE party_id = ? AND qq_id = ?",
                    (party_id, qq_id),
                )
                next_member = connection.execute(
                    """
                    SELECT qq_id FROM game_party_members
                    WHERE party_id = ?
                    ORDER BY joined_at, CAST(qq_id AS INTEGER) LIMIT 1
                    """,
                    (party_id,),
                ).fetchone()
                if next_member is None:
                    connection.execute(
                        "DELETE FROM game_parties WHERE party_id = ?", (party_id,)
                    )
                elif str(party["owner_qq"]) == qq_id:
                    connection.execute(
                        """
                        UPDATE game_parties SET owner_qq = ?, updated_at = ?
                        WHERE party_id = ?
                        """,
                        (str(next_member["qq_id"]), now, party_id),
                    )
        return len(memberships)

    def disband_party(
        self,
        group_id: str,
        requester_qq: str,
        *,
        party_id: int | None = None,
    ) -> bool:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT party_id, owner_qq FROM game_parties
                WHERE group_id = ?
                ORDER BY created_at, party_id
                """,
                (group_id,),
            ).fetchall()
            if not rows:
                raise ValueError("本群当前没有车队")
            if party_id is None:
                owned = [
                    row for row in rows if str(row["owner_qq"]) == requester_qq
                ]
                if not owned:
                    raise ValueError("只有车主可以解散车队")
                if len(owned) != 1:
                    raise ValueError("你创建了多辆车，请在 #散车 后填写编号或车名")
                row = owned[0]
                party_id = int(row["party_id"])
            else:
                row = next(
                    (item for item in rows if int(item["party_id"]) == party_id),
                    None,
                )
                if row is None:
                    raise ValueError("没有找到这辆车")
            if str(row["owner_qq"]) != requester_qq:
                raise ValueError("只有车主可以解散车队")
            cursor = connection.execute(
                "DELETE FROM game_parties WHERE party_id = ?", (party_id,)
            )
        return cursor.rowcount > 0

    def set_match_commentary_enabled(
        self,
        group_id: str,
        qq_id: str,
        *,
        enabled: bool,
    ) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO match_commentary_preferences
                    (group_id, qq_id, enabled, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(group_id, qq_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (group_id, qq_id, int(enabled), now),
            )

    def is_match_commentary_enabled(self, group_id: str, qq_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT enabled FROM match_commentary_preferences
                WHERE group_id = ? AND qq_id = ?
                """,
                (group_id, qq_id),
            ).fetchone()
        return row is None or bool(row["enabled"])

    def clear_match_commentary_preference(self, group_id: str, qq_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM match_commentary_preferences
                WHERE group_id = ? AND qq_id = ?
                """,
                (group_id, qq_id),
            )

    def is_weekly_chemistry_processed(self, group_id: str, week_start: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM weekly_chemistry_runs
                WHERE group_id = ? AND week_start = ?
                """,
                (group_id, week_start),
            ).fetchone()
        return row is not None

    def mark_weekly_chemistry_processed(
        self,
        group_id: str,
        week_start: str,
        *,
        was_sent: bool,
    ) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO weekly_chemistry_runs
                    (group_id, week_start, was_sent, completed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(group_id, week_start) DO UPDATE SET
                    was_sent = excluded.was_sent,
                    completed_at = excluded.completed_at
                """,
                (group_id, week_start, int(was_sent), now),
            )
            connection.execute(
                """
                DELETE FROM weekly_chemistry_runs
                WHERE week_start < date(?, '-84 days')
                """,
                (week_start,),
            )

    def unbind(self, group_id: str, qq_id: str) -> bool:
        with self._connect() as connection:
            binding = connection.execute(
                "SELECT steam_id FROM bindings WHERE group_id = ? AND qq_id = ?",
                (group_id, qq_id),
            ).fetchone()
            cursor = connection.execute(
                "DELETE FROM bindings WHERE group_id = ? AND qq_id = ?",
                (group_id, qq_id),
            )
            if binding is not None:
                connection.execute(
                    "DELETE FROM match_tracking WHERE group_id = ? AND steam_id = ?",
                    (group_id, binding["steam_id"]),
                )
                connection.execute(
                    "DELETE FROM match_source_tracking "
                    "WHERE group_id = ? AND steam_id = ?",
                    (group_id, binding["steam_id"]),
                )
                connection.execute(
                    "DELETE FROM match_notifications WHERE group_id = ? AND steam_id = ?",
                    (group_id, binding["steam_id"]),
                )
                connection.execute(
                    "DELETE FROM match_source_notifications "
                    "WHERE group_id = ? AND steam_id = ?",
                    (group_id, binding["steam_id"]),
                )
                connection.execute(
                    """
                    UPDATE group_member_memory
                    SET stats_json = '{}', stats_updated_at = NULL
                    WHERE group_id = ? AND qq_id = ?
                    """,
                    (group_id, qq_id),
                )
                if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'steam_presence_states'"
                ).fetchone():
                    connection.execute(
                        "DELETE FROM steam_presence_states "
                        "WHERE group_id = ? AND steam_id = ?",
                        (group_id, str(binding["steam_id"])),
                    )
                if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'watch_stage_states'"
                ).fetchone():
                    connection.execute(
                        "DELETE FROM watch_stage_states "
                        "WHERE group_id = ? AND steam_id = ?",
                        (group_id, str(binding["steam_id"])),
                    )
        return cursor.rowcount > 0

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Binding:
        return Binding(
            group_id=row["group_id"],
            qq_id=row["qq_id"],
            steam_id=row["steam_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _valorant_binding_from_row(row: sqlite3.Row) -> ValorantBinding:
        return ValorantBinding(
            group_id=str(row["group_id"]),
            qq_id=str(row["qq_id"]),
            riot_id=str(row["riot_id"]),
            riot_id_key=str(row["riot_id_key"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _skin_watch_from_row(row: sqlite3.Row) -> SkinWatchItem:
        return SkinWatchItem(
            item_id=int(row["item_id"]),
            group_id=str(row["group_id"]),
            market_hash_name=str(row["market_hash_name"]),
            added_by_qq=str(row["added_by_qq"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _party_from_rows(
        row: sqlite3.Row,
        member_rows: list[sqlite3.Row],
    ) -> GameParty:
        return GameParty(
            party_id=int(row["party_id"]),
            group_id=str(row["group_id"]),
            name=str(row["name"]),
            owner_qq=str(row["owner_qq"]),
            capacity=int(row["capacity"]),
            scheduled_for=str(row["scheduled_for"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            members=tuple(
                PartyMember(
                    qq_id=str(member["qq_id"]),
                    nickname=str(member["nickname"]),
                    joined_at=str(member["joined_at"]),
                )
                for member in member_rows
            ),
        )

    @staticmethod
    def _compliance_from_row(row: sqlite3.Row) -> GroupMemberCompliance:
        try:
            raw_reasons = json.loads(str(row["reasons_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_reasons = []
        reasons = (
            tuple(str(reason) for reason in raw_reasons if str(reason))
            if isinstance(raw_reasons, list)
            else ()
        )
        return GroupMemberCompliance(
            group_id=str(row["group_id"]),
            qq_id=str(row["qq_id"]),
            first_noncompliant_date=str(row["first_noncompliant_date"]),
            last_checked_date=str(row["last_checked_date"]),
            reasons=reasons,
        )
