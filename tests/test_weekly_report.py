from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pwcs2_bot.community import CommunityRepository
from pwcs2_bot.database import BindingRepository
from pwcs2_bot.weekly_report import WeeklyReportService

TZ = ZoneInfo("Asia/Shanghai")


class FakeApi:
    async def match_list(self, steam_id: str, *, page_size: int) -> list[dict]:
        suffix = int(steam_id[-1])
        return [
            {
                "matchId": "shared",
                "steamName": f"Game Player {suffix}",
                "endTime": "2026-07-26 20:00:00",
                "mapName": "荒漠迷城",
                "team": 1,
                "winTeam": 1,
                "kill": 20 + suffix,
                "death": 10,
                "pwRating": 1.1 + suffix / 100,
                "pvpScoreChange": 15,
            },
            {
                "matchId": "old",
                "endTime": "2026-07-18 20:00:00",
                "mapName": "炙热沙城Ⅱ",
            },
        ]


class ActiveFakeApi:
    async def match_list(self, steam_id: str, *, page_size: int) -> list[dict]:
        return [
            {
                "matchId": f"active-{index}",
                "steamName": "Active Game Player",
                "endTime": f"2026-07-{19 + index:02d} 20:00:00",
                "mapName": "荒漠迷城",
                "team": 1,
                "winTeam": 1 if index <= 3 else 2,
                "kill": 20,
                "death": 10,
                "pwRating": 1.2,
                "pvpScoreChange": 10,
            }
            for index in range(1, 6)
        ]


async def test_weekly_report_deduplicates_group_matches_and_sends_once(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bindings.db"
    bindings = BindingRepository(path)
    bindings.bind("group", "10001", "76561198000000001")
    bindings.bind("group", "10002", "76561198000000002")
    bindings.upsert_group_member_identity("group", "10001", "牛牛")
    bindings.upsert_group_member_identity("group", "10002", "精灵")
    repository = CommunityRepository(path)
    sent = []

    async def send(group_id, snapshot) -> None:
        sent.append((group_id, snapshot))

    service = WeeklyReportService(
        repository,
        bindings,
        FakeApi(),  # type: ignore[arg-type]
        send,
        initial_delay_seconds=0,
    )

    assert await service.poll_once(
        now=datetime(2026, 7, 26, 21, 59, tzinfo=TZ)
    ) == 0
    assert await service.poll_once(
        now=datetime(2026, 7, 26, 22, 0, tzinfo=TZ)
    ) == 1
    assert await service.poll_once(
        now=datetime(2026, 7, 26, 22, 5, tzinfo=TZ)
    ) == 0

    snapshot = sent[0][1]
    assert snapshot.active_members == 2
    assert snapshot.unique_matches == 1
    assert snapshot.popular_maps == (("荒漠迷城", 1),)
    assert {member.name for member in snapshot.members} == {"Game Player 1", "Game Player 2"}


async def test_weekly_activity_reward_survives_send_retry_without_duplicate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bindings.db"
    bindings = BindingRepository(path)
    bindings.bind("group", "10001", "76561198000000001")
    bindings.upsert_group_member_identity("group", "10001", "牛牛")
    repository = CommunityRepository(path)
    sent = []
    attempts = 0

    async def send(group_id, snapshot) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary send failure")
        sent.append((group_id, snapshot))

    service = WeeklyReportService(
        repository,
        bindings,
        ActiveFakeApi(),  # type: ignore[arg-type]
        send,
        initial_delay_seconds=0,
    )
    now = datetime(2026, 7, 26, 22, 0, tzinfo=TZ)

    assert await service.poll_once(now=now) == 0
    assert repository.wallet("group", "10001")[0] == 130
    assert await service.poll_once(now=now) == 1
    assert repository.wallet("group", "10001")[0] == 130
    assert sent[0][1].activity_rewards == (("10001", "Active Game Player", 30),)
