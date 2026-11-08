"""Quiet, opt-in daily rewards derived only from verified match snapshots."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone

from pwcs2_bot.community import CommunityRepository
from pwcs2_bot.database import BindingRepository
from pwcs2_bot.match_formatting import MatchSnapshot
from pwcs2_bot.query_source import parse_source_argument

CHINA = timezone(timedelta(hours=8))
# key, name, requirement, reward, progress column, target
TASKS = (
    ("kills", "火力全开", "单场击杀达到 20", 10, "max_kills", 20),
    ("assists", "团队支援", "单场助攻达到 6", 10, "max_assists", 6),
    ("team_win", "双排凯旋", "与至少一位已绑定群友同队获胜", 15, "team_win", 1),
)


def _count(value: str) -> int | None:
    raw = str(value).strip()
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 3:
        return None
    count = int(raw)
    return count if count <= 200 else None


class ChallengeService:
    def __init__(
        self,
        community: CommunityRepository,
        bindings: BindingRepository,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.community = community
        self.bindings = bindings
        self.clock = clock
        # Reuse the wallet connection so claiming a reward and recording its
        # ledger reference are one transaction, even across multiple workers.
        with community._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_challenge_progress (
                    group_id TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    challenge_date TEXT NOT NULL,
                    source TEXT NOT NULL CHECK (source IN ('pvp', 'official')),
                    max_kills INTEGER NOT NULL DEFAULT 0,
                    max_assists INTEGER NOT NULL DEFAULT 0,
                    team_win INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, qq_id, challenge_date, source)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS daily_challenge_date "
                "ON daily_challenge_progress(challenge_date)"
            )

    def record_match(self, group_id: str, match: MatchSnapshot) -> None:
        """Maxima make replays/notifications from multiple players idempotent.

        Missing dates never become today's activity. Team wins require scores
        and two distinct, currently bound Steam accounts on the winning side.
        """
        now = self.clock().astimezone(CHINA)
        try:
            ended = datetime.fromisoformat(match.ended_at.replace("Z", "+00:00"))
        except ValueError:
            return
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=CHINA)
        ended = ended.astimezone(CHINA)
        if (
            match.source not in {"pvp", "official"}
            or not match.match_id
            or ended > now + timedelta(minutes=5)
            or ended.date() > now.date()
            or ended.date() < now.date() - timedelta(days=1)
        ):
            return
        bindings = {
            item.steam_id: item.qq_id for item in self.bindings.list_group_bindings(group_id)
        }
        players = {p.steam_id: p for p in match.players if p.steam_id in bindings}
        score1, score2 = _count(match.team1_score), _count(match.team2_score)
        winner = 0
        if score1 is not None and score2 is not None and score1 != score2:
            winner = 1 if score1 > score2 else 2
        tracked = next((p for p in match.players if p.steam_id == match.steam_id), None)
        if match.result == "draw":
            winner = 0
        elif tracked is not None and tracked.team in {1, 2} and match.result in {"win", "loss"}:
            expected = tracked.team if match.result == "win" else 3 - tracked.team
            if expected != winner:
                # API adjudication/forfeit can disagree with visible scores.
                # K/A is still useful; uncertain wins must not award team coins.
                winner = 0
        winners = {bindings[p.steam_id] for p in players.values() if p.team == winner and winner}
        rows: dict[str, tuple[int, int, int]] = {
            bindings[p.steam_id]: (
                _count(p.kills) or 0,
                _count(p.assists) or 0,
                int(len(winners) >= 2 and p.team == winner),
            )
            for p in players.values()
        }
        # Match-list responses often omit the ten-player roster. Their own
        # K/A values can still qualify, but must never imply a teammate win.
        if match.steam_id in bindings and match.steam_id not in players:
            rows[bindings[match.steam_id]] = (
                _count(match.kills) or 0, _count(match.assists) or 0, 0,
            )
        if not rows:
            return
        with self.community._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM daily_challenge_progress WHERE challenge_date < ?",
                ((now.date() - timedelta(days=2)).isoformat(),),
            )
            connection.executemany(
                """
                INSERT INTO daily_challenge_progress
                    (group_id, qq_id, challenge_date, source,
                     max_kills, max_assists, team_win, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (group_id, qq_id, challenge_date, source) DO UPDATE SET
                    max_kills = MAX(max_kills, excluded.max_kills),
                    max_assists = MAX(max_assists, excluded.max_assists),
                    team_win = MAX(team_win, excluded.team_win),
                    updated_at = excluded.updated_at
                """,
                [
                    (group_id, qq_id, ended.date().isoformat(), match.source,
                     kills, assists, team_win, now.isoformat())
                    for qq_id, (kills, assists, team_win) in rows.items()
                ],
            )

    def handle(self, group_id: str, qq_id: str, argument: str = "") -> str:
        selected = parse_source_argument(argument)
        tokens = selected.argument.split()
        if any(token not in {"领取", "昨天", "今日", "今天"} for token in tokens):
            raise ValueError("用法：#挑战 [gp] [昨天] [领取]；默认查看今日完美挑战")
        if "昨天" in tokens and ({"今日", "今天"} & set(tokens)):
            raise ValueError("请选择今天或昨天")
        if self.bindings.get_by_qq(group_id, qq_id) is None:
            return "请先 #绑定 <SteamID64>，再参与每日挑战。"
        day = self.clock().astimezone(CHINA).date()
        if "昨天" in tokens:
            day -= timedelta(days=1)
        source = selected.source
        label = "官匹" if source == "official" else "完美"
        prefix = f"daily-challenge:{day.isoformat()}:{source}:"
        credited = 0
        with self.community._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            progress = connection.execute(
                "SELECT * FROM daily_challenge_progress "
                "WHERE group_id=? AND qq_id=? AND challenge_date=? AND source=?",
                (group_id, qq_id, day.isoformat(), source),
            ).fetchone()
            claimed = {
                row[0]
                for row in connection.execute(
                    "SELECT reference_key FROM coin_ledger "
                    "WHERE group_id=? AND qq_id=? AND reference_key LIKE ?",
                    (group_id, qq_id, prefix + "%"),
                )
            }
            lines = [f"🎯 每日挑战｜{label}｜{day.isoformat()}"]
            for key, name, requirement, reward, column, target in TASKS:
                value = int(progress[column]) if progress else 0
                reference = prefix + key
                if "领取" in tokens and value >= target and reference not in claimed:
                    if self.community._change_balance(
                        connection, group_id, qq_id, reward,
                        f"每日挑战·{label}·{name}", reference,
                    ):
                        credited += reward
                    claimed.add(reference)
                state = "已领取" if reference in claimed else (
                    "可领取" if value >= target else f"{min(value, target)}/{target}"
                )
                lines.append(f"• {name} +{reward}｜{requirement}｜{state}")
        if "领取" in tokens:
            lines.append(f"已到账 {credited} 牛币。" if credited else "暂时没有可领取的新奖励。")
        selector = " gp" if source == "official" else ""
        yesterday = " 昨天" if "昨天" in tokens else ""
        lines.extend([
            f"发送 #挑战{selector}{yesterday} 领取，一次领取所有已达成奖励。",
            "按北京时间结算；完美/官匹各每天最多 35 牛币，可补领昨天。",
            f"仅统计上线后收到的比赛；#战绩{selector} 可补录近十场击杀/助攻数据。",
        ])
        return "\n".join(lines)
