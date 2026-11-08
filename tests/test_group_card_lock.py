import asyncio
from pathlib import Path

import pytest
from nanobot.bus.queue import MessageBus
from nanobot.channels.napcat import NapcatChannel

from pwcs2_bot.channel import PWCS2Channel
from pwcs2_bot.database import BindingRepository
from pwcs2_bot.group_card_lock import GroupCardLockReconciler


async def test_reconciler_restores_only_drifted_locked_card(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.set_group_card_lock(
        "987654", "100001", "Locked One", updated_by_qq="admin"
    )
    repository.set_group_card_lock(
        "987654", "100002", "Locked Two", updated_by_qq="admin"
    )
    restored: list[tuple[str, str]] = []

    async def fetch(group_id: str) -> list[dict[str, object]]:
        assert group_id == "987654"
        return [
            {
                "user_id": 100001,
                "role": "member",
                "card": "Member changed this",
            },
            {"user_id": 100002, "role": "admin", "card": "Locked Two"},
        ]

    async def restore(group_id: str, qq_id: str) -> bool:
        restored.append((group_id, qq_id))
        return True

    reconciler = GroupCardLockReconciler(
        repository,
        fetch,
        restore,
        lambda: "999999",
    )

    assert await reconciler.run_once() == (("987654", "100001"),)
    assert restored == [("987654", "100001")]


async def test_reconciler_does_not_write_when_cards_have_not_drifted(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.set_group_card_lock(
        "987654", "100001", "Still Locked", updated_by_qq="admin"
    )
    restored: list[tuple[str, str]] = []

    async def fetch(_group_id: str) -> list[dict[str, object]]:
        return [{"user_id": "100001", "role": "member", "card": "Still Locked"}]

    async def restore(group_id: str, qq_id: str) -> bool:
        restored.append((group_id, qq_id))
        return True

    reconciler = GroupCardLockReconciler(
        repository,
        fetch,
        restore,
        lambda: "999999",
    )

    assert await reconciler.run_once() == ()
    assert restored == []


async def test_reconciler_safely_skips_invalid_self_privileged_and_robot_members(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    for qq_id in ("999999", "100001", "100002", "100003"):
        repository.set_group_card_lock(
            "987654", qq_id, f"Locked {qq_id}", updated_by_qq="admin"
        )
    restored: list[tuple[str, str]] = []

    async def fetch(_group_id: str) -> list[dict[str, object]]:
        return [
            {"user_id": "not-a-qq", "role": "member", "card": "Drifted"},
            {"user_id": "999999", "role": "member", "card": "Drifted"},
            {"user_id": "100001", "role": "owner", "card": "Drifted"},
            {"user_id": "100002", "role": "unknown", "card": "Drifted"},
            {
                "user_id": "100003",
                "role": "member",
                "is_robot": "true",
                "card": "Drifted",
            },
        ]

    async def restore(group_id: str, qq_id: str) -> bool:
        restored.append((group_id, qq_id))
        return True

    reconciler = GroupCardLockReconciler(
        repository,
        fetch,
        restore,
        lambda: "999999",
    )

    assert await reconciler.run_once() == ()
    assert restored == []


async def test_reconciler_continues_after_one_member_restore_fails(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    for qq_id in ("100001", "100002"):
        repository.set_group_card_lock(
            "987654", qq_id, f"Locked {qq_id}", updated_by_qq="admin"
        )
    attempted: list[str] = []

    async def fetch(_group_id: str) -> list[dict[str, object]]:
        return [
            {"user_id": "100001", "role": "member", "card": "Drifted One"},
            {"user_id": "100002", "role": "member", "card": "Drifted Two"},
        ]

    async def restore(_group_id: str, qq_id: str) -> bool:
        attempted.append(qq_id)
        if qq_id == "100001":
            raise RuntimeError("temporary OneBot failure")
        return True

    reconciler = GroupCardLockReconciler(
        repository,
        fetch,
        restore,
        lambda: "999999",
    )

    assert await reconciler.run_once() == (("987654", "100002"),)
    assert attempted == ["100001", "100002"]


async def test_reconciler_runs_at_startup_and_then_periodically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")

    async def fetch(_group_id: str) -> list[dict[str, object]]:
        return []

    async def restore(_group_id: str, _qq_id: str) -> bool:
        return True

    reconciler = GroupCardLockReconciler(
        repository,
        fetch,
        restore,
        lambda: "999999",
        interval_seconds=0.01,
        initial_delay_seconds=0,
        allowed_groups=["987654"],
    )
    calls = 0

    async def run_once() -> tuple[tuple[str, str], ...]:
        nonlocal calls
        calls += 1
        if calls == 2:
            reconciler.stop()
        return ()

    monkeypatch.setattr(reconciler, "run_once", run_once)

    await asyncio.wait_for(reconciler.run(), timeout=1)

    assert calls == 2


async def test_reconciler_prefers_configured_groups_over_persisted_discovery(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.set_group_card_lock(
        "persisted", "100001", "Locked", updated_by_qq="admin"
    )
    queried: list[str] = []

    async def fetch(group_id: str) -> list[dict[str, object]]:
        queried.append(group_id)
        return []

    async def restore(_group_id: str, _qq_id: str) -> bool:
        return True

    reconciler = GroupCardLockReconciler(
        repository,
        fetch,
        restore,
        lambda: "999999",
        allowed_groups=["222222", "111111"],
    )

    assert await reconciler.run_once() == ()
    assert queried == ["111111", "222222"]


async def test_channel_starts_and_stops_group_card_reconciler_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport_release = asyncio.Event()
    reconciler_started = asyncio.Event()
    reconciler_cancelled = asyncio.Event()

    async def fake_transport_start(_channel: NapcatChannel) -> None:
        await transport_release.wait()

    async def fake_transport_stop(_channel: NapcatChannel) -> None:
        transport_release.set()

    monkeypatch.setattr(NapcatChannel, "start", fake_transport_start)
    monkeypatch.setattr(NapcatChannel, "stop", fake_transport_stop)
    channel = PWCS2Channel(
        {
            "enabled": True,
            "wsUrl": "ws://127.0.0.1:3001",
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "allowedGroups": ["987654"],
            "groupCardLockEnabled": True,
            "matchNotificationsEnabled": False,
            "predictionEnabled": False,
            "dailySummaryEnabled": False,
            "dailyAdminEnabled": False,
            "membershipSyncEnabled": False,
            "groupLevelReminderEnabled": False,
            "aiMemberMemoryEnabled": False,
            "dailyWordCloudEnabled": False,
            "skinPriceEnabled": False,
            "weeklyChemistryEnabled": False,
            "grenadeQuizEnabled": False,
            "weeklyReportEnabled": False,
            "tsEnabled": False,
            "valorantEnabled": False,
            "aiEnabled": False,
        },
        MessageBus(),
    )

    async def fake_reconciler_run() -> None:
        reconciler_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            reconciler_cancelled.set()

    monkeypatch.setattr(channel.group_card_reconciler, "run", fake_reconciler_run)

    start_task = asyncio.create_task(channel.start())
    await asyncio.wait_for(reconciler_started.wait(), timeout=1)
    reconcile_task = channel._group_card_reconcile_task
    assert reconcile_task is not None
    assert reconcile_task.get_name() == "pwcs2-group-card-reconcile"

    await channel.stop()
    await asyncio.wait_for(start_task, timeout=1)

    assert channel._group_card_reconcile_task is None
    assert reconcile_task.cancelled()
    assert reconciler_cancelled.is_set()
