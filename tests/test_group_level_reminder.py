from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pwcs2_bot.database import BindingRepository
from pwcs2_bot.group_level_reminder import (
    LEVEL_REASON,
    UNBOUND_REASON,
    ComplianceReminder,
    GroupLevelReminderService,
    select_group_level_members,
    select_noncompliant_group_members,
    select_unbound_group_members,
)

TZ = ZoneInfo("Asia/Shanghai")


def test_select_group_level_members_excludes_privileged_members_and_robots() -> None:
    members = [
        {"user_id": 300, "level": "1", "qq_level": 88},
        {"user_id": "100", "level": 1, "qq_level": 0},
        {"user_id": 200, "level": "2", "qq_level": 1},
        {"user_id": 400, "level": 1, "is_robot": True},
        {"user_id": 500, "level": 1, "role": "admin"},
        {"user_id": 600, "level": 1, "role": "owner"},
        {"user_id": "invalid", "level": 1},
    ]

    assert select_group_level_members(members, target_level=1) == ("100", "300")


def test_select_unbound_members_excludes_bound_privileged_and_robot_accounts() -> None:
    members = [
        {"user_id": 100, "level": 1},
        {"user_id": 200, "level": 2},
        {"user_id": 300, "level": 3, "is_robot": True},
        {"user_id": 400, "level": 1, "role": "admin"},
        {"user_id": 500, "level": 1, "role": "owner"},
        {"user_id": "invalid", "level": 2},
    ]

    assert select_unbound_group_members(
        members,
        bound_user_ids={"100"},
    ) == ("200",)


def test_select_noncompliant_members_uses_or_policy_and_reason_union() -> None:
    members = [
        {"user_id": 100, "level": 1},
        {"user_id": 200, "level": 2},
        {"user_id": 300, "level": 1},
        {"user_id": 400, "level": 1, "role": "admin"},
        {"user_id": 500, "level": 1, "is_robot": True},
        {"user_id": 600, "level": 2},
    ]

    assert select_noncompliant_group_members(
        members,
        bound_user_ids={"100", "600"},
        target_level=1,
    ) == {
        "100": (LEVEL_REASON,),
        "200": (UNBOUND_REASON,),
        "300": (LEVEL_REASON, UNBOUND_REASON),
    }


