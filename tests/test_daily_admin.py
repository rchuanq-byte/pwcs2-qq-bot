from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from nanobot.bus.queue import MessageBus

from pwcs2_bot.channel import PWCS2Channel
from pwcs2_bot.daily_admin import (
    DailyAdminCandidate,
    DailyAdminService,
    format_daily_admin_announcement,
    rank_daily_admin_candidates,
    select_weighted_daily_admin,
)
from pwcs2_bot.daily_summary import DailyGroupSummary, DailyMemberSummary
from pwcs2_bot.database import BindingRepository

TZ = ZoneInfo("Asia/Shanghai")


def _daily_member(qq_id: str, matches: int) -> DailyMemberSummary:
    return DailyMemberSummary(
        qq_id=qq_id,
        steam_id=f"7656119800000{int(qq_id):04d}",
        name=f"Player-{qq_id}",
        matches=matches,
        wins=matches,
        losses=0,
        draws=0,
        kills=20 * matches,
        assists=5 * matches,
        deaths=10 * matches,
        kd=2.0,
        avg_rating=1.2,
        avg_adr=90,
        elo_change=15 * matches,
        mvp_count=matches,
        maps=(("荒漠迷城", matches),),
    )


def test_daily_admin_ranking_caps_spam_excludes_manual_admin_and_rotates() -> None:
    members = [
        {"user_id": 1001, "role": "member", "level": "2", "card": "A"},
        {"user_id": 1002, "role": "admin", "level": "9", "card": "人工管理"},
        {"user_id": 1003, "role": "admin", "level": "3", "card": "昨日值日"},
        {"user_id": 9999, "role": "owner", "level": "9", "card": "机器人"},
    ]

    ranked = rank_daily_admin_candidates(
        members=members,
        bound_qq_ids={"1001", "1002", "1003"},
        match_counts={"1001": 2, "1002": 9, "1003": 2},
        message_counts={"1001": 500, "1002": 500, "1003": 50},
        source_date=date(2026, 7, 22),
        bot_qq_id="9999",
        previous_qq_id="1003",
    )

    assert [item.qq_id for item in ranked] == ["1001", "1003"]
    assert ranked[0].message_score == 30
    assert ranked[0].total_score == 58
    assert ranked[1].total_score == 48


def _candidate(qq_id: str, score: float) -> DailyAdminCandidate:
    return DailyAdminCandidate(
        qq_id=qq_id,
        nickname=f"Player-{qq_id}",
        match_count=1,
        message_count=5,
        match_score=score,
        message_score=0,
        repeat_penalty=0,
        total_score=score,
    )


def test_daily_admin_selection_uses_score_weights_instead_of_always_top() -> None:
    candidates = (_candidate("high", 60), _candidate("low", 20))
    requested: list[int] = []

    def choose_low_boundary(upper: int) -> int:
        requested.append(upper)
        return 6000

    high = select_weighted_daily_admin(candidates, randbelow=lambda _upper: 5999)
    low = select_weighted_daily_admin(candidates, randbelow=choose_low_boundary)

    assert high is candidates[0]
    assert low is candidates[1]
    assert requested == [8000]


def test_daily_admin_selection_falls_back_to_uniform_when_all_weights_are_zero() -> None:
    candidates = (_candidate("first", 0), _candidate("second", 0))

    selected = select_weighted_daily_admin(
        candidates,
        randbelow=lambda upper: upper - 1,
    )

    assert selected is candidates[1]


def test_daily_admin_selection_never_selects_zero_weight_when_positive_exists() -> None:
    candidates = (_candidate("zero", 0), _candidate("positive", 20))

    selected = select_weighted_daily_admin(candidates, randbelow=lambda _upper: 0)

    assert selected is candidates[1]


