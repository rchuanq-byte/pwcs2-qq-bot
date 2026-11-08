from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pwcs2_bot.commands import ParsedCommand
from pwcs2_bot.database import BindingRepository
from pwcs2_bot.unbound_guard import UnboundMessageGuard

TZ = ZoneInfo("Asia/Shanghai")


def test_unbound_guard_counts_daily_and_marks_reminder_and_mute_points(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    now = [datetime(2026, 7, 2, 12, 0, tzinfo=TZ)]
    guard = UnboundMessageGuard(
        repository,
        daily_limit=10,
        mute_duration_seconds=600,
        now=lambda: now[0],
    )

    decisions = [
        guard.record_if_restricted(
            group_id="group",
            qq_id="100001",
            role="member",
            command=None,
        )
        for _ in range(10)
    ]

    assert all(decision is not None for decision in decisions)
    assert [decision.count for decision in decisions if decision is not None] == list(
        range(1, 11)
    )
    assert [
        decision.count
        for decision in decisions
        if decision is not None and decision.should_remind
    ] == [1, 5, 9, 10]
    assert decisions[-1] is not None and decisions[-1].should_mute
    assert repository.get_unbound_message_count("group", "100001", "2026-07-02") == 10

    now[0] = datetime(2026, 7, 3, 0, 1, tzinfo=TZ)
    next_day = guard.record_if_restricted(
        group_id="group",
        qq_id="100001",
        role="member",
        command=None,
    )
    assert next_day is not None and next_day.count == 1
    assert repository.get_unbound_message_count("group", "100001", "2026-07-02") == 0


def test_unbound_guard_exempts_bound_members_admins_and_safe_commands(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.bind("group", "bound", "76561198000000001")
    guard = UnboundMessageGuard(repository, daily_limit=3)

    assert (
        guard.record_if_restricted(
            group_id="group", qq_id="bound", role="member", command=None
        )
        is None
    )
    assert (
        guard.record_if_restricted(
            group_id="group", qq_id="admin", role="admin", command=None
        )
        is None
    )
    for action in (
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
    ):
        assert (
            guard.record_if_restricted(
                group_id="group",
                qq_id="unbound",
                role="member",
                command=ParsedCommand(action),
            )
            is None
        )


def test_valorant_binding_does_not_bypass_steam_unbound_guard(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.increment_unbound_message_count(
        "group",
        "valorant-only",
        "2026-07-31",
    )
    repository.bind_valorant("group", "valorant-only", "ExamplePlayer#12345")
    assert (
        repository.get_unbound_message_count(
            "group",
            "valorant-only",
            "2026-07-31",
        )
        == 1
    )
    guard = UnboundMessageGuard(
        repository,
        daily_limit=3,
        now=lambda: datetime(2026, 7, 31, 12, 0, tzinfo=TZ),
    )

    ordinary_message = guard.record_if_restricted(
        group_id="group",
        qq_id="valorant-only",
        role="member",
        command=None,
    )
    valorant_query = guard.record_if_restricted(
        group_id="group",
        qq_id="valorant-only",
        role="member",
        command=ParsedCommand("valorant_stats"),
    )

    assert ordinary_message is not None and ordinary_message.count == 2
    assert valorant_query is not None and valorant_query.count == 3
    assert valorant_query.should_mute is True
    assert (
        repository.get_unbound_message_count(
            "group",
            "valorant-only",
            "2026-07-31",
        )
        == 3
    )
