import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pwcs2_bot.ai_group_data import AIGroupDataService
from pwcs2_bot.commands import CommandService
from pwcs2_bot.community import CommunityRepository, GroupPlayService
from pwcs2_bot.database import BindingRepository
from pwcs2_bot.teamspeak import TeamSpeakChannel, TeamSpeakMember, TeamSpeakStatus


class Api:
    def __init__(self):
        self.calls = []
        self.matches = []
        self.official = []
        self.failed = set()

    async def detail_stats_v2(self, steam_id):
        self.calls.append(("stats", steam_id))
        if steam_id in self.failed:
            raise RuntimeError("network offline")
        return {
            "steamId": steam_id,
            "name": "比赛名",
            "seasonId": "S99",
            "cnt": 20,
            "pvpScore": 2100,
            "pwRating": 1.12,
            "winRate": 0.55,
            "radar": {"marksmanship": {"detail": {"rapid_stop_rate_raw": 0.87}}},
        }

    async def detail_stats(self, steam_id):
        return await self.detail_stats_v2(steam_id)

    async def match_list(self, steam_id, *, page_size):
        self.calls.append(("perfect", steam_id, page_size))
        if steam_id in self.failed:
            raise RuntimeError("network offline")
        return self.matches[:page_size]

    async def official_match_list(self, steam_id, *, page_size):
        self.calls.append(("official", steam_id, page_size))
        if steam_id in self.failed:
            raise RuntimeError("network offline")
        return self.official[:page_size]


@pytest.fixture
def data(tmp_path):
    path = tmp_path / "test.db"
    repo = BindingRepository(path)
    community = CommunityRepository(path)
    for group, qq, name, steam in (
        ("g1", "100001", "牛牛", "76561197960266101"),
        ("g1", "100002", "ExamplePlayer", "76561197960266105"),
        ("g2", "100003", "外群成员", "76561197960266103"),
    ):
        repo.bind(group, qq, steam)
        repo.upsert_group_member_identity(group, qq, name)
    repo.upsert_group_member_identity("g1", "100004", "没绑Steam")
    api = Api()
    commands = CommandService(repo, api, group_play=GroupPlayService(community, repo))
    return SimpleNamespace(
        service=AIGroupDataService(commands), api=api, repo=repo, community=community
    )


async def query(data, **args):
    return await data.service.for_request("g1", "100001").execute("query_group_statistics", args)


@pytest.mark.parametrize("name", ["me", "牛牛", "100001", "76561197960266101"])
async def test_current_member_resolution_and_missing_metrics_stay_missing(data, name):
    result = await query(data, kind="cs_stats", member=name)
    assert result["ok"]
    assert result["source"] == "perfect"
    assert result["data"]["qq_id"] == "100001"
    assert result["data"]["metrics"]["rapid_stop"] == 87
    assert result["data"]["metrics"]["kd"] is None
    assert result["data"]["stats"]["kills"] == "-"


async def test_names_are_case_insensitive_fuzzy_but_ambiguity_never_defaults_to_self(data):
    result = await query(data, kind="cs_stats", member="HUNG")
    assert result["data"]["qq_id"] == "100002"
    data.repo.upsert_group_member_identity("g1", "100004", "ExamplePlayer")
    result = await query(data, kind="cs_stats", member="ExamplePlayer")
    assert not result["ok"]
    assert "不唯一" in result["message"]
    assert "100002" in result["message"] and "100004" in result["message"]
    assert len(data.api.calls) == 1


@pytest.mark.parametrize("member", ["外群成员", "100003", "76561197960266103", "doesnotexist"])
async def test_cannot_query_nonmember_or_other_group(data, member):
    assert not (await query(data, kind="cs_stats", member=member))["ok"]
    assert not data.api.calls


@pytest.mark.parametrize(
    "args",
    [
        {"kind": "coins", "group_id": "g2"},
        {"kind": "grant_coins"},
        {"kind": "coins", "limit": True},
        {"kind": "coins", "limit": 100},
        {"kind": "coins", "member": {"qq": "100001"}},
        {"kind": "activity", "date": "2099-01-01"},
        {"kind": "activity", "date": "2020-01-01"},
        {"kind": "activity", "date": "' OR 1=1 --"},
        {"kind": "cs_stats", "source": "other"},
    ],
)
async def test_invalid_arguments_never_dispatch(data, args):
    result = await query(data, **args)
    assert result["error"] == "invalid_query"
    assert not data.api.calls


