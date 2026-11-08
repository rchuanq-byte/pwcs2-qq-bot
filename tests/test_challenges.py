import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from pwcs2_bot.challenges import ChallengeService
from pwcs2_bot.commands import CommandService, ParsedCommand
from pwcs2_bot.community import CommunityError, CommunityRepository, GroupPlayService
from pwcs2_bot.database import BindingRepository
from pwcs2_bot.match_formatting import build_match_snapshot

G, A, B, C = "group", "10001", "10002", "10003"
S1, S2, S3 = "76561199000000001", "76561199000000002", "76561199000000003"
NOW = datetime(2026, 10, 18, 14, tzinfo=UTC)


@pytest.fixture
def service(tmp_path):
    path = tmp_path / "test.db"
    bindings = BindingRepository(path)
    community = CommunityRepository(path)
    for qq, steam in [(A, S1), (B, S2), (C, S3)]:
        bindings.bind(G, qq, steam)
        bindings.upsert_group_member_identity(G, qq, qq)
    return ChallengeService(community, bindings, clock=lambda: NOW)


def snapshot(**overrides):
    raw = {
        "matchId": "PVP@one", "endTime": "2026-10-18 21:00:00",
        "score1": 13, "score2": 8, "team": 1, "winTeam": 1,
        "kill": 22, "assist": 7,
    }
    match = build_match_snapshot(raw, steam_id=S1, detail={"players": [
        {"playerId": S1, "team": 1, "kill": 22, "assist": 7},
        {"playerId": S2, "team": 1, "kill": 18, "assist": 9},
        {"playerId": S3, "team": 2, "kill": 21, "assist": 2},
    ]}, group_member_steam_ids={S1, S2, S3})
    return replace(match, **overrides)


def test_rewards_require_claim_and_replay_cannot_pay_twice(service):
    service.record_match(G, snapshot())
    service.record_match(G, snapshot(steam_id=S2))
    assert service.community.peek_wallet(G, A) is None
    assert service.handle(G, A).count("可领取") == 3
    assert "到账 35" in service.handle(G, A, "领取")
    assert service.community.wallet(G, A)[0] == 135
    reloaded = ChallengeService(service.community, service.bindings, clock=lambda: NOW)
    assert "没有可领取" in reloaded.handle(G, A, "领取")
    assert reloaded.handle(G, A).count("已领取") == 3
    assert "到账 25" in service.handle(G, B, "领取")
    assert "到账 10" in service.handle(G, C, "领取")


def test_concurrent_claims_are_atomic(service):
    service.record_match(G, snapshot())
    with ThreadPoolExecutor(max_workers=5) as pool:
        replies = list(pool.map(lambda _: service.handle(G, A, "领取"), range(5)))
    assert sum("到账 35" in text for text in replies) == 1
    assert service.community.wallet(G, A)[0] == 135
    assert len([row for row in service.community.coin_ledger(G, A) if "挑战" in row[1]]) == 3


def test_sources_and_groups_do_not_share_progress_or_claims(service):
    service.record_match(G, snapshot())
    assert "可领取" not in service.handle(G, A, "gp")
    service.record_match(G, snapshot(source="official", match_id="MM@one"))
    assert "到账 35" in service.handle(G, A, "gp 领取")
    assert "到账 35" in service.handle(G, A, "领取")
    service.bindings.bind("elsewhere", A, S1)
    assert "可领取" not in service.handle("elsewhere", A)
    assert service.community.wallet(G, A)[0] == 170


@pytest.mark.parametrize("ended", [
    "未知时间", "", "2026-10-16 20:00:00", "2026-10-19 10:00:00",
    "2026-10-18 23:00:00",
])
def test_invalid_old_or_future_data_never_becomes_todays_progress(service, ended):
    service.record_match(G, snapshot(ended_at=ended))
    assert "可领取" not in service.handle(G, A)
    assert service.community.peek_wallet(G, A) is None


def test_utc_end_time_is_bucketed_in_beijing_and_yesterday_is_claimable(service):
    # 18:00 UTC on the 17th is 02:00 Beijing on the 18th.
    service.record_match(G, snapshot(ended_at="2026-10-17T18:00:00Z"))
    assert "到账 35" in service.handle(G, A, "领取")
    service.record_match(G, snapshot(ended_at="2026-10-17T12:00:00Z"))
    assert "到账 35" in service.handle(G, A, "昨天 领取")
    assert service.community.wallet(G, A)[0] == 170
    service.clock = lambda: NOW + timedelta(days=1)
    assert "已领取" in service.handle(G, A, "昨天")


def test_roster_missing_cannot_invent_a_teammate_win(service):
    service.record_match(G, snapshot(players=()))
    assert "到账 20" in service.handle(G, A, "领取")
    assert "可领取" not in service.handle(G, B)


@pytest.mark.parametrize("changes", [
    {"team1_score": "-"}, {"team2_score": "?"},
    {"team1_score": "12", "team2_score": "12"},
    {"result": "loss"}, {"result": "draw"},
])
def test_unknown_scores_and_draws_do_not_count_as_wins(service, changes):
    service.record_match(G, snapshot(**changes))
    assert "到账 20" in service.handle(G, A, "领取")


