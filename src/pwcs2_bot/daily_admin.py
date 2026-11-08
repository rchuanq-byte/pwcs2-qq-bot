"""Select and rotate a transparent, activity-based QQ group duty administrator."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger

from pwcs2_bot.daily_summary import DailySummaryService
from pwcs2_bot.database import BindingRepository, DailyAdminRun

FetchGroupMembers = Callable[[str], Awaitable[list[dict[str, Any]]]]
ApplyAssignment = Callable[[str, str | None, str | None], Awaitable[None]]
SendAnnouncement = Callable[[str, DailyAdminRun], Awaitable[None]]
RandBelow = Callable[[int], int]

_SCORE_TICKET_SCALE = 100
_WEIGHTED_SELECTION_METHOD = "score_weighted_random"


@dataclass(frozen=True, slots=True)
class DailyAdminCandidate:
    qq_id: str
    nickname: str
    match_count: int
    message_count: int
    match_score: float
    message_score: float
    repeat_penalty: float
    total_score: float


def _member_level(member: dict[str, Any]) -> int | None:
    try:
        return int(str(member.get("level", "")).strip())
    except ValueError:
        return None


def _is_robot(member: dict[str, Any]) -> bool:
    value = member.get("is_robot", False)
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes"}


def rank_daily_admin_candidates(
    *,
    members: list[dict[str, Any]],
    bound_qq_ids: set[str],
    match_counts: dict[str, int],
    message_counts: dict[str, int],
    source_date: date,
    bot_qq_id: str,
    previous_qq_id: str | None,
    min_group_level: int = 2,
    min_messages: int = 5,
    match_cap: int = 5,
    message_cap: int = 50,
    match_weight: float = 70,
    message_weight: float = 30,
    repeat_penalty: float = 10,
) -> tuple[DailyAdminCandidate, ...]:
    """Return eligible candidates ordered by score with deterministic tie-breaking."""
    ranked: list[tuple[DailyAdminCandidate, str]] = []
    for member in members:
        qq_id = str(member.get("user_id") or "").strip()
        if not qq_id.isdecimal() or qq_id == bot_qq_id or qq_id not in bound_qq_ids:
            continue
        role = str(member.get("role") or "").strip().casefold()
        if role != "member" and not (role == "admin" and qq_id == previous_qq_id):
            continue
        if _is_robot(member):
            continue
        level = _member_level(member)
        if level is None or level < min_group_level:
            continue
        matches = max(0, int(match_counts.get(qq_id, 0)))
        messages = max(0, int(message_counts.get(qq_id, 0)))
        if matches < 1 and messages < min_messages:
            continue
        match_points = min(matches, match_cap) / match_cap * match_weight
        message_points = min(messages, message_cap) / message_cap * message_weight
        penalty = repeat_penalty if qq_id == previous_qq_id else 0.0
        total = max(0.0, match_points + message_points - penalty)
        nickname = str(member.get("card") or member.get("nickname") or qq_id).strip()
        candidate = DailyAdminCandidate(
            qq_id=qq_id,
            nickname=nickname[:80] or qq_id,
            match_count=matches,
            message_count=messages,
            match_score=round(match_points, 2),
            message_score=round(message_points, 2),
            repeat_penalty=round(penalty, 2),
            total_score=round(total, 2),
        )
        tie_key = hashlib.sha256(
            f"{source_date.isoformat()}:{qq_id}".encode()
        ).hexdigest()
        ranked.append((candidate, tie_key))
    ranked.sort(
        key=lambda item: (
            -item[0].total_score,
            -item[0].match_count,
            -item[0].message_count,
            item[1],
        )
    )
    return tuple(item[0] for item in ranked)


def select_weighted_daily_admin(
    candidates: tuple[DailyAdminCandidate, ...],
    *,
    randbelow: RandBelow = secrets.randbelow,
) -> DailyAdminCandidate | None:
    """Select one candidate randomly, using the composite score as its weight."""
    if not candidates:
        return None
    weights = [
        max(0, int(round(candidate.total_score * _SCORE_TICKET_SCALE)))
        for candidate in candidates
    ]
    total_weight = sum(weights)
    if total_weight <= 0:
        # An eligible previous administrator can have a zero score after the
        # repeat penalty. If every candidate is in that state, draw uniformly
        # instead of silently leaving the post vacant.
        weights = [1] * len(candidates)
        total_weight = len(candidates)
    ticket = randbelow(total_weight)
    if ticket < 0 or ticket >= total_weight:
        raise ValueError("randbelow returned a value outside the requested range")
    cumulative = 0
    for candidate, weight in zip(candidates, weights, strict=True):
        cumulative += weight
        if ticket < cumulative:
            return candidate
    raise RuntimeError("weighted daily administrator draw did not select a candidate")


def _winner_probability(run: DailyAdminRun) -> float:
    weights = [
        max(
            0,
            int(round(float(item.get("total_score") or 0) * _SCORE_TICKET_SCALE)),
        )
        for item in run.ranking
    ]
    total_weight = sum(weights)
    if total_weight <= 0:
        return 100 / len(run.ranking) if run.ranking else 0
    winner_weight = next(
        (
            weight
            for item, weight in zip(run.ranking, weights, strict=True)
            if str(item.get("qq_id")) == run.winner_qq_id
        ),
        0,
    )
    return winner_weight / total_weight * 100


def format_daily_admin_announcement(
    run: DailyAdminRun,
    *,
    ban_revoke_threshold_seconds: int = 600,
) -> str:
    if run.winner_qq_id is None:
        return (
            "🌙 今日值日管理员\n"
            f"统计日期：{run.source_date}\n"
            "昨日无人达到候选门槛（至少 1 场 CS2 或 5 条发言），今日不设值日管理员。"
        )
    lines = [
        "🌟 今日值日管理员",
        f"{run.winner_nickname}（QQ {run.winner_qq_id}）",
        f"统计日期：{run.source_date}",
        (
            f"综合分 {run.total_score:.1f}｜CS2 {run.match_count} 场 "
            f"{run.match_score:.1f}/70｜发言 {run.message_count} 条 "
            f"{run.message_score:.1f}/30"
        ),
    ]
    selection_method = str(run.ranking[0].get("selection_method") or "")
    if selection_method == _WEIGHTED_SELECTION_METHOD:
        lines.append(
            "抽签规则：按综合分权重随机抽取，分数越高概率越大，"
            f"并非最高分必选（本次中签概率约 {_winner_probability(run):.1f}%）。"
        )
    else:
        lines.append("选拔规则：沿用本次换届开始前已持久化的旧版计划。")
    winner = next(
        (item for item in run.ranking if str(item.get("qq_id")) == run.winner_qq_id),
        None,
    )
    if winner and float(winner.get("repeat_penalty") or 0) > 0:
        lines.append("连任调节：-10 分（让活跃成员有更多轮换机会）")
    if run.ranking:
        lines.append("昨日活跃榜：")
        for index, item in enumerate(run.ranking[:3], start=1):
            lines.append(
                f"{index}. {item['nickname']} {float(item['total_score']):.1f} 分"
                f"（{int(item['match_count'])} 场 / {int(item['message_count'])} 条）"
            )
    threshold_minutes = ban_revoke_threshold_seconds / 60
    lines.extend(
        [
            "值日职责：组织开黑与 TS、维护聊天秩序、处理黑历史纠纷。",
            (
                "权限边界：管理员不得禁言成员；机器人会立即解禁，原禁言超过 "
                f"{threshold_minutes:g} 分钟还会自动撤权。"
            ),
            "同时禁止踢人、擅改群资料或滥用成员隐私；严重事项交给群主处理。",
            "任期至明日换届；日常换届只撤销机器人授予的权限，超时禁言违规除外。",
        ]
    )
    return "\n".join(lines)


class DailyAdminService:
    def __init__(
        self,
        repository: BindingRepository,
        daily_summary: DailySummaryService,
        fetch_group_members: FetchGroupMembers,
        apply_assignment: ApplyAssignment,
        send_announcement: SendAnnouncement,
        get_bot_qq_id: Callable[[], str],
        *,
        enabled: bool = True,
        run_time: str = "00:00",
        timezone_name: str = "Asia/Shanghai",
        poll_interval_seconds: float = 300,
        initial_delay_seconds: float = 60,
        min_group_level: int = 2,
        min_messages: int = 5,
        match_cap: int = 5,
        message_cap: int = 50,
        match_weight: float = 70,
        message_weight: float = 30,
        repeat_penalty: float = 10,
        allowed_groups: list[str] | None = None,
        randbelow: RandBelow = secrets.randbelow,
    ) -> None:
        hour, minute = (int(part) for part in run_time.split(":"))
        self.repository = repository
        self.daily_summary = daily_summary
        self.fetch_group_members = fetch_group_members
        self.apply_assignment = apply_assignment
        self.send_announcement = send_announcement
        self.get_bot_qq_id = get_bot_qq_id
        self.enabled = enabled
        self.run_time = time(hour, minute)
        self.timezone = ZoneInfo(timezone_name)
        self.poll_interval_seconds = poll_interval_seconds
        self.initial_delay_seconds = initial_delay_seconds
        self.min_group_level = min_group_level
        self.min_messages = min_messages
        self.match_cap = match_cap
        self.message_cap = message_cap
        self.match_weight = match_weight
        self.message_weight = message_weight
        self.repeat_penalty = repeat_penalty
        self.allowed_groups = set(allowed_groups or [])
        self.randbelow = randbelow
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def record_message(self, group_id: str, qq_id: str, now: datetime | None = None) -> None:
        local_now = now or datetime.now(self.timezone)
        if local_now.tzinfo is None:
            local_now = local_now.replace(tzinfo=self.timezone)
        else:
            local_now = local_now.astimezone(self.timezone)
        self.repository.record_daily_member_message(
            group_id,
            local_now.date().isoformat(),
            qq_id,
        )

    async def run(self) -> None:
        if not self.enabled or await self._wait(self.initial_delay_seconds):
            return
        logger.info(
            "pwcs2: daily admin scheduler started (time={} timezone={})",
            self.run_time.strftime("%H:%M"),
            self.timezone.key,
        )
        while not self._stop_event.is_set():
            try:
                await self.run_due_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("pwcs2: unexpected daily admin failure")
            if await self._wait(self.poll_interval_seconds):
                return

    async def _wait(self, seconds: float) -> bool:
        if seconds <= 0:
            return self._stop_event.is_set()
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except TimeoutError:
            return False
        return True

    async def run_due_once(self, now: datetime | None = None) -> int:
        local_now = now or datetime.now(self.timezone)
        if local_now.tzinfo is None:
            local_now = local_now.replace(tzinfo=self.timezone)
        else:
            local_now = local_now.astimezone(self.timezone)
        if local_now.time().replace(tzinfo=None) < self.run_time:
            return 0
        duty_date = local_now.date()
        source_date = duty_date - timedelta(days=1)
        if self.allowed_groups:
            group_ids = self.allowed_groups
        else:
            group_ids = {
                binding.group_id for binding in self.repository.list_bindings()
            } | set(
                self.repository.list_daily_member_activity_groups(
                    source_date.isoformat()
                )
            )
        completed = 0
        for group_id in sorted(group_ids):
            try:
                run = self.repository.get_daily_admin_run(
                    group_id, duty_date.isoformat()
                )
                if run is None:
                    run = await self._plan_group(group_id, duty_date, source_date)
                    self.repository.plan_daily_admin(run)
                    run = self.repository.get_daily_admin_run(
                        group_id, duty_date.isoformat()
                    )
                    if run is None:
                        raise RuntimeError("daily admin plan was not persisted")
                if run.status == "planned":
                    await self.apply_assignment(
                        group_id, run.previous_qq_id, run.winner_qq_id
                    )
                    self.repository.complete_daily_admin_assignment(
                        group_id, run.duty_date, run.winner_qq_id
                    )
                    run = self.repository.get_daily_admin_run(group_id, run.duty_date)
                    if run is None:
                        raise RuntimeError("daily admin assignment was not persisted")
                if not run.announcement_sent:
                    await self.send_announcement(group_id, run)
                    self.repository.mark_daily_admin_announced(group_id, run.duty_date)
                completed += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "pwcs2: daily admin failed group={} duty_date={}: {}",
                    group_id,
                    duty_date.isoformat(),
                    exc,
                )
        return completed

    async def _plan_group(
        self,
        group_id: str,
        duty_date: date,
        source_date: date,
    ) -> DailyAdminRun:
        members = await self.fetch_group_members(group_id)
        summary = await self.daily_summary.collect_group(group_id, source_date)
        bindings = self.repository.list_group_bindings(group_id)
        previous = self.repository.get_daily_admin(group_id)
        matches = {member.qq_id: member.matches for member in summary.members}
        messages = self.repository.get_daily_member_message_counts(
            group_id, source_date.isoformat()
        )
        ranked = rank_daily_admin_candidates(
            members=members,
            bound_qq_ids={binding.qq_id for binding in bindings},
            match_counts=matches,
            message_counts=messages,
            source_date=source_date,
            bot_qq_id=self.get_bot_qq_id(),
            previous_qq_id=previous,
            min_group_level=self.min_group_level,
            min_messages=self.min_messages,
            match_cap=self.match_cap,
            message_cap=self.message_cap,
            match_weight=self.match_weight,
            message_weight=self.message_weight,
            repeat_penalty=self.repeat_penalty,
        )
        winner = select_weighted_daily_admin(ranked, randbelow=self.randbelow)
        ranking = tuple(
            {
                "qq_id": item.qq_id,
                "nickname": item.nickname,
                "match_count": item.match_count,
                "message_count": item.message_count,
                "match_score": item.match_score,
                "message_score": item.message_score,
                "repeat_penalty": item.repeat_penalty,
                "total_score": item.total_score,
                "selection_method": _WEIGHTED_SELECTION_METHOD,
            }
            for item in ranked
        )
        return DailyAdminRun(
            group_id=group_id,
            duty_date=duty_date.isoformat(),
            source_date=source_date.isoformat(),
            previous_qq_id=previous,
            winner_qq_id=winner.qq_id if winner else None,
            winner_nickname=winner.nickname if winner else "",
            match_count=winner.match_count if winner else 0,
            message_count=winner.message_count if winner else 0,
            match_score=winner.match_score if winner else 0,
            message_score=winner.message_score if winner else 0,
            total_score=winner.total_score if winner else 0,
            ranking=ranking,
            status="planned",
            announcement_sent=False,
        )
