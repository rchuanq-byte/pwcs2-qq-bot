"""Periodically reconcile persisted group-card locks against NapCat state."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from loguru import logger

from pwcs2_bot.database import BindingRepository

FetchGroupMembers = Callable[[str], Awaitable[list[dict[str, object]]]]
RestoreGroupCard = Callable[[str, str], Awaitable[bool]]
GetBotQQ = Callable[[], str]


class GroupCardLockReconciler:
    """Use notices for low latency, and polling as the authoritative safety net."""

    def __init__(
        self,
        repository: BindingRepository,
        fetch_group_members: FetchGroupMembers,
        restore_group_card: RestoreGroupCard,
        get_bot_qq: GetBotQQ,
        *,
        interval_seconds: float = 300,
        initial_delay_seconds: float = 15,
        allowed_groups: list[str] | None = None,
    ) -> None:
        self.repository = repository
        self.fetch_group_members = fetch_group_members
        self.restore_group_card = restore_group_card
        self.get_bot_qq = get_bot_qq
        self.interval_seconds = interval_seconds
        self.initial_delay_seconds = initial_delay_seconds
        self.allowed_groups = {
            str(group_id) for group_id in (allowed_groups or []) if str(group_id).isdecimal()
        }
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        # Channel instances can be restarted in-process during reconnect tests.
        self._stop_event.clear()
        if await self._wait(self.initial_delay_seconds):
            return
        logger.info(
            "pwcs2: group-card reconciliation started (interval={}s)",
            self.interval_seconds,
        )
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("pwcs2: unexpected group-card reconciliation failure")
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

    async def run_once(self) -> tuple[tuple[str, str], ...]:
        """Restore every visible drift and return the successfully restored members."""
        group_ids = self.allowed_groups or set(
            self.repository.list_group_card_lock_group_ids()
        )
        bot_qq = self.get_bot_qq().strip()
        restored: list[tuple[str, str]] = []
        for group_id in sorted(group_ids):
            try:
                # This fresh read also establishes first-seen baselines without
                # overwriting names explicitly chosen through the admin command.
                members = await self.fetch_group_members(group_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "pwcs2: group-card member query failed group={}: {}",
                    group_id,
                    exc,
                )
                continue

            for member in members:
                qq_id = str(member.get("user_id") or "")
                role = str(member.get("role") or "").strip().casefold()
                if (
                    not qq_id.isdecimal()
                    or qq_id == bot_qq
                    or role not in {"member", "admin"}
                    or _onebot_true(member.get("is_robot", False))
                ):
                    continue
                desired = self.repository.get_group_card_lock(group_id, qq_id)
                if desired is None:
                    continue
                current = str(member.get("card") or member.get("nickname") or qq_id)
                if current == desired:
                    continue
                try:
                    if await self.restore_group_card(group_id, qq_id):
                        restored.append((group_id, qq_id))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # A transient failure for one member must not prevent the
                    # remaining locks from being checked during this pass.
                    logger.warning(
                        "pwcs2: group-card reconcile failed group={} qq={}: {}",
                        group_id,
                        qq_id,
                        exc,
                    )
        if restored:
            logger.info(
                "pwcs2: group-card reconciliation restored {} member(s)",
                len(restored),
            )
        return tuple(restored)


def _onebot_true(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes"}