async def test_first_fifteen_days_warn_then_remove_at_full_grace_boundary(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bindings.db"
    repository = BindingRepository(path)
    repository.bind("987654", "100", "76561198000000001")
    fetches: list[str] = []
    sent: list[tuple[str, tuple[ComplianceReminder, ...]]] = []
    removals: list[tuple[str, tuple[str, ...]]] = []

    async def fetch(group_id: str) -> list[dict]:
        fetches.append(group_id)
        return [
            {"user_id": 100, "level": 1},
            {"user_id": 200, "level": 2},
            {"user_id": 300, "level": "1"},
        ]

    async def send(
        group_id: str,
        reminders: tuple[ComplianceReminder, ...],
    ) -> None:
        sent.append((group_id, reminders))

    async def remove(
        group_id: str,
        user_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        removals.append((group_id, user_ids))
        return user_ids

    service = GroupLevelReminderService(repository, fetch, send, remove)
    first_check = datetime(2026, 7, 1, 20, 1, tzinfo=TZ)

    for elapsed_days in range(15):
        now = first_check + timedelta(days=elapsed_days)
        assert await service.run_due_once(now) == 1
        assert await service.run_due_once(now) == 0
        assert sent[-1][0] == "987654"
        assert [item.user_id for item in sent[-1][1]] == ["100", "200", "300"]
        assert {item.remaining_days for item in sent[-1][1]} == {
            15 - elapsed_days
        }
        assert removals == []

    assert sent[-2][1][0].remaining_days == 2
    assert sent[-1][1][0].remaining_days == 1

    removal_time = first_check + timedelta(days=15)
    assert await service.run_due_once(removal_time) == 1
    assert removals == [("987654", ("100", "200", "300"))]
    assert len(fetches) == 16
    assert len(sent) == 15
    for user_id in ("100", "200", "300"):
        assert repository.get_group_member_compliance("987654", user_id) is None
    assert BindingRepository(path).is_group_level_reminder_processed(
        "987654", "2026-07-16"
    )


async def test_becoming_compliant_resets_the_grace_period(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.bind("987654", "100", "76561198000000001")
    members = [{"user_id": 100, "level": 1}]
    sent: list[tuple[ComplianceReminder, ...]] = []

    async def fetch(_group_id: str) -> list[dict]:
        return members

    async def send(
        _group_id: str,
        reminders: tuple[ComplianceReminder, ...],
    ) -> None:
        sent.append(reminders)

    service = GroupLevelReminderService(repository, fetch, send)

    assert await service.run_due_once(datetime(2026, 7, 1, 20, 1, tzinfo=TZ)) == 1
    first = repository.get_group_member_compliance("987654", "100")
    assert first is not None and first.first_noncompliant_date == "2026-07-01"

    members[0]["level"] = 2
    assert await service.run_due_once(datetime(2026, 7, 2, 20, 1, tzinfo=TZ)) == 0
    assert repository.get_group_member_compliance("987654", "100") is None

    members[0]["level"] = 1
    assert await service.run_due_once(datetime(2026, 7, 3, 20, 1, tzinfo=TZ)) == 1
    restarted = repository.get_group_member_compliance("987654", "100")
    assert restarted is not None
    assert restarted.first_noncompliant_date == "2026-07-03"
    assert sent[-1] == (
        ComplianceReminder("100", (LEVEL_REASON,), remaining_days=15),
    )


async def test_observation_gap_restarts_the_grace_period(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.bind("987654", "100", "76561198000000001")
    sent: list[tuple[ComplianceReminder, ...]] = []

    async def fetch(_group_id: str) -> list[dict]:
        return [{"user_id": 100, "level": 1}]

    async def send(
        _group_id: str,
        reminders: tuple[ComplianceReminder, ...],
    ) -> None:
        sent.append(reminders)

    service = GroupLevelReminderService(repository, fetch, send)
    assert await service.run_due_once(datetime(2026, 7, 1, 20, 1, tzinfo=TZ)) == 1
    assert await service.run_due_once(datetime(2026, 7, 3, 20, 1, tzinfo=TZ)) == 1

    record = repository.get_group_member_compliance("987654", "100")
    assert record is not None
    assert record.first_noncompliant_date == "2026-07-03"
    assert sent[-1] == (
        ComplianceReminder("100", (LEVEL_REASON,), remaining_days=15),
    )


async def test_failed_removal_is_retained_and_retried_next_day(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.bind("987654", "100", "76561198000000001")
    attempts = 0
    sent: list[tuple[ComplianceReminder, ...]] = []

    async def fetch(_group_id: str) -> list[dict]:
        return [{"user_id": 100, "level": 1}]

    async def send(
        _group_id: str,
        reminders: tuple[ComplianceReminder, ...],
    ) -> None:
        sent.append(reminders)

    async def remove(
        _group_id: str,
        user_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        nonlocal attempts
        attempts += 1
        return user_ids if attempts > 1 else ()

    service = GroupLevelReminderService(repository, fetch, send, remove)
    first_check = datetime(2026, 7, 1, 20, 1, tzinfo=TZ)
    for elapsed_days in range(15):
        await service.run_due_once(first_check + timedelta(days=elapsed_days))
    await service.run_due_once(datetime(2026, 7, 16, 20, 1, tzinfo=TZ))

    assert attempts == 1
    assert sent[-1] == (
        ComplianceReminder("100", (LEVEL_REASON,), remaining_days=0),
    )
    assert repository.get_group_member_compliance("987654", "100") is not None

    await service.run_due_once(datetime(2026, 7, 17, 20, 1, tzinfo=TZ))
    assert attempts == 2
    assert repository.get_group_member_compliance("987654", "100") is None


async def test_daily_removal_limit_defers_excess_members(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    active_ids = {str(100 + index) for index in range(12)}
    removals: list[tuple[str, ...]] = []
    sent: list[tuple[ComplianceReminder, ...]] = []

    async def fetch(_group_id: str) -> list[dict]:
        return [
            {"user_id": int(user_id), "level": 2}
            for user_id in sorted(active_ids, key=int)
        ]

    async def send(
        _group_id: str,
        reminders: tuple[ComplianceReminder, ...],
    ) -> None:
        sent.append(reminders)

    async def remove(
        _group_id: str,
        user_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        removals.append(user_ids)
        active_ids.difference_update(user_ids)
        return user_ids

    service = GroupLevelReminderService(
        repository,
        fetch,
        send,
        remove,
        allowed_groups=["987654"],
        removal_max_per_day=10,
    )
    first_check = datetime(2026, 7, 1, 20, 1, tzinfo=TZ)
    for elapsed_days in range(15):
        await service.run_due_once(first_check + timedelta(days=elapsed_days))
    await service.run_due_once(datetime(2026, 7, 16, 20, 1, tzinfo=TZ))

    assert len(removals[0]) == 10
    assert [item.user_id for item in sent[-1]] == ["110", "111"]
    assert {item.remaining_days for item in sent[-1]} == {0}

    await service.run_due_once(datetime(2026, 7, 17, 20, 1, tzinfo=TZ))
    assert removals[1] == ("110", "111")
    assert active_ids == set()


async def test_empty_or_failed_check_does_not_spam(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.bind("987654", "100", "76561198000000001")
    attempts = 0

    async def fetch(_group_id: str) -> list[dict]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("NapCat offline")
        return [{"user_id": 100, "level": 2}]

    async def send(
        _group_id: str,
        _reminders: tuple[ComplianceReminder, ...],
    ) -> None:
        raise AssertionError("empty target list must not be sent")

    service = GroupLevelReminderService(repository, fetch, send)
    now = datetime(2026, 7, 2, 20, 1, tzinfo=TZ)

    assert await service.run_due_once(now) == 0
    assert not repository.is_group_level_reminder_processed("987654", "2026-07-02")
    assert await service.run_due_once(now) == 0
    assert await service.run_due_once(now) == 0
    assert attempts == 2
    assert repository.is_group_level_reminder_processed("987654", "2026-07-02")


async def test_reminder_waits_until_configured_time(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.bind("987654", "100", "76561198000000001")

    async def unexpected_fetch(_group_id: str) -> list[dict]:
        raise AssertionError("member list must not be fetched before the deadline")

    service = GroupLevelReminderService(repository, unexpected_fetch, lambda *_: None)

    assert await service.run_due_once(datetime(2026, 7, 2, 19, 59, tzinfo=TZ)) == 0
