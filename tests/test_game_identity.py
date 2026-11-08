import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pwcs2_bot.game_identity import game_name, game_profile, match_sort_key, resolve_game_profile
from pwcs2_bot.perfectworld import PerfectWorldApiError
from pwcs2_bot.steam_presence import SteamPresenceError

STEAM_ID = "76561197960266101"


def test_names_only_use_explicit_game_fields_and_keep_unicode() -> None:
    assert game_name({"steamName": "  𝓖𝓪𝓶𝓮 🎯  ", "name": "Platform Name"}) == "𝓖𝓪𝓶𝓮 🎯"
    assert game_name({"nickName": "ID<>&", "name": "Platform Name"}) == "ID<>&"
    assert game_name({"card": "群名片", "qq_name": "QQ昵称"}, STEAM_ID) == STEAM_ID
    assert game_name({"steamName": 123, "nickName": "  "}, STEAM_ID) == STEAM_ID
    assert game_profile({"name": "地图名称"}, match_row=True) == {"name": ""}


async def test_newest_match_name_does_not_need_network() -> None:
    api = SimpleNamespace(match_detail=AsyncMock(), detail_stats=AsyncMock())
    profile = await resolve_game_profile(api, STEAM_ID, [
        {"matchId": "old", "timeStamp": 100, "steamName": "Old Game Name"},
        {"matchId": "new", "timeStamp": 200, "steamNick": "New Game Name"},
    ])
    assert profile == {"name": "New Game Name"}
    api.match_detail.assert_not_awaited()
    api.detail_stats.assert_not_awaited()


@pytest.mark.parametrize(("source", "data_source"), [("official", 1), ("pvp", 3)])
async def test_detail_identity_matches_exact_steam_id(source: str, data_source: int) -> None:
    api = SimpleNamespace(
        match_detail=AsyncMock(return_value={"players": [
            {"playerId": "76561198000000001", "nickName": "Teammate"},
            {"playerId": int(STEAM_ID), "nickName": "𝓖𝓪𝓶𝓮 🎯", "avatar": "https://avatar"},
        ]}),
        detail_stats=AsyncMock(),
    )
    profile = await resolve_game_profile(
        api, STEAM_ID, [{"matchId": "new"}], source=source,
    )
    assert profile == {"name": "𝓖𝓪𝓶𝓮 🎯", "avatar": "https://avatar"}
    api.match_detail.assert_awaited_once_with("new", data_source=data_source)
    api.detail_stats.assert_not_awaited()


async def test_official_missing_player_uses_steam_not_teammate_or_perfect_profile() -> None:
    api = SimpleNamespace(
        match_detail=AsyncMock(return_value={"players": [
            {"playerId": "76561198000000001", "nickName": "Wrong Player"},
        ]}),
        detail_stats=AsyncMock(return_value={"name": "Perfect Player"}),
    )
    steam = SimpleNamespace(
        configured=True,
        summaries=AsyncMock(return_value={STEAM_ID: SimpleNamespace(persona_name="Steam Player")}),
    )
    result = await resolve_game_profile(
        api, STEAM_ID, [{"matchId": "new"}], source="official", steam_presence=steam,
    )
    assert result == {"name": "Steam Player"}
    steam.summaries.assert_awaited_once_with((STEAM_ID,))
    api.detail_stats.assert_not_awaited()


async def test_perfect_identity_can_fall_back_to_game_profile() -> None:
    api = SimpleNamespace(
        match_detail=AsyncMock(side_effect=PerfectWorldApiError("unavailable")),
        detail_stats=AsyncMock(return_value={"name": "Perfect Player"}),
    )
    assert await resolve_game_profile(api, STEAM_ID, [{"matchId": "new"}]) == {
        "name": "Perfect Player",
    }


@pytest.mark.parametrize("error", [PerfectWorldApiError("unavailable"), TimeoutError()])
async def test_optional_lookup_failure_returns_steam_id(error: Exception) -> None:
    api = SimpleNamespace(
        match_detail=AsyncMock(side_effect=error),
        detail_stats=AsyncMock(side_effect=error),
    )
    steam = SimpleNamespace(
        configured=True, summaries=AsyncMock(side_effect=SteamPresenceError("unavailable")),
    )
    assert await resolve_game_profile(
        api, STEAM_ID, [{"matchId": "new"}], steam_presence=steam,
    ) == {"name": STEAM_ID}


async def test_missing_identity_does_not_reuse_older_name_or_borrow_another_player() -> None:
    api = SimpleNamespace(match_detail=AsyncMock(return_value={"players": [
        None, {"steamId": "76561198000000001", "nickName": "Wrong Player"},
        {"steamId": STEAM_ID, "nickName": "  "},
    ]}))
    assert await resolve_game_profile(api, STEAM_ID, [
        {"matchId": "old", "timeStamp": 100, "steamName": "Old Name"},
        {"matchId": "new", "timeStamp": 200},
    ], source="official") == {"name": STEAM_ID}


async def test_lookup_cancellation_propagates() -> None:
    api = SimpleNamespace(match_detail=AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await resolve_game_profile(api, STEAM_ID, [{"matchId": "new"}], source="official")


async def test_slow_optional_lookup_is_bounded_and_cancelled(monkeypatch) -> None:
    original_timeout = asyncio.timeout
    cancelled = asyncio.Event()

    def quick_timeout(seconds: float):
        assert seconds == 8
        return original_timeout(0.02)

    async def hang(*_args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr("pwcs2_bot.game_identity.asyncio.timeout", quick_timeout)
    steam = SimpleNamespace(configured=True, summaries=hang)
    assert await resolve_game_profile(
        SimpleNamespace(), STEAM_ID, [], source="official", steam_presence=steam,
    ) == {"name": STEAM_ID}
    assert cancelled.is_set()


def test_sort_key_retains_timestamp_and_end_time_ordering() -> None:
    assert match_sort_key({"timeStamp": "200.0", "matchId": "new"}) == (200, "new")
    assert match_sort_key({"timeStamp": True, "matchId": "bool"}) == (0, "bool")
    assert match_sort_key({"timeStamp": float("inf")}) == (0, "")
    assert match_sort_key({"endTime": "2026-09-29 12:00:00"})[0] > 0
