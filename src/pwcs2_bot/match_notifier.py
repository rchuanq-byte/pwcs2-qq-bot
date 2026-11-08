"""Poll completed PVP and official matches and notify each bound QQ group once."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from pwcs2_bot.database import BindingRepository
from pwcs2_bot.match_formatting import (
    MatchSnapshot,
    build_match_snapshot,
    match_sort_key,
)
from pwcs2_bot.perfectworld import PerfectWorldApiError, PerfectWorldClient

SendMatch = Callable[[str, MatchSnapshot], Awaitable[None]]
ObserveMatches = Callable[[str, str, list[dict[str, Any]], int], Awaitable[None]]


class MatchNotifier:
    def __init__(
        self,
        repository: BindingRepository,
        api: PerfectWorldClient,
        send_match: SendMatch,
        *,
        poll_interval_seconds: float = 180,
        initial_delay_seconds: float = 15,
        page_size: int = 5,
        max_notifications_per_poll: int = 3,
        allowed_groups: list[str] | None = None,
        observe_matches: ObserveMatches | None = None,
    ) -> None:
        self.repository = repository
        self.api = api
        self.send_match = send_match
        self.poll_interval_seconds = poll_interval_seconds
        self.initial_delay_seconds = initial_delay_seconds
        self.page_size = page_size
        self.max_notifications_per_poll = max_notifications_per_poll
        self.allowed_groups = set(allowed_groups or [])
        self.observe_matches = observe_matches
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        if await self._wait(self.initial_delay_seconds):
            return
        logger.info(
            "pwcs2: match notifier started (interval={}s)",
            self.poll_interval_seconds,
        )
        while not self._stop_event.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("pwcs2: unexpected match notifier failure")
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

    async def poll_once(self) -> None:
        groups_by_steam: dict[str, set[str]] = defaultdict(set)
        member_ids_by_group: dict[str, set[str]] = defaultdict(set)
        for binding in self.repository.list_bindings():
            if self.allowed_groups and binding.group_id not in self.allowed_groups:
                continue
            groups_by_steam[binding.steam_id].add(binding.group_id)
            member_ids_by_group[binding.group_id].add(binding.steam_id)

        detail_cache: dict[tuple[str, int], dict[str, Any] | None] = {}
        sent_counts: dict[str, int] = defaultdict(int)
        failed_groups: set[str] = set()

        for steam_id, group_ids in groups_by_steam.items():
            if self._stop_event.is_set():
                return
            source_batches: list[
                tuple[int, dict[str, list[dict[str, Any]]]]
            ] = []
            for data_source, fetch_matches in (
                (3, self.api.match_list),
                (1, self.api.official_match_list),
            ):
                try:
                    matches = await fetch_matches(
                        steam_id,
                        page_size=self.page_size,
                    )
                except (PerfectWorldApiError, ValueError) as exc:
                    logger.warning(
                        "pwcs2: match poll failed for {} source={}: {}",
                        steam_id,
                        data_source,
                        exc,
                    )
                    continue

                matches = self._valid_matches(matches)
                if self.observe_matches is not None:
                    for group_id in sorted(group_ids):
                        try:
                            await self.observe_matches(
                                group_id,
                                steam_id,
                                matches,
                                data_source,
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            logger.warning(
                                "pwcs2: match observer failed "
                                "group={} steam={} source={}: {}",
                                group_id,
                                steam_id,
                                data_source,
                                exc,
                            )

                match_ids = [str(match["matchId"]) for match in matches]
                initialized_groups: set[str] = set()
                for group_id in group_ids:
                    if self.repository.initialize_match_source_tracking(
                        group_id,
                        steam_id,
                        data_source,
                        match_ids,
                    ):
                        initialized_groups.add(group_id)
                        logger.info(
                            "pwcs2: initialized match baseline "
                            "group={} steam={} source={} matches={}",
                            group_id,
                            steam_id,
                            data_source,
                            len(match_ids),
                        )

                pending_by_group: dict[str, list[dict[str, Any]]] = {}
                for group_id in group_ids - initialized_groups:
                    if group_id in failed_groups:
                        continue
                    initialized_at = (
                        self.repository.match_source_tracking_initialized_at(
                            group_id,
                            steam_id,
                            data_source,
                        )
                    )
                    unseen = [
                        match
                        for match in matches
                        if not self.repository.is_group_source_match_notified(
                            group_id,
                            data_source,
                            str(match["matchId"]),
                        )
                        and self._ended_after_tracking_started(match, initialized_at)
                    ]
                    unseen.sort(key=match_sort_key)
                    if unseen:
                        pending_by_group[group_id] = unseen
                if pending_by_group:
                    source_batches.append((data_source, pending_by_group))

            if not source_batches:
                continue

            profile: dict[str, Any] = {}
            try:
                profile = await self.api.detail_stats(steam_id)
            except PerfectWorldApiError as exc:
                logger.warning(
                    "pwcs2: profile enrichment failed for match notification {}: {}",
                    steam_id,
                    exc,
                )

            for data_source, pending_by_group in source_batches:
                for group_id, pending in pending_by_group.items():
                    if group_id in failed_groups:
                        continue
                    for raw_match in pending:
                        if sent_counts[group_id] >= self.max_notifications_per_poll:
                            logger.info(
                                "pwcs2: deferred match notifications "
                                "group={} steam={} source={}",
                                group_id,
                                steam_id,
                                data_source,
                            )
                            break
                        match_id = str(raw_match["matchId"])
                        detail_key = (match_id, data_source)
                        if detail_key not in detail_cache:
                            try:
                                detail_cache[detail_key] = await self.api.match_detail(
                                    match_id,
                                    data_source=data_source,
                                )
                            except (PerfectWorldApiError, ValueError) as exc:
                                detail_cache[detail_key] = None
                                logger.warning(
                                    "pwcs2: match detail enrichment failed "
                                    "match={} source={}: {}",
                                    match_id,
                                    data_source,
                                    exc,
                                )
                        detail = detail_cache[detail_key]
                        if not self._detail_ready(detail):
                            logger.info(
                                "pwcs2: match detail not ready; retrying later "
                                "group={} steam={} match={} source={}",
                                group_id,
                                steam_id,
                                match_id,
                                data_source,
                            )
                            continue
                        snapshot = build_match_snapshot(
                            raw_match,
                            steam_id=steam_id,
                            profile=profile,
                            detail=detail,
                            group_member_steam_ids=member_ids_by_group[group_id],
                        )
                        try:
                            await self.send_match(group_id, snapshot)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            # A failed QQ send must not fan out into another
                            # attempt for every bound player/source this poll.
                            # Leave matches pending and retry on the next poll;
                            # other groups and match observers still proceed.
                            failed_groups.add(group_id)
                            logger.warning(
                                "pwcs2: match notification failed "
                                "group={} match={} source={}: {}",
                                group_id,
                                snapshot.match_id,
                                data_source,
                                exc,
                            )
                            break
                        self.repository.mark_source_match_notified(
                            group_id,
                            steam_id,
                            data_source,
                            snapshot.match_id,
                        )
                        sent_counts[group_id] += 1

    @staticmethod
    def _valid_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        valid: list[dict[str, Any]] = []
        for match in matches:
            match_id = str(match.get("matchId") or "").strip()
            if not match_id or match_id in seen:
                continue
            seen.add(match_id)
            valid.append(match)
        return valid

    @staticmethod
    def _detail_ready(detail: dict[str, Any] | None) -> bool:
        if not isinstance(detail, dict):
            return False
        players = detail.get("players")
        return isinstance(players, list) and len(players) >= 10

    @staticmethod
    def _ended_after_tracking_started(
        match: dict[str, Any],
        initialized_at: str | None,
    ) -> bool:
        if not initialized_at:
            return True
        timestamp = match_sort_key(match)[0]
        if timestamp <= 0:
            return True
        try:
            initialized = datetime.fromisoformat(initialized_at)
        except ValueError:
            return True
        if initialized.tzinfo is None:
            initialized = initialized.replace(tzinfo=UTC)
        return timestamp > int(initialized.timestamp())
