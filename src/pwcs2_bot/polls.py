"""On-demand group polls, isolated from predictions and the coin economy."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

LETTERS = "ABCDEF"
CHINA_TIMEZONE = timezone(timedelta(hours=8))
POLL_HELP = (
    "🗳 群内投票（不消耗牛币）\n"
    "/投票 发起 今晚玩什么？ | CS2 | 瓦 | 休息\n"
    "默认30分钟；可写：/投票 发起 60分钟 问题 | 选项1 | 选项2\n"
    "/投票 编号 A　参与或改票\n"
    "/投票 结果 编号　查看票数\n"
    "/投票 结束 编号　发起者或管理员结束\n"
    "每人一票；到期自动停止收票，不主动刷屏。"
)


class PollError(ValueError):
    """A safe, user-facing poll error."""


class PollService:
    """A small SQLite service; ``clock`` must return a timezone-aware datetime.

    Every action is serialized with SQLite, including limits and vote updates.
    A nonempty request ID deduplicates successful actions across process restarts.
    Expiry is evaluated on access, so no background tasks are needed.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS community_polls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    question TEXT NOT NULL,
                    options_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    ended_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_community_polls_active
                    ON community_polls(group_id, ended_at, expires_at);
                CREATE TABLE IF NOT EXISTS community_poll_votes (
                    poll_id INTEGER NOT NULL REFERENCES community_polls(id),
                    qq_id TEXT NOT NULL,
                    option_index INTEGER NOT NULL CHECK(option_index BETWEEN 0 AND 5),
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(poll_id, qq_id)
                );
                CREATE TABLE IF NOT EXISTS poll_requests (
                    group_id TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    response TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(group_id, qq_id, request_id)
                );
            """)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def handle(
        self,
        group_id: str,
        qq_id: str,
        role: str,
        argument: str,
        request_id: str = "",
    ) -> str:
        """Handle the text after /投票; role is OneBot's member/admin/owner.

        Validation failures become short replies. Unexpected storage or clock
        errors propagate to the caller's normal command error handling.
        """
        try:
            group_id, qq_id = str(group_id), str(qq_id)
            request_id = str(request_id or "")
            if not group_id or not qq_id or max(len(group_id), len(qq_id)) > 128:
                raise PollError("投票需要有效的群聊和成员身份。")
            if len(request_id) > 128:
                raise PollError("消息编号过长，请重新发送命令。")
            self._validate_text(argument, 512, "投票命令")
            argument = argument.strip().replace("｜", "|")
            fingerprint = hashlib.sha256(argument.encode("utf-8")).hexdigest()
            with closing(self._connect()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                if request_id:
                    receipt = conn.execute(
                        "SELECT fingerprint, response FROM poll_requests "
                        "WHERE group_id = ? AND qq_id = ? AND request_id = ?",
                        (group_id, qq_id, request_id),
                    ).fetchone()
                    if receipt:
                        if receipt["fingerprint"] != fingerprint:
                            raise PollError("同一条消息不能重复执行不同的投票操作。")
                        return str(receipt["response"])
                now = self.clock()
                if now.tzinfo is None or now.utcoffset() is None:
                    raise RuntimeError("PollService clock must return a timezone-aware datetime")
                timestamp = now.timestamp()
                response = self._dispatch(conn, group_id, qq_id, role, argument, timestamp)
                if request_id:
                    conn.execute(
                        "INSERT INTO poll_requests VALUES (?, ?, ?, ?, ?, ?)",
                        (group_id, qq_id, request_id, fingerprint, response, timestamp),
                    )
                return response
        except PollError as exc:
            return str(exc)

    @staticmethod
    def _validate_text(value: str, limit: int, label: str) -> None:
        if not isinstance(value, str) or len(value) > limit:
            raise PollError(f"{label}最多{limit}个字符。")
        if any(unicodedata.category(char).startswith("C") for char in value):
            raise PollError("投票内容不能包含换行、控制符或隐藏字符。")
        if "[cq:" in value.casefold():
            raise PollError("投票内容不能包含 CQ 消息代码，请使用纯文字。")

    def _dispatch(
        self, conn: sqlite3.Connection, group_id: str, qq_id: str,
        role: str, argument: str, now: float,
    ) -> str:
        if argument in ("", "列表"):
            return self._list(conn, group_id, now)
        if argument == "帮助":
            return POLL_HELP
        parts = argument.split(maxsplit=1)
        if parts[0] == "发起":
            return self._create(conn, group_id, qq_id, parts[1] if len(parts) > 1 else "", now)
        if parts[0] in ("结果", "结束") and len(parts) == 2:
            poll = self._get(conn, group_id, parts[1])
            if parts[0] == "结束":
                if poll["created_by"] != qq_id and role not in ("admin", "owner"):
                    raise PollError("只有投票发起者或群管理员可以结束投票。")
                if self._active(poll, now):
                    conn.execute("UPDATE community_polls SET ended_at = ? WHERE id = ?",
                                 (now, poll["id"]))
                    poll = self._get(conn, group_id, str(poll["id"]))
            return self._results(conn, poll, now)
        if len(parts) == 2 and re.fullmatch(r"[0-9]{1,12}", parts[0]):
            return self._vote(conn, self._get(conn, group_id, parts[0]), qq_id, parts[1], now)
        return POLL_HELP

    def _create(
        self, conn: sqlite3.Connection, group_id: str, qq_id: str, text: str, now: float,
    ) -> str:
        duration = 30
        match = re.match(r"^([+-]?[0-9]+)\s*(?:分钟|分)(?:\s+|$)", text)
        if match:
            duration = int(match[1])
            text = text[match.end():]
        if not 1 <= duration <= 1440:
            raise PollError("投票时长须为1—1440分钟。")
        pieces = [part.strip() for part in text.split("|")]
        if not 3 <= len(pieces) <= 7 or not all(pieces):
            raise PollError("请填写问题和2—6个选项，用 | 分隔：/投票 发起 今晚玩什么？ | CS2 | 瓦")
        question, *options = pieces
        self._validate_text(question, 100, "投票问题")
        for option in options:
            self._validate_text(option, 40, "投票选项")
        if len({" ".join(option.casefold().split()) for option in options}) != len(options):
            raise PollError("投票选项不能重复。")
        active = conn.execute(
            "SELECT created_by FROM community_polls "
            "WHERE group_id = ? AND ended_at IS NULL AND expires_at > ?",
            (group_id, now),
        ).fetchall()
        if any(row["created_by"] == qq_id for row in active):
            raise PollError("你已有一个进行中的投票，请先结束或等待到期。")
        if len(active) >= 3:
            raise PollError("本群已有3个进行中的投票，请等待一个结束后再发起。")
        cursor = conn.execute(
            "INSERT INTO community_polls "
            "(group_id, created_by, question, options_json, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (group_id, qq_id, question, json.dumps(options, ensure_ascii=False),
             now, now + duration * 60),
        )
        poll_id = cursor.lastrowid
        lines = [f"🗳 投票 #{poll_id}｜{question}"]
        lines += [f"{LETTERS[index]}. {option}" for index, option in enumerate(options)]
        lines += [f"开放{duration}分钟｜每人一票，可改票",
                  f"参与：/投票 {poll_id} A｜票数：/投票 结果 {poll_id}",
                  "到期停止收票，结果按需查询。"]
        return "\n".join(lines)

    @staticmethod
    def _get(conn: sqlite3.Connection, group_id: str, poll_id: str) -> sqlite3.Row:
        if not re.fullmatch(r"[0-9]{1,12}", poll_id.strip()):
            raise PollError("请输入有效的投票编号，例如：/投票 结果 1")
        row = conn.execute("SELECT * FROM community_polls WHERE group_id = ? AND id = ?",
                           (group_id, int(poll_id))).fetchone()
        if row is None:
            raise PollError("本群没有这个投票，请发送 /投票 查看。")
        return row

    @staticmethod
    def _active(poll: sqlite3.Row, now: float) -> bool:
        return poll["ended_at"] is None and poll["expires_at"] > now

    def _list(self, conn: sqlite3.Connection, group_id: str, now: float) -> str:
        polls = conn.execute(
            "SELECT * FROM community_polls WHERE group_id = ? AND ended_at IS NULL "
            "AND expires_at > ? ORDER BY id DESC LIMIT 5", (group_id, now),
        ).fetchall()
        if not polls:
            return "本群暂无进行中的投票。\n" + POLL_HELP
        lines = ["🗳 本群进行中的投票"]
        for poll in polls:
            count = conn.execute("SELECT COUNT(*) FROM community_poll_votes WHERE poll_id = ?",
                                 (poll["id"],)).fetchone()[0]
            remaining = max(1, int((poll["expires_at"] - now + 59) // 60))
            lines.append(f"#{poll['id']} {poll['question']}｜{count}票｜约{remaining}分钟")
        lines += ["/投票 结果 编号 查看选项和票数；/投票 编号 A 参与。",
                  "/投票 帮助 查看发起方法。"]
        return "\n".join(lines)

    def _vote(
        self, conn: sqlite3.Connection, poll: sqlite3.Row, qq_id: str, choice: str, now: float,
    ) -> str:
        if not self._active(poll, now):
            raise PollError(f"投票 #{poll['id']} 已结束，发送 /投票 结果 {poll['id']} 查看结果。")
        choice = choice.strip().upper()
        if choice in LETTERS and len(choice) == 1:
            index = LETTERS.index(choice)
        elif re.fullmatch("[1-6]", choice):
            index = int(choice) - 1
        else:
            raise PollError("请选择对应选项字母 A—F 或数字1—6。")
        options = json.loads(poll["options_json"])
        if index >= len(options):
            raise PollError("这个投票没有该选项，请先查看投票结果中的选项。")
        previous = conn.execute(
            "SELECT option_index FROM community_poll_votes WHERE poll_id = ? AND qq_id = ?",
            (poll["id"], qq_id),
        ).fetchone()
        conn.execute(
            "INSERT INTO community_poll_votes (poll_id, qq_id, option_index, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(poll_id, qq_id) "
            "DO UPDATE SET option_index = excluded.option_index, updated_at = excluded.updated_at",
            (poll["id"], qq_id, index, now),
        )
        action = "已改投" if previous is not None and previous[0] != index else "已投"
        return f"投票 #{poll['id']}：{action} {LETTERS[index]}，每人仅计一票。"

    def _results(self, conn: sqlite3.Connection, poll: sqlite3.Row, now: float) -> str:
        options = json.loads(poll["options_json"])
        counts = dict(conn.execute(
            "SELECT option_index, COUNT(*) FROM community_poll_votes "
            "WHERE poll_id = ? GROUP BY option_index", (poll["id"],),
        ).fetchall())
        total = sum(counts.values())
        active = self._active(poll, now)
        status = "进行中" if active else "已结束"
        lines = [f"🗳 投票 #{poll['id']}｜{status}", poll["question"]]
        for index, option in enumerate(options):
            count = counts.get(index, 0)
            percent = f"{count / total:.0%}" if total else "0%"
            lines.append(f"{LETTERS[index]}. {option}：{count}票（{percent}）")
        lines.append(f"共{total}人参与")
        deadline = datetime.fromtimestamp(poll["expires_at"], CHINA_TIMEZONE)
        if active:
            lines.append(f"截止北京时间{deadline:%m-%d %H:%M}｜/投票 {poll['id']} A 参与或改票")
        elif poll["ended_at"] is None:
            lines.append("已到期，停止收票。")
        else:
            lines.append("发起者或管理员已提前结束投票。")
        return "\n".join(lines)
