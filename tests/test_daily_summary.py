from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from pwcs2_bot.ai_chat import AIChatError
from pwcs2_bot.daily_summary import DailySummaryService, _match_datetime
from pwcs2_bot.database import BindingRepository
from pwcs2_bot.perfectworld import PerfectWorldApiError

TZ = ZoneInfo("Asia/Shanghai")


def _match(
    match_id: str,
    ended_at: datetime,
    *,
    win: bool = True,
    kills: int = 20,
    deaths: int = 10,
    rating: float = 1.2,
    elo_change: int = 15,
) -> dict:
    return {
        "matchId": match_id,
        "timeStamp": int(ended_at.timestamp()),
        "endTime": ended_at.strftime("%Y-%m-%d %H:%M:%S"),
        "mapName": "荒漠迷城",
        "team": 1,
        "winTeam": 1 if win else 2,
        "score1": 13 if win else 8,
        "score2": 8 if win else 13,
        "kill": kills,
        "assist": 5,
        "death": deaths,
        "pwRating": rating,
        "adr": 82.5,
        "pvpScoreChange": elo_change,
    }


class FakeApi:
    def __init__(self, matches: dict[str, list[dict]]) -> None:
        self.matches = matches
        self.match_queries: list[tuple[str, int]] = []

    async def match_list(self, steam_id: str, *, page_size: int) -> list[dict]:
        self.match_queries.append((steam_id, page_size))
        return self.matches.get(steam_id, [])

    async def detail_stats(self, steam_id: str) -> dict:
        return {"name": f"Player-{steam_id[-2:]}"}


class FakeAi:
    def __init__(self, answer: str = "AI 总结正文") -> None:
        self.answer = answer
        self.requests: list[dict] = []

    async def complete_once(self, **kwargs) -> str:
        self.requests.append(kwargs)
        return self.answer


class FailingAi:
    async def complete_once(self, **_kwargs) -> str:
        raise AIChatError("tunnel offline")


async def test_daily_summary_aggregates_members_and_deduplicates_group_matches(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    first = "76561198000000001"
    second = "76561198000000002"
    repository.bind("group", "qq-1", first)
    repository.bind("group", "qq-2", second)
    target = datetime(2026, 7, 1, 20, 0, tzinfo=TZ)
    old = datetime(2026, 6, 30, 20, 0, tzinfo=TZ)
    api = FakeApi(
        {
            first: [_match("shared", target), _match("old", old)],
            second: [
                _match("shared", target, kills=12, deaths=12, rating=0.95),
                _match(
                    "solo",
                    target,
                    win=False,
                    kills=18,
                    deaths=20,
                    rating=0.88,
                    elo_change=-18,
                ),
            ],
        }
    )
    service = DailySummaryService(repository, api, FakeAi(), lambda *_args: None)

    summary = await service.collect_group("group", date(2026, 7, 1))

    assert summary.unique_matches == 2
    assert len(summary.members) == 2
    assert summary.members[0].name == "Player-01"
    assert summary.members[0].matches == 1
    second_summary = next(member for member in summary.members if member.steam_id == second)
    assert (second_summary.matches, second_summary.wins, second_summary.losses) == (2, 1, 1)
    assert second_summary.kd == 30 / 32
    assert second_summary.elo_change == -3
    assert api.match_queries == [(first, 50), (second, 50)]


async def test_due_summary_uses_ai_sends_once_and_persists_run(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    steam_id = "76561198000000001"
    repository.bind("group", "qq-1", steam_id)
    target = datetime(2026, 7, 1, 21, 0, tzinfo=TZ)
    ai = FakeAi()
    sent: list[tuple[str, str]] = []

    async def send(group_id: str, content: str) -> None:
        sent.append((group_id, content))

    service = DailySummaryService(
        repository,
        FakeApi({steam_id: [_match("match", target)]}),
        ai,
        send,
    )
    now = datetime(2026, 7, 1, 23, 31, tzinfo=TZ)

    assert await service.run_due_once(now) == 1
    assert await service.run_due_once(now) == 0
    assert sent == [
        (
            "group",
            "🎮 CS2 每日群报｜2026-07-01\n"
            "AI 总结正文\n"
            "📊 完美平台数据核对\n"
            "• Player-01：1场 1胜0负0平｜K/A/D 20/5/10｜K/D 2.00｜"
            "Rating 1.20｜ELO +15｜常玩 荒漠迷城\n"
            "数据：1 人出战 · 1 场独立对局",
        )
    ]
    assert repository.is_daily_summary_processed("group", "2026-07-01")
    payload = json.loads(ai.requests[0]["content"].split("\n", 1)[1])
    assert payload["unique_group_matches"] == 1
    assert payload["members"][0]["elo_change"] == 15.0
    assert ai.requests[0]["max_tokens"] == 4096


async def test_empty_day_is_marked_without_sending(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    steam_id = "76561198000000001"
    repository.bind("group", "qq-1", steam_id)
    sent: list[str] = []

    async def send(_group_id: str, content: str) -> None:
        sent.append(content)

    service = DailySummaryService(repository, FakeApi({steam_id: []}), FakeAi(), send)
    now = datetime(2026, 7, 1, 23, 31, tzinfo=TZ)

    assert await service.run_due_once(now) == 0
    assert await service.run_due_once(now) == 0
    assert sent == []
    assert repository.is_daily_summary_processed("group", "2026-07-01")


async def test_ai_failure_uses_deterministic_fallback(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    steam_id = "76561198000000001"
    repository.bind("group", "qq-1", steam_id)
    service = DailySummaryService(
        repository,
        FakeApi(
            {
                steam_id: [
                    _match("match", datetime(2026, 7, 1, 20, 0, tzinfo=TZ))
                ]
            }
        ),
        FailingAi(),
        lambda *_args: None,
    )
    summary = await service.collect_group("group", date(2026, 7, 1))

    content = await service.generate_summary(summary)

    assert "CS2 每日群报｜2026-07-01" in content
    assert "Player-01：1 场 1胜0负" in content
    assert "ELO +15" in content


def test_match_datetime_prefers_explicit_end_time_over_timestamp() -> None:
    match = {
        "endTime": "2026-07-01 23:59:00",
        "timeStamp": int(datetime(2026, 7, 2, 1, 0, tzinfo=TZ).timestamp()),
    }

    parsed = _match_datetime(match, TZ)

    assert parsed is not None
    assert parsed.date() == date(2026, 7, 1)
    assert parsed.hour == 23


async def test_daily_summary_retries_instead_of_sending_partial_data(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    first = "76561198000000001"
    second = "76561198000000002"
    repository.bind("group", "qq-1", first)
    repository.bind("group", "qq-2", second)

    class PartiallyFailingApi(FakeApi):
        async def match_list(self, steam_id: str, *, page_size: int) -> list[dict]:
            if steam_id == second:
                raise PerfectWorldApiError("temporary")
            return await super().match_list(steam_id, page_size=page_size)

    api = PartiallyFailingApi(
        {first: [_match("match", datetime(2026, 7, 1, 20, 0, tzinfo=TZ))]}
    )
    service = DailySummaryService(repository, api, FakeAi(), lambda *_args: None)

    with pytest.raises(PerfectWorldApiError, match="完整重试"):
        await service.collect_group("group", date(2026, 7, 1))
