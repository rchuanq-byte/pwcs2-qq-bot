"""Persistent daily message quota for group members without Steam bindings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from pwcs2_bot.commands import ParsedCommand
from pwcs2_bot.database import BindingRepository

_EXEMPT_ACTIONS = {
    "bind",
    "binding",
    "valorant_bind",
    "valorant_binding",
    "valorant_unbind",
    "help",
    "skin_add",
    "skin_remove",
    "skin_list",
    "skin_prices",
    "memory_add",
    "memory_list",
    "memory_delete",
}
_EXEMPT_ROLES = {"owner", "admin"}


@dataclass(frozen=True, slots=True)
class UnboundMessageDecision:
    count: int
    limit: int
    remaining: int
    should_remind: bool
    should_mute: bool
    mute_duration_seconds: int


class UnboundMessageGuard:
    def __init__(
        self,
        repository: BindingRepository,
        *,
        daily_limit: int = 10,
        mute_duration_seconds: int = 600,
        timezone_name: str = "Asia/Shanghai",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.daily_limit = daily_limit
        self.mute_duration_seconds = mute_duration_seconds
        self.timezone = ZoneInfo(timezone_name)
        self._now = now or (lambda: datetime.now(self.timezone))

    def record_if_restricted(
        self,
        *,
        group_id: str,
        qq_id: str,
        role: str,
        command: ParsedCommand | None,
    ) -> UnboundMessageDecision | None:
        if role.casefold() in _EXEMPT_ROLES:
            return None
        if command is not None and command.action in _EXEMPT_ACTIONS:
            return None
        if self.repository.get_by_qq(group_id, qq_id) is not None:
            return None

        current = self._now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=self.timezone)
        usage_date = current.astimezone(self.timezone).date().isoformat()
        count = self.repository.increment_unbound_message_count(
            group_id,
            qq_id,
            usage_date,
        )
        remaining = max(0, self.daily_limit - count)
        reminder_counts = {
            1,
            max(1, self.daily_limit // 2),
            max(1, self.daily_limit - 1),
        }
        return UnboundMessageDecision(
            count=count,
            limit=self.daily_limit,
            remaining=remaining,
            should_remind=count in reminder_counts or count >= self.daily_limit,
            should_mute=count >= self.daily_limit,
            mute_duration_seconds=self.mute_duration_seconds,
        )