async def test_daily_admin_service_assigns_once_and_persists_audit(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.bind("group", "1001", "76561198000001001")
    repository.bind("group", "1002", "76561198000001002")
    for _ in range(10):
        repository.record_daily_member_message("group", "2026-07-22", "1001")
    for _ in range(50):
        repository.record_daily_member_message("group", "2026-07-22", "1002")

    class FakeSummary:
        async def collect_group(self, group_id: str, summary_date: date):
            return DailyGroupSummary(
                group_id=group_id,
                summary_date=summary_date,
                unique_matches=3,
                total_bound_members=2,
                failed_members=0,
                members=(_daily_member("1001", 2), _daily_member("1002", 1)),
            )

    members = [
        {"user_id": 9999, "role": "owner", "level": "9", "is_robot": True},
        {"user_id": 1001, "role": "member", "level": "2", "card": "两场哥"},
        {"user_id": 1002, "role": "member", "level": "2", "card": "话痨"},
    ]
    applied: list[tuple[str, str | None, str | None]] = []
    announced = []
    draws: list[int] = []

    async def fetch(_group_id: str):
        return members

    async def apply(group_id: str, previous: str | None, winner: str | None):
        applied.append((group_id, previous, winner))
        if len(applied) == 1:
            raise RuntimeError("transient assignment failure")

    async def announce(_group_id: str, run):
        announced.append(run)

    def choose_last_ticket(upper: int) -> int:
        draws.append(upper)
        return upper - 1

    service = DailyAdminService(
        repository,
        FakeSummary(),  # type: ignore[arg-type]
        fetch,
        apply,
        announce,
        lambda: "9999",
        allowed_groups=["group"],
        randbelow=choose_last_ticket,
    )
    now = datetime(2026, 7, 23, 0, 1, tzinfo=TZ)

    assert await service.run_due_once(now) == 0
    planned = repository.get_daily_admin_run("group", "2026-07-23")
    assert planned is not None
    assert planned.status == "planned"
    assert planned.winner_qq_id == "1001"

    def reject_second_draw(_upper: int) -> int:
        raise AssertionError("persisted daily administrator must not be redrawn")

    restarted_service = DailyAdminService(
        repository,
        FakeSummary(),  # type: ignore[arg-type]
        fetch,
        apply,
        announce,
        lambda: "9999",
        allowed_groups=["group"],
        randbelow=reject_second_draw,
    )
    assert await restarted_service.run_due_once(now) == 1
    assert await restarted_service.run_due_once(now) == 1
    assert applied == [
        ("group", None, "1001"),
        ("group", None, "1001"),
    ]
    assert len(announced) == 1
    assert draws == [7800]
    assert repository.get_daily_admin("group") == "1001"
    run = repository.get_daily_admin_run("group", "2026-07-23")
    assert run is not None
    assert run.status == "assigned"
    assert run.announcement_sent is True
    assert run.source_date == "2026-07-22"
    assert run.total_score == 34
    announcement = format_daily_admin_announcement(run)
    assert "按综合分权重随机抽取" in announcement
    assert "本次中签概率约 43.6%" in announcement
    legacy_run = replace(
        run,
        ranking=tuple(
            {key: value for key, value in item.items() if key != "selection_method"}
            for item in run.ranking
        ),
    )
    legacy_announcement = format_daily_admin_announcement(legacy_run)
    assert "已持久化的旧版计划" in legacy_announcement
    assert "按综合分权重随机抽取" not in legacy_announcement


async def test_channel_rotates_only_bot_tracked_admin(tmp_path: Path, monkeypatch) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "allowedGroups": ["123456"],
        },
        MessageBus(),
    )
    channel._self_id = 9999
    members = [
        {"user_id": 9999, "role": "owner", "level": "9", "is_robot": True},
        {"user_id": 1001, "role": "admin", "level": "3", "is_robot": False},
        {"user_id": 1002, "role": "member", "level": "3", "is_robot": False},
    ]
    actions: list[tuple[str, dict]] = []

    async def fetch(_group_id: str):
        return members

    async def action(name: str, params: dict):
        actions.append((name, params))
        return {"status": "ok", "retcode": 0}

    monkeypatch.setattr(channel, "_fetch_group_members", fetch)
    monkeypatch.setattr(channel, "_call_action", action)
    try:
        await channel._apply_daily_admin("123456", "1001", "1002")
    finally:
        await channel.stop()

    assert actions == [
        (
            "set_group_admin",
            {"group_id": 123456, "user_id": 1002, "enable": True},
        ),
        (
            "set_group_admin",
            {"group_id": 123456, "user_id": 1001, "enable": False},
        ),
    ]


async def test_channel_refuses_admin_rotation_when_bot_is_not_owner(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "allowedGroups": ["123456"],
        },
        MessageBus(),
    )
    channel._self_id = 9999

    async def fetch(_group_id: str):
        return [
            {"user_id": 9999, "role": "admin", "level": "9", "is_robot": True},
            {"user_id": 1002, "role": "member", "level": "3", "is_robot": False},
        ]

    monkeypatch.setattr(channel, "_fetch_group_members", fetch)
    try:
        with pytest.raises(RuntimeError, match="不是群主"):
            await channel._apply_daily_admin("123456", None, "1002")
    finally:
        await channel.stop()
