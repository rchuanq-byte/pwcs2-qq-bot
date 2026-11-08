from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pwcs2_bot.database import BindingRepository
from pwcs2_bot.perfectworld import PerfectWorldApiError
from pwcs2_bot.weekly_chemistry import WeeklyChemistryService

TZ = ZoneInfo("Asia/Shanghai")


class FakeApi:
    def __init__(self) -> None:
        self.fail = False
        self.calls: list[tuple[str, int]] = []

    async def match_list(self, steam_id: str, *, page_size: int) -> list[dict]:
        self.calls.append((steam_id, page_size))
        if self.fail:
            raise PerfectWorldApiError("temporary")
        return [
            {
                "matchId": "shared",
                "steamName": f"Game Player {steam_id[-1]}",
                "team": 1,
                "winTeam": 1,
                "pwRating": 1.1,
            }
        ]


def _repository(tmp_path: Path) -> BindingRepository:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.bind("group", "10001", "76561198000000001")
    repository.bind("group", "10002", "76561198000000002")
    repository.upsert_group_member_identity("group", "10001", "牛牛")
    repository.upsert_group_member_identity("group", "10002", "精灵")
    return repository


async def test_weekly_network_waits_for_sunday_sends_once_and_persists(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    api = FakeApi()
    sent = []

    async def send(group_id, snapshot) -> None:
        sent.append((group_id, snapshot))

    service = WeeklyChemistryService(
        repository,
        api,  # type: ignore[arg-type]
        send,
        initial_delay_seconds=0,
    )

    assert await service.poll_once(now=datetime(2026, 7, 26, 21, 29, tzinfo=TZ)) == 0
    assert await service.poll_once(now=datetime(2026, 7, 26, 21, 30, tzinfo=TZ)) == 1
    assert await service.poll_once(now=datetime(2026, 7, 26, 22, 0, tzinfo=TZ)) == 0

    assert len(sent) == 1
    assert sent[0][0] == "group"
    assert sent[0][1].week_start == "2026-07-20"
    assert sent[0][1].pairs[0].same_team_win_rate == "100.0%"
    assert sent[0][1].pairs[0].first_name == "Game Player 1"
    assert sent[0][1].pairs[0].second_name == "Game Player 2"
    assert repository.is_weekly_chemistry_processed("group", "2026-07-20")


async def test_weekly_network_retries_when_any_member_query_fails(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    api = FakeApi()
    api.fail = True
    sent = []

    async def send(group_id, snapshot) -> None:
        sent.append((group_id, snapshot))

    service = WeeklyChemistryService(
        repository,
        api,  # type: ignore[arg-type]
        send,
        initial_delay_seconds=0,
    )

    assert await service.poll_once(now=datetime(2026, 7, 26, 21, 30, tzinfo=TZ)) == 0
    assert not repository.is_weekly_chemistry_processed("group", "2026-07-20")
    assert sent == []
