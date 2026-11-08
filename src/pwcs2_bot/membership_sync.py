"""Reconcile persisted QQ bindings against authoritative group member lists."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from loguru import logger

from pwcs2_bot.database import Binding, BindingRepository

FetchGroupMemberIds = Callable[[str], Awaitable[set[str]]]
RemoveMemberData = Callable[[str, str], None]


class MembershipReconciler:
    def __init__(
        self,
        repository: BindingRepository,
        fetch_group_member_ids: FetchGroupMemberIds,
        remove_member_data: RemoveMemberData | None = None,
        *,
        interval_seconds: float = 21600,
        initial_delay_seconds: float = 45,
        allowed_groups: list[str] | None = None,
    ) -> None:
        self.repository = repository
        self.fetch_group_member_ids = fetch_group_member_ids
        self.remove_member_data = remove_member_data
        self.interval_seconds = interval_seconds
        self.initial_delay_seconds = initial_delay_seconds
        self.allowed_groups = set(allowed_groups or [])
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        if await self._wait(self.initial_delay_seconds):
            return
        logger.info(
            "pwcs2: membership reconciliation started (interval={}s)",
            self.interval_seconds,
        )
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("pwcs2: unexpected membership reconciliation failure")
            if await self._wait(self.interval_seconds):
                return

    async def _wait(self, seconds: float) -> bool:
        if seconds <= 0:
            return self._stop_event.is_set()
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except TimeoutError:
            return False
        return True

    async def run_once(self) -> tuple[Binding, ...]:
        group_ids = {
            group_id
            for group_id in self.repository.list_binding_group_ids()
            if not self.allowed_groups or group_id in self.allowed_groups
        }
        removed: list[Binding] = []
        for group_id in sorted(group_ids):
            try:
                member_ids = await self.fetch_group_member_ids(group_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "pwcs2: membership query failed group={}: {}",
                    group_id,
                    exc,
                )
                continue
            if not member_ids:
                logger.warning(
                    "pwcs2: refusing empty member list for group={}",
                    group_id,
                )
                continue
            previously_bound = set(self.repository.list_bound_qq_ids(group_id))
            removed_qq_ids = previously_bound - member_ids
            stale = self.repository.remove_members_not_in_group(group_id, member_ids)
            if removed_qq_ids:
                if self.remove_member_data is not None:
                    for qq_id in sorted(removed_qq_ids):
                        self.remove_member_data(group_id, qq_id)
                logger.info(
                    "pwcs2: removed {} stale bound members from group={}",
                    len(removed_qq_ids),
                    group_id,
                )
            if stale:
                removed.extend(stale)
        return tuple(removed)
