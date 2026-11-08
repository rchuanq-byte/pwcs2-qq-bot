"""Generate a weekly group chemistry network from recent Perfect World matches."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from loguru import logger

from pwcs2_bot.chemistry import ChemistryNetworkSnapshot, build_chemistry_network
from pwcs2_bot.database import Binding, BindingRepository
from pwcs2_bot.game_identity import resolve_game_profile
from pwcs2_bot.perfectworld import PerfectWorldApiError, PerfectWorldClient

SendNetwork = Callable[[str, ChemistryNetworkSnapshot], Awaitable[None]]


class WeeklyChemistryService:
    def __init__(
        self,
        repository: BindingRepository,
        api: PerfectWorldClient,
        send_network: SendNetwork,
        *,
        enabled: bool = True,
        send_time: str = "21:30",
        weekday: int = 6,
        timezone_name: str = "Asia/Shanghai",
        poll_interval_seconds: float = 300,
        initial_delay_seconds: float = 60,
        match_page_size: int = 50,
        allowed_groups: list[str] | None = None,
    ) -> None:
        hour, minute = (int(part) for part in send_time.split(":", 1))
        if not 0 <= weekday <= 6:
            raise ValueError("chemistry weekday must be between 0 and 6")
        self.repository = repository
        self.api = api
        self.send_network = send_network
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
            "pwcs2: weekly chemistry started (weekday={} time={} timezone={})",
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
                logger.exception("pwcs2: unexpected weekly chemistry failure")
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
        week_start = (local_now.date() - timedelta(days=local_now.weekday())).isoformat()
        groups: dict[str, list[Binding]] = {}
        for binding in self.repository.list_bindings():
            if self.allowed_groups and binding.group_id not in self.allowed_groups:
                continue
            groups.setdefault(binding.group_id, []).append(binding)

        sent = 0
        for group_id, bindings in sorted(groups.items()):
            if self.repository.is_weekly_chemistry_processed(group_id, week_start):
                continue
            if len(bindings) < 2:
                self.repository.mark_weekly_chemistry_processed(
                    group_id,
                    week_start,
                    was_sent=False,
                )
                continue
            try:
                snapshot = await self._build_group_snapshot(
                    group_id,
                    bindings,
                    week_start=week_start,
                )
            except (PerfectWorldApiError, ValueError) as exc:
                logger.warning(
                    "pwcs2: weekly chemistry data incomplete group={}: {}",
                    group_id,
                    exc,
                )
                continue
            if not snapshot.pairs:
                self.repository.mark_weekly_chemistry_processed(
                    group_id,
                    week_start,
                    was_sent=False,
                )
                continue
            try:
                await self.send_network(group_id, snapshot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "pwcs2: weekly chemistry send failed group={}: {}",
                    group_id,
                    exc,
                )
                continue
            self.repository.mark_weekly_chemistry_processed(
                group_id,
                week_start,
                was_sent=True,
            )
            sent += 1
        return sent

    async def _build_group_snapshot(
        self,
        group_id: str,
        bindings: list[Binding],
        *,
        week_start: str,
    ) -> ChemistryNetworkSnapshot:
        semaphore = asyncio.Semaphore(4)

        async def fetch(binding: Binding) -> tuple[str, list[dict], str]:
            async with semaphore:
                matches = await self.api.match_list(
                    binding.steam_id,
                    page_size=self.match_page_size,
                )
                profile = (
                    await resolve_game_profile(self.api, binding.steam_id, matches)
                    if matches else {"name": binding.steam_id}
                )
            return binding.qq_id, matches, profile["name"]

        results = await asyncio.gather(
            *(fetch(binding) for binding in bindings),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise PerfectWorldApiError(
                f"{len(failures)}/{len(bindings)} 名成员比赛记录查询失败"
            )
        matches_by_qq = {
            result[0]: result[1]
            for result in results
            if isinstance(result, tuple)
        }
        names = {result[0]: result[2] for result in results if isinstance(result, tuple)}
        return build_chemistry_network(
            matches_by_qq,
            names_by_qq=names,
            week_start=week_start,
        )