async def test_tools_are_readonly_and_do_not_award_welcome_coins(data):
    result = await query(data, kind="coins", member="me")
    assert result["data"]["balance"] is None
    assert result["data"]["wallet_exists"] is False
    assert data.community.peek_wallet("g1", "100001") is None
    assert data.community.coin_ranking("g1") == []
    toolbox = data.service.for_request("g1", "100001")
    for name in ("exec", "sql", "grant_coins", "handle_result", "__dict__"):
        assert (await toolbox.execute(name, {"amount": 1000}))["error"] == "unknown_tool"
    assert data.community.coin_ranking("g1") == []


async def test_cache_scoped_by_member_and_group_no_cross_request_identity_leak(data):
    data.community.wallet("g1", "100001")
    a = await query(data, kind="coins", member="me")
    b = await data.service.for_request("g1", "100002").execute(
        "query_group_statistics", {"kind": "coins", "member": "me"}
    )
    c = await data.service.for_request("g2", "100003").execute(
        "query_group_statistics", {"kind": "coins", "member": "me"}
    )
    assert a["data"]["balance"] == 100
    assert b["data"]["balance"] is None and c["data"]["balance"] is None
    assert b["data"]["qq_id"] == "100002" and c["data"]["qq_id"] == "100003"
    assert c["group_id"] == "g2"


async def test_sources_remain_separate_including_recent_matches(data):
    data.api.matches = [match("PVP1", data, team=1, winner=2)]
    data.api.official = [match("GP1", data, team=2, winner=2)]
    p = await query(data, kind="cs_recent_matches", member="me")
    g = await query(data, kind="cs_recent_matches", member="me", source="official")
    assert p["ok"] and g["ok"]
    assert p["source"] == "perfect" and g["source"] == "official"
    assert "PVP1" in json.dumps(p) and "GP1" not in json.dumps(p)
    assert "GP1" in json.dumps(g) and "PVP1" not in json.dumps(g)
    assert ("official", "76561197960266101", 10) in data.api.calls
    invalid = await query(data, kind="cs_ranking", source="official", metric="elo")
    assert invalid["error"] == "invalid_query"


@pytest.mark.parametrize("metric", ["rapid_stop", "win_rate", "headshot", "elo", "kd"])
async def test_ranking_metrics_use_correct_command_aliases(data, metric):
    result = await query(data, kind="cs_ranking", metric=metric, limit=1)
    assert result["ok"]
    assert result["data"]["metric"] == metric
    assert len(result["data"]["entries"]) <= 1


async def test_cache_fresh_then_stale_clearly_labelled_then_expired(data):
    first = await query(data, kind="cs_stats", member="me")
    second = await query(data, kind="cs_stats", member="me")
    assert first["cache_state"] == "fresh"
    assert second["cache_state"] == "recent" and len(data.api.calls) == 1
    key = next(iter(data.service._cache))
    stamp, value = data.service._cache[key]
    data.service._cache[key] = (stamp - 60, value)
    data.api.failed.add("76561197960266101")
    stale = await query(data, kind="cs_stats", member="me")
    assert stale["cache_state"] == "stale" and stale["age_seconds"] >= 60
    assert stale["warning"] and stale["fetched_at"] == first["fetched_at"]
    data.service._cache[key] = (stamp - 1000, value)
    failed = await query(data, kind="cs_stats", member="me")
    assert failed["ok"] is False and "data" not in failed


def match(match_id, data, *, team=1, winner=1, days_ago=0):
    ended = datetime.now(data.service.timezone) - timedelta(days=days_ago)
    return {
        "matchId": match_id,
        "endTime": ended.strftime("%Y-%m-%d %H:%M:%S"),
        "team": team,
        "winTeam": winner,
        "score1": 13,
        "score2": 9,
        "kill": 20,
        "death": 10,
        "assist": 6,
        "pwRating": 1.2,
        "mapName": "de_dust2",
    }


async def test_daily_correct_timezone_dedup_and_player_count_not_group_count(data):
    data.api.matches = [match("today", data), match("today", data), match("old", data, days_ago=1)]
    result = (await query(data, kind="daily_cs"))["data"]
    assert result["unique_matches"] == 1
    assert result["player_match_count"] == 2
    assert result["wins"] == 2 and result["losses"] == 0
    assert result["coverage_complete"]


