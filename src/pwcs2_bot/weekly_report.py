"""Build a Sunday poster from group members' Perfect World matches."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger

from pwcs2_bot.community import (
    WEEKLY_ACTIVITY_MATCHES,
    WEEKLY_ACTIVITY_REWARD,
    CommunityRepository,
)
from pwcs2_bot.daily_summary import _match_datetime, _match_identity, _member_summary
from pwcs2_bot.database import Binding, BindingRepository
from pwcs2_bot.game_identity import resolve_game_profile
from pwcs2_bot.perfectworld import PerfectWorldApiError, PerfectWorldClient


@dataclass(frozen=True, slots=True)
class WeeklyReportMember:
    qq_id: str
    name: str
    matches: int
    wins: int
    losses: int
    kills: int
    deaths: int
    kd: float | None
    avg_rating: float | None
    avg_adr: float | None
    elo_change: float | None
    favorite_map: str


@dataclass(frozen=True, slots=True)
class WeeklyReportSnapshot:
    group_id: str
    week_start: str
    week_end: str
    bound_members: int
    active_members: int
    unique_matches: int
    total_kills: int
    total_elo_change: float
    popular_maps: tuple[tuple[str, int], ...]
    members: tuple[WeeklyReportMember, ...]
    activity_rewards: tuple[tuple[str, str, int], ...] = ()


SendReport = Callable[[str, WeeklyReportSnapshot], Awaitable[None]]


def format_weekly_report(snapshot: WeeklyReportSnapshot) -> str:
    lines = [
        f"📈 CS2 每周战报｜{snapshot.week_start}～{snapshot.week_end}",
        f"{snapshot.active_members}/{snapshot.bound_members} 人出战｜"
        f"{snapshot.unique_matches} 场独立对局｜{snapshot.total_kills} 杀",
    ]
    for member in snapshot.members[:8]:
        rating = "-" if member.avg_rating is None else f"{member.avg_rating:.2f}"
        elo = f"{member.elo_change:+.0f}" if member.elo_change is not None else "-"
        lines.append(
            f"• {member.name}：{member.matches}场 {member.wins}胜｜"
            f"Rating {rating}｜ELO {elo}"
        )
    if snapshot.activity_rewards:
        reward_names = "、".join(name for _qq_id, name, _reward in snapshot.activity_rewards)
        lines.append(
            f"🎁 {WEEKLY_ACTIVITY_MATCHES} 场活跃奖励：{reward_names}"
            f"（每人 +{WEEKLY_ACTIVITY_REWARD} 牛币）"
        )
    return "\n".join(lines)


class WeeklyReportService:
    def __init__(
        self,
        repository: CommunityRepository,
        bindings: BindingRepository,
        api: PerfectWorldClient,
        send_report: SendReport,
        *,
        enabled: bool = True,
        send_time: str = "22:00",
        weekday: int = 6,
        timezone_name: str = "Asia/Shanghai",
        poll_interval_seconds: float = 300,
        initial_delay_seconds: float = 90,
        match_page_size: int = 50,
        allowed_groups: list[str] | None = None,
    ) -> None:
        hour, minute = (int(part) for part in send_time.split(":", 1))
        if not 0 <= weekday <= 6:
            raise ValueError("weekly report weekday must be between 0 and 6")
        self.repository = repository
        self.bindings = bindings
        self.api = api
        self.send_report = send_report
        self.enabled = enabled
        self.send_time = time(hour, minute)
        self.weekday = weekday
        self.timezone = ZoneInfo(timezone_name)
        self.poll_interval_seconds = poll_interval_seconds
        self.initial_delay_seconds = initial_delay_seconds
        self.match_page_size = match_page_size
        self.allowed_groups = set(allowed_groups or [])
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        if not self.enabled or await self._wait(self.initial_delay_seconds):
            return
        logger.info(
            "pwcs2: weekly report started (weekday={} time={} timezone={})",
            self.weekday,
            self.send_time.strftime("%H:%M"),
            self.timezone.key,
        )
        while not self._stop_event.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("pwcs2: unexpected weekly report failure")
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

    async def poll_once(self, *, now: datetime | None = None) -> int:
        local_now = (now or datetime.now(self.timezone)).astimezone(self.timezone)
        if local_now.weekday() != self.weekday or local_now.time() < self.send_time:
            return 0
        start = local_now.date() - timedelta(days=local_now.weekday())
        week_start = start.isoformat()
        groups: dict[str, list[Binding]] = {}
        for binding in self.bindings.list_bindings():
            if self.allowed_groups and binding.group_id not in self.allowed_groups:
                continue
            groups.setdefault(binding.group_id, []).append(binding)
        sent = 0
        for group_id, bindings in sorted(groups.items()):
            if self.repository.weekly_report_processed(group_id, week_start):
                continue
            try:
                snapshot = await self.collect_group(
                    group_id,
                    bindings,
                    start=start,
                    end=local_now.date(),
                )
            except (PerfectWorldApiError, ValueError) as exc:
                logger.warning(
                    "pwcs2: weekly report data incomplete group={}: {}", group_id, exc
                )
                continue
            if not snapshot.members:
                self.repository.mark_weekly_report(group_id, week_start, was_sent=False)
                continue
            eligible = [
                (member.qq_id, member.matches)
                for member in snapshot.members
                if member.matches >= WEEKLY_ACTIVITY_MATCHES
            ]
            self.repository.award_weekly_activity(group_id, week_start, eligible)
            names = {member.qq_id: member.name for member in snapshot.members}
            steam_ids = {binding.qq_id: binding.steam_id for binding in bindings}
            activity_rewards = tuple(
                (qq_id, names.get(qq_id) or steam_ids.get(qq_id) or "未公开游戏ID", reward)
                for qq_id, _match_count, reward
                in self.repository.weekly_activity_rewards(group_id, week_start)
            )
            snapshot = replace(snapshot, activity_rewards=activity_rewards)
            try:
                await self.send_report(group_id, snapshot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("pwcs2: weekly report send failed group={}: {}", group_id, exc)
                continue
            self.repository.mark_weekly_report(group_id, week_start, was_sent=True)
            sent += 1
        return sent

    async def collect_group(
        self,
        group_id: str,
        bindings: list[Binding],
        *,
        start: date,
        end: date,
    ) -> WeeklyReportSnapshot:
        semaphore = asyncio.Semaphore(4)

        async def fetch(
            binding: Binding,
        ) -> tuple[Binding, list[dict[str, Any]], dict[str, str]]:
            async with semaphore:
                raw = await self.api.match_list(
                    binding.steam_id, page_size=self.match_page_size
                )
                matches = []
                seen: set[str] = set()
                for match in raw:
                    ended_at = _match_datetime(match, self.timezone)
                    if ended_at is None or not start <= ended_at.date() <= end:
                        continue
                    identity = _match_identity(match)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    matches.append(match)
                profile = (
                    await resolve_game_profile(self.api, binding.steam_id, matches)
                    if matches else {"name": binding.steam_id}
                )
            return binding, matches, profile

        results = await asyncio.gather(
            *(fetch(binding) for binding in bindings),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise PerfectWorldApiError(
                f"{len(failures)}/{len(bindings)} 名成员的周比赛记录查询失败"
            )
        members: list[WeeklyReportMember] = []
        unique_matches: dict[str, str] = {}
        for result in results:
            if not isinstance(result, tuple):
                continue
            binding, matches, profile = result
            if not matches:
                continue
            summary = _member_summary(binding, matches, profile=profile)
            members.append(
                WeeklyReportMember(
                    qq_id=binding.qq_id,
                    name=summary.name,
                    matches=summary.matches,
                    wins=summary.wins,
                    losses=summary.losses,
                    kills=summary.kills,
                    deaths=summary.deaths,
                    kd=summary.kd,
                    avg_rating=summary.avg_rating,
                    avg_adr=summary.avg_adr,
                    elo_change=summary.elo_change,
                    favorite_map=summary.maps[0][0] if summary.maps else "未知地图",
                )
            )
            for match in matches:
                unique_matches.setdefault(
                    _match_identity(match),
                    str(match.get("mapName") or match.get("map") or "未知地图"),
                )
        members.sort(
            key=lambda member: (
                -(member.avg_rating if member.avg_rating is not None else -1),
                -member.matches,
                member.name.casefold(),
            )
        )
        popular_maps = tuple(
            Counter(unique_matches.values()).most_common(3)
        )
        return WeeklyReportSnapshot(
            group_id=group_id,
            week_start=start.isoformat(),
            week_end=end.isoformat(),
            bound_members=len(bindings),
            active_members=len(members),
            unique_matches=len(unique_matches),
            total_kills=sum(member.kills for member in members),
            total_elo_change=sum(member.elo_change or 0 for member in members),
            popular_maps=popular_maps,
            members=tuple(members),
        )
