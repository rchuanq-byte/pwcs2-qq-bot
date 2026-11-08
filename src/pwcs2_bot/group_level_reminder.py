"""Daily grace-period enforcement for low-level or Steam-unbound members."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger

from pwcs2_bot.database import BindingRepository, GroupMemberCompliance

FetchGroupMembers = Callable[[str], Awaitable[list[dict[str, Any]]]]
RemoveMembers = Callable[[str, tuple[str, ...]], Awaitable[tuple[str, ...]]]

LEVEL_REASON = "level"
UNBOUND_REASON = "unbound"
_EXEMPT_ROLES = {"owner", "admin"}


@dataclass(frozen=True, slots=True)
class ComplianceReminder:
    user_id: str
    reasons: tuple[str, ...]
    remaining_days: int


SendReminder = Callable[
    [str, tuple[ComplianceReminder, ...]],
    Awaitable[None],
]


def _is_truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes"}
    return bool(value)


def _is_exempt_member(member: dict[str, Any]) -> bool:
    role = str(member.get("role") or "member").strip().casefold()
    return role in _EXEMPT_ROLES or _is_truthy(member.get("is_robot"))


def select_group_level_members(
    members: list[dict[str, Any]],
    *,
    target_level: int,
) -> tuple[str, ...]:
    """Return ordinary, numeric QQ IDs matching NapCat's group ``level`` field."""
    selected: set[str] = set()
    for member in members:
        if _is_exempt_member(member):
            continue
        try:
            level = int(str(member.get("level", "")).strip())
        except ValueError:
            continue
        user_id = str(member.get("user_id") or "").strip()
        if level == target_level and user_id.isdecimal():
            selected.add(user_id)
    return tuple(sorted(selected, key=int))


def select_unbound_group_members(
    members: list[dict[str, Any]],
    *,
    bound_user_ids: set[str],
) -> tuple[str, ...]:
    """Return ordinary, numeric, non-robot QQ IDs without a Steam binding."""
    selected: set[str] = set()
    for member in members:
        if _is_exempt_member(member):
            continue
        user_id = str(member.get("user_id") or "").strip()
        if user_id.isdecimal() and user_id not in bound_user_ids:
            selected.add(user_id)
    return tuple(sorted(selected, key=int))


def select_noncompliant_group_members(
    members: list[dict[str, Any]],
    *,
    bound_user_ids: set[str],
    target_level: int,
) -> dict[str, tuple[str, ...]]:
    """Select ordinary members violating either binding or group-level policy."""
    selected: dict[str, set[str]] = {}
    for user_id in select_group_level_members(members, target_level=target_level):
        selected.setdefault(user_id, set()).add(LEVEL_REASON)
    for user_id in select_unbound_group_members(
        members,
        bound_user_ids=bound_user_ids,
    ):
        selected.setdefault(user_id, set()).add(UNBOUND_REASON)
    return {
        user_id: tuple(sorted(reasons))
        for user_id, reasons in sorted(selected.items(), key=lambda item: int(item[0]))
    }


class GroupLevelReminderService:
    """Warn noncompliant members daily and remove them after a persisted grace period."""

    def __init__(
        self,
        repository: BindingRepository,
        fetch_group_members: FetchGroupMembers,
        send_reminder: SendReminder,
        remove_members: RemoveMembers | None = None,
        *,
        enabled: bool = True,
        send_time: str = "20:00",
        timezone_name: str = "Asia/Shanghai",
        target_level: int = 1,
        include_unbound: bool = True,
        removal_grace_days: int = 15,
        removal_max_per_day: int = 10,
        poll_interval_seconds: float = 300,
        initial_delay_seconds: float = 30,
        allowed_groups: list[str] | None = None,
    ) -> None:
        hour, minute = (int(part) for part in send_time.split(":"))
        self.repository = repository
        self.fetch_group_members = fetch_group_members
        self.send_reminder = send_reminder
        self.remove_members = remove_members
        self.enabled = enabled
        self.send_time = time(hour, minute)
        self.timezone = ZoneInfo(timezone_name)
        self.target_level = target_level
        self.include_unbound = include_unbound
        self.removal_grace_days = removal_grace_days
        self.removal_max_per_day = removal_max_per_day
        self.poll_interval_seconds = poll_interval_seconds
        self.initial_delay_seconds = initial_delay_seconds
        self.allowed_groups = set(allowed_groups or [])
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        if not self.enabled or await self._wait(self.initial_delay_seconds):
            return
        logger.info(
            "pwcs2: group compliance enforcement started "
            "(time={} timezone={} level={} include_unbound={} grace_days={})",
            self.send_time.strftime("%H:%M"),
            self.timezone.key,
            self.target_level,
            self.include_unbound,
            self.removal_grace_days,
        )
        while not self._stop_event.is_set():
            try:
                await self.run_due_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("pwcs2: unexpected group compliance enforcement failure")
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
        if local_now.time().replace(tzinfo=None) < self.send_time:
            return 0

        reminder_date = local_now.date()
        if self.allowed_groups:
            group_ids = self.allowed_groups
        else:
            group_ids = {
                binding.group_id for binding in self.repository.list_bindings()
            } | set(self.repository.list_group_member_compliance_groups())

        acted = 0
        for group_id in sorted(group_ids):
            if self.repository.is_group_level_reminder_processed(
                group_id, reminder_date.isoformat()
            ):
                continue
            try:
                members = await self.fetch_group_members(group_id)
                bound_user_ids = {
                    binding.qq_id
                    for binding in self.repository.list_group_bindings(group_id)
                }
                noncompliant = select_noncompliant_group_members(
                    members,
                    bound_user_ids=bound_user_ids if self.include_unbound else {
                        str(member.get("user_id"))
                        for member in members
                        if member.get("user_id") is not None
                    },
                    target_level=self.target_level,
                )
                records = self.repository.reconcile_group_member_compliance(
                    group_id,
                    noncompliant,
                    check_date=reminder_date.isoformat(),
                )
                reminders = self._build_reminders(records, reminder_date)
                due_ids = tuple(
                    item.user_id
                    for item in reminders
                    if item.remaining_days == 0
                )[: self.removal_max_per_day]
                resolved_ids: set[str] = set()
                if due_ids and self.remove_members is not None:
                    resolved_ids.update(await self.remove_members(group_id, due_ids))
                    for user_id in resolved_ids:
                        self.repository.clear_group_member_compliance(group_id, user_id)
                pending_reminders = tuple(
                    item for item in reminders if item.user_id not in resolved_ids
                )
                if pending_reminders:
                    await self.send_reminder(group_id, pending_reminders)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "pwcs2: group compliance check failed group={} date={}: {}",
                    group_id,
                    reminder_date.isoformat(),
                    exc,
                )
                continue
            self.repository.mark_group_level_reminder_processed(
                group_id,
                reminder_date.isoformat(),
                target_count=len(noncompliant),
            )
            if noncompliant:
                acted += 1
        return acted

    def _build_reminders(
        self,
        records: tuple[GroupMemberCompliance, ...],
        reminder_date: date,
    ) -> tuple[ComplianceReminder, ...]:
        reminders: list[ComplianceReminder] = []
        for record in records:
            first_date = date.fromisoformat(record.first_noncompliant_date)
            elapsed_days = max(0, (reminder_date - first_date).days)
            reminders.append(
                ComplianceReminder(
                    user_id=record.qq_id,
                    reasons=record.reasons,
                    remaining_days=max(0, self.removal_grace_days - elapsed_days),
                )
            )
        return tuple(reminders)