async def test_daily_official_winner_uses_player_team(data):
    data.api.official = [match("gp", data, team=2, winner=2)]
    result = await query(data, kind="daily_cs", source="official", member="me")
    assert result["data"]["wins"] == 1 and result["data"]["losses"] == 0
    assert all(call[0] == "official" for call in data.api.calls)


async def test_daily_partial_failure_is_reported_not_zero_and_all_failed_is_error(data):
    data.api.matches = [match("1", data)]
    data.api.failed.add("76561197960266105")
    result = await query(data, kind="daily_cs")
    assert result["ok"] and not result["data"]["coverage_complete"]
    assert result["data"]["incomplete_member_ids"] == ["100002"]
    data.service._cache.clear()
    data.api.failed.add("76561197960266101")
    assert (await query(data, kind="daily_cs"))["error"] == "data_unavailable"


async def test_daily_50_record_window_does_not_claim_complete_coverage(data):
    data.api.matches = [match(str(i), data) for i in range(50)]
    result = await query(data, kind="daily_cs", member="me")
    assert result["data"]["player_match_count"] == 50
    assert not result["data"]["coverage_complete"]


async def test_daily_missing_kills_unknown_results_and_zero_placeholder_rating(data):
    raw = match("incomplete", data)
    raw.pop("kill")
    raw.pop("team")
    raw["pwRating"] = 0
    data.api.official = [raw]
    result = await query(data, kind="daily_cs", member="me", source="official")
    row = result["data"]["members"][0]
    assert row["kills"] is None and row["kd"] is None
    assert row["avg_rating"] is None and row["metric_sample_counts"]["avg_rating"] == 0
    assert row["unknown_results"] == 1 and row["wins"] == row["losses"] == 0
    assert row["elo_change"] is None and row["mvp_count"] is None


async def test_activity_scope_date_and_empty_me_do_not_invent_counts(data):
    day = datetime.now(data.service.timezone).date().isoformat()
    for _ in range(3):
        data.repo.record_daily_member_message("g1", day, "100002")
    data.repo.record_daily_member_message("g2", day, "100003")
    result = await query(data, kind="activity")
    assert result["data"]["total_recorded_messages"] == 3
    assert result["data"]["members"][0]["qq_id"] == "100002"
    assert (await query(data, kind="activity", member="me"))["data"]["members"] == []


async def test_queries_share_bounded_network_slots_and_release_on_cancel(data, monkeypatch):
    active, peak = 0, 0

    async def slow(*args):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        try:
            await asyncio.sleep(0.02)
            return {"n": 1}
        finally:
            active -= 1

    monkeypatch.setattr(data.service, "_query", slow)
    await asyncio.gather(*(query(data, kind="teamspeak", limit=n) for n in range(1, 7)))
    assert peak == 2 and data.service._slots._value == 2
    task = asyncio.create_task(query(data, kind="teamspeak", limit=7))
    await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert data.service._slots._value == 2


async def test_readonly_misc_providers_and_no_images_or_arbitrary_symbols(data):
    status = TeamSpeakStatus("test", 32, (TeamSpeakChannel(1, "lobby", (TeamSpeakMember("a"),)),))
    data.service.commands.teamspeak = SimpleNamespace(status=AsyncMock(return_value=status))
    ts = await query(data, kind="teamspeak")
    assert ts["data"]["online_clients"] == 1
    assert (await query(data, kind="parties"))["data"]["parties"] == []
    assert (await query(data, kind="daily_admin"))["data"]["selection"] is None
    quotes = AsyncMock()
    data.service.commands.skin_prices = SimpleNamespace(client=SimpleNamespace(quotes=quotes))
    assert (await query(data, kind="skin_prices"))["data"]["entries"] == []
    quotes.assert_not_called()


async def test_missing_binding_asks_for_binding_without_api_calls(data):
    result = await query(data, kind="cs_stats", member="没绑Steam")
    assert result["error"] == "invalid_query" and "未" in result["message"]
    assert not data.api.calls


async def test_tool_metadata_binds_actual_requester_mentions_and_clock(data):
    toolbox = data.service.for_request("g1", "100002", ("100001",))
    assert '"requester_qq_id": "100002"' in toolbox.context
    assert '"mentioned_qq_ids": ["100001"]' in toolbox.context
    assert "+08:00" in toolbox.context
    found = await toolbox.execute("find_group_members", {"query": "me"})
    assert found["members"][0]["qq_id"] == "100002"
    all_members = await toolbox.execute("find_group_members", {"query": ""})
    assert {m["qq_id"] for m in all_members["members"]} == {"100001", "100002", "100004"}