def test_duplicate_player_or_unbound_teammate_cannot_unlock_team_win(service):
    match = snapshot()
    service.record_match(G, replace(match, players=(match.players[0], match.players[0])))
    assert "可领取" not in service.handle(G, A).split("双排凯旋")[1]
    service.bindings.unbind(G, B)
    service.record_match(G, match)
    assert "到账 20" in service.handle(G, A, "领取")


def test_partial_payload_cannot_erase_completed_progress(service):
    service.record_match(G, snapshot())
    match = snapshot()
    partial = replace(match.players[0], kills="-", assists="-", team=0)
    service.record_match(G, replace(match, players=(partial,), kills="0", assists="0"))
    assert "到账 35" in service.handle(G, A, "领取")


def test_unbound_and_invalid_commands_do_not_create_wallets(service):
    assert "先 #绑定" in service.handle(G, "99999", "领取")
    with pytest.raises(ValueError):
        service.handle(G, A, "领取 99999")
    with pytest.raises(ValueError):
        service.handle(G, A, "昨天 今天")
    assert service.community.peek_wallet(G, A) is None


def test_tip_replay_keeps_transfer_and_fee_exactly_once(service):
    repo = service.community
    result = repo.tip(G, A, B, 50, request_id="message-1")
    assert result == (45, 5, 50)
    assert repo.tip(G, A, B, 50, request_id="message-1") == result
    assert repo.wallet(G, A)[0] == 50
    assert repo.wallet(G, B)[0] == 145
    with pytest.raises(CommunityError, match="同一条消息"):
        repo.tip(G, A, C, 50, request_id="message-1")
    assert repo.peek_wallet(G, C) is None


def test_concurrent_tip_retries_and_insufficient_funds(service):
    repo = service.community
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda _: repo.tip(G, A, B, 80, request_id="same"), range(4)))
    assert all(reply == (72, 8, 20) for reply in replies)
    with pytest.raises(CommunityError, match="不足"):
        repo.tip(G, A, C, 50, request_id="insufficient")
    assert repo.peek_wallet(G, C) is None
    assert repo.wallet(G, A)[0] == 20


async def test_commands_route_interactions_and_pass_tip_message_id(service):
    play = GroupPlayService(service.community, service.bindings, prediction_enabled=False)
    commands = CommandService(service.bindings, AsyncMock(), group_play=play)
    commands.challenges = service
    for alias, action in [("挑战", "challenge"), ("投票", "poll"), ("分队", "party_balance")]:
        assert commands.parse(f"/{alias}").action == action
    assert "竞猜" not in (await commands.handle(ParsedCommand("interaction_help"),
                                             group_id=G, qq_id=A))
    assert "投票" in (await commands.handle(ParsedCommand("help", "互动"), group_id=G, qq_id=A))
    poll = await commands.handle(ParsedCommand("poll", "发起 今晚玩什么|CS2|瓦"),
                                 group_id=G, qq_id=A)
    assert "今晚玩什么" in poll
    for _ in range(2):
        await commands.handle_result(ParsedCommand("coin_tip", f"@{B} 50"),
                                     group_id=G, qq_id=A, request_id="tip-command")
    assert service.community.wallet(G, A)[0] == 50


async def test_history_query_updates_challenges_without_extra_api_calls(service):
    api = AsyncMock()
    api.match_list.return_value = [{
        "matchId": "PVP@query", "endTime": "2026-10-18 21:00:00",
        "kill": 22, "assist": 8, "score1": 13, "score2": 7,
    }]
    api.detail_stats.return_value = {}
    play = GroupPlayService(service.community, service.bindings)
    commands = CommandService(service.bindings, api, group_play=play)
    commands.challenges = service
    result = await commands.handle_result(ParsedCommand("matches"), group_id=G, qq_id=A)
    assert result.match_history is not None
    assert "到账 20" in service.handle(G, A, "领取")
    api.match_list.assert_awaited_once()
    api.detail_stats.assert_awaited_once()


async def test_storage_error_returns_safe_reply_instead_of_silence(service, monkeypatch):
    play = GroupPlayService(service.community, service.bindings)
    commands = CommandService(service.bindings, AsyncMock(), group_play=play)

    def failed(*args, **kwargs):
        raise sqlite3.OperationalError("locked /private/server/path")

    monkeypatch.setattr(commands.polls, "handle", failed)
    result = await commands.handle(ParsedCommand("poll"), group_id=G, qq_id=A)
    assert "稍后重试" in result
    assert "/private" not in result


async def test_channel_records_progress_without_an_extra_reward_message(tmp_path, monkeypatch):
    from nanobot.bus.queue import MessageBus

    from pwcs2_bot.channel import PWCS2Channel

    channel = PWCS2Channel({
        "enabled": True, "allowFrom": ["*"], "pwToken": "test-token",
        "databasePath": str(tmp_path / "channel.db"), "statsImageEnabled": False,
    }, MessageBus())
    channel.repository.bind(G, A, S1)
    channel.commands.challenges.clock = lambda: NOW
    reply = AsyncMock()
    monkeypatch.setattr(channel, "_reply", reply)
    try:
        await channel._send_match_notification(G, snapshot(players=()))
        assert reply.await_count == 1
        assert "到账 20" in channel.commands.challenges.handle(G, A, "领取")
    finally:
        await channel.stop()
