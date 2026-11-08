from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from pwcs2_bot.commands import CommandService, ParsedCommand, parse_steam_id
from pwcs2_bot.community import CommunityRepository, GroupPlayService
from pwcs2_bot.database import BindingRepository
from pwcs2_bot.group_ai_memory import GroupAIMemoryService
from pwcs2_bot.perfectworld import PerfectWorldApiError
from pwcs2_bot.steam_presence import SteamPresence, SteamPresenceError
from pwcs2_bot.teamspeak import TeamSpeakChannel, TeamSpeakMember, TeamSpeakStatus
from pwcs2_bot.valorant import (
    ValorantQueryError,
    ValorantSnapshot,
    valorant_snapshot_to_payload,
)
from pwcs2_bot.watch_stage import WatchStagePlayer, WatchStageSnapshot


class FakeApi:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.v2_queries: list[str] = []
        self.match_queries: list[tuple[str, int]] = []
        self.official_match_queries: list[tuple[str, int]] = []
        self.official_matches: list[dict[str, Any]] = []
        self.official_error = False

    async def detail_stats(self, steam_id: str) -> dict[str, Any]:
        self.queries.append(steam_id)
        return {
            "name": "Test Player",
            "steamId": steam_id,
            "seasonId": "S20",
            "pvpScore": 1800,
            "pwRating": 1.23,
            "cnt": 10,
            "winRate": 0.6,
            "kills": 200,
            "deaths": 100,
            "assists": 30,
            "adr": 88.5,
            "headShotRatio": 0.5,
        }

    async def detail_stats_v2(self, steam_id: str) -> dict[str, Any]:
        self.v2_queries.append(steam_id)
        offset = int(steam_id[-1])
        return {
            "name": f"Player {offset}",
            "steamId": steam_id,
            "seasonId": "S24",
            "cnt": 20,
            "pvpScore": 1800 + offset,
            "pwRating": 1.1 + offset / 100,
            "winRate": 0.5,
            "adr": 80 + offset,
            "headShotRatio": 0.45,
            "radar": {"marksmanship": {"detail": {"rapid_stop_rate_raw": 0.7 + offset / 100}}},
        }

    async def match_list(self, steam_id: str, *, page_size: int) -> list[dict[str, Any]]:
        self.match_queries.append((steam_id, page_size))
        return [
            {
                "matchId": f"PVP@{index}",
                "timeStamp": 1000 - index,
                "endTime": f"2026-07-01 20:{index:02d}:00",
                "mapName": "荒漠迷城",
                "team": 1,
                "winTeam": 1 if index % 2 else 2,
                "score1": 13 if index % 2 else 8,
                "score2": 8 if index % 2 else 13,
                "kill": 20,
                "assist": 5,
                "death": 10,
                "pwRating": 1.2,
                "we": 10,
                "pvpScoreChange": 12 if index % 2 else -10,
            }
            for index in range(10)
        ]

    async def official_match_list(
        self,
        steam_id: str,
        *,
        page_size: int,
    ) -> list[dict[str, Any]]:
        self.official_match_queries.append((steam_id, page_size))
        if self.official_error:
            raise PerfectWorldApiError("official source unavailable")
        return self.official_matches[:page_size]


class FakeTeamSpeak:
    async def status(self) -> TeamSpeakStatus:
        return TeamSpeakStatus(
            server_name="牛牛精灵 · CS2 黑金开黑大厅",
            max_clients=32,
            channels=(
                TeamSpeakChannel(
                    channel_id=1,
                    name="◆・综合大厅",
                    members=(TeamSpeakMember("ExamplePlayer"),),
                ),
            ),
        )


class FakeWatchStage:
    def __init__(self) -> None:
        self.queries: list[tuple[str, str | None]] = []
        self.snapshots: dict[str, WatchStageSnapshot] = {}

    async def snapshot(
        self,
        steam_id: str,
        *,
        platform: str | None = None,
    ) -> WatchStageSnapshot:
        self.queries.append((steam_id, platform))
        return self.snapshots.get(steam_id) or WatchStageSnapshot(
            status="no_active_match",
            requested_steam_id=steam_id,
            requested_name="",
            platform=platform or "2",
            match_id="",
            map_name="",
            ct_score=None,
            terrorist_score=None,
            duration="",
            warmup=False,
            updated_at="2026-07-27T00:00:00+00:00",
            players=(),
        )


class FakeSteamPresence:
    configured = True

    def __init__(self) -> None:
        self.queries: list[tuple[str, ...]] = []
        self.presences: dict[str, SteamPresence] = {}
        self.error: SteamPresenceError | None = None

    async def summaries(
        self,
        steam_ids: list[str] | tuple[str, ...],
    ) -> dict[str, SteamPresence]:
        normalized = tuple(steam_ids)
        self.queries.append(normalized)
        if self.error is not None:
            raise self.error
        return {
            steam_id: self.presences[steam_id]
            for steam_id in normalized
            if steam_id in self.presences
        }


class FakeValorant:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.error: ValorantQueryError | None = None

    async def query(self, riot_id: str) -> ValorantSnapshot:
        self.queries.append(riot_id)
        if self.error is not None:
            raise self.error
        return ValorantSnapshot(
            riot_id=riot_id,
            rank="黄金 1",
            record="9胜1负",
            win_rate="90%",
            kda="1.22",
            acs="222",
            rr_change="+174",
            source_updated_at="更新时间 2026-07-31 12:00:00",
            fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
            matches=(),
        )


@pytest.fixture
def service(tmp_path: Path) -> CommandService:
    return CommandService(
        BindingRepository(tmp_path / "db.sqlite"),
        FakeApi(),
        prefix="#",
        teamspeak=FakeTeamSpeak(),
        steam_presence=FakeSteamPresence(),  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("#绑定 76561198929215155", ParsedCommand("bind", "76561198929215155")),
        ("#瓦绑定 ExamplePlayer#12345", ParsedCommand("valorant_bind", "ExamplePlayer#12345")),
        ("#瓦战绩", ParsedCommand("valorant_stats", "")),
        ("#瓦战绩 @123456", ParsedCommand("valorant_stats", "@123456")),
        ("#我的瓦绑定", ParsedCommand("valorant_binding", "")),
        ("#瓦解绑", ParsedCommand("valorant_unbind", "")),
        ("/stats @123456", ParsedCommand("stats", "@123456")),
        ("#数据 @123456", ParsedCommand("stats", "@123456")),
        ("#数据 @123456 gp", ParsedCommand("stats", "@123456 gp")),
        ("#战绩 @123456", ParsedCommand("matches", "@123456")),
        ("#战绩 gp @123456", ParsedCommand("matches", "gp @123456")),
        ("#观战 @123456", ParsedCommand("spectate", "@123456")),
        ("#观战 gp @123456", ParsedCommand("spectate", "gp @123456")),
        ("#谁在打", ParsedCommand("playing", "")),
        ("#谁在打 gp", ParsedCommand("playing", "gp")),
        ("#游戏状态", ParsedCommand("game_status", "")),
        ("#游戏状态 @123456", ParsedCommand("game_status", "@123456")),
        ("#Steam状态", ParsedCommand("game_status", "")),
        (" #我的绑定 ", ParsedCommand("binding", "")),
        ("#排行 急停", ParsedCommand("ranking", "急停")),
        ("#排行 gp Rating", ParsedCommand("ranking", "gp Rating")),
        ("#ts", ParsedCommand("teamspeak", "")),
        ("#TS状态", ParsedCommand("teamspeak", "")),
        ("#开黑 5 22:30", ParsedCommand("party_create", "5 22:30")),
        (
            "#发车晚上八点荒漠迷城五人车",
            ParsedCommand("party_create", "晚上八点荒漠迷城五人车"),
        ),
        ("#上车", ParsedCommand("party_join", "")),
        ("#上车荒漠", ParsedCommand("party_join", "荒漠")),
        ("#下车", ParsedCommand("party_leave", "")),
        ("#车队 12", ParsedCommand("party_status", "12")),
        ("#羁绊 @123456", ParsedCommand("chemistry", "@123456")),
        ("#羁绊 gp @123456", ParsedCommand("chemistry", "gp @123456")),
        ("#关闭锐评", ParsedCommand("commentary_off", "")),
        ("#开启锐评", ParsedCommand("commentary_on", "")),
        ("#牛币", ParsedCommand("coin_balance", "")),
        ("#签到", ParsedCommand("checkin", "")),
        ("#牛币获取", ParsedCommand("coin_earn", "")),
        ("#牛币流水", ParsedCommand("coin_ledger", "")),
        ("#打赏 @123456 50", ParsedCommand("coin_tip", "@123456 50")),
        ("#商店", ParsedCommand("shop", "")),
        ("#使用称号 吊大指挥官", ParsedCommand("custom_badge", "吊大指挥官")),
        ("#提示", ParsedCommand("quiz_hint", "")),
        ("#战绩背景 赛博霓虹", ParsedCommand("stats_background", "赛博霓虹")),
        (
            "#发起竞猜 今晚谁赢 | 牛牛 | 精灵",
            ParsedCommand("prediction_create", "今晚谁赢 | 牛牛 | 精灵"),
        ),
        ("#下注 2 50", ParsedCommand("prediction_bet", "2 50")),
        ("#选手 B", ParsedCommand("grenade_quiz", "B")),
        ("#职业选手 C", ParsedCommand("grenade_quiz", "C")),
        ("#每日一题 D", ParsedCommand("grenade_quiz", "D")),
        ("#投掷物 B", ParsedCommand("grenade_quiz", "B")),
        ("#黑历史 @123456", ParsedCommand("history", "@123456")),
        ("#复仇", ParsedCommand("revenge", "")),
        ("#改名 @123456 牛牛战神", ParsedCommand("card_rename", "@123456 牛牛战神")),
        ("#记住 群里每周五十点开黑", ParsedCommand("memory_add", "群里每周五十点开黑")),
        ("#记忆", ParsedCommand("memory_list", "")),
        ("#忘记 3", ParsedCommand("memory_delete", "3")),
        (
            "#饰品添加 AK-47 | Redline (Field-Tested)",
            ParsedCommand("skin_add", "AK-47 | Redline (Field-Tested)"),
        ),
        ("#饰品删除 3", ParsedCommand("skin_remove", "3")),
        ("#饰品列表", ParsedCommand("skin_list", "")),
        ("#饰品价格", ParsedCommand("skin_prices", "")),
        ("#", ParsedCommand("help", "")),
        ("普通消息", None),
        ("#不存在", None),
    ],
)
def test_parse_commands(service: CommandService, text: str, expected: ParsedCommand | None) -> None:
    assert service.parse(text) == expected


async def test_ai_memory_commands_add_list_and_delete_with_permissions(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "db.sqlite")
    memory = GroupAIMemoryService(repository)
    commands = CommandService(
        repository,
        FakeApi(),  # type: ignore[arg-type]
        group_ai_memory=memory,
    )

    added = await commands.handle_result(
        ParsedCommand("memory_add", "群里每周五晚上十点开黑"),
        group_id="100",
        qq_id="100001",
        nickname="Alice",
    )
    listed = await commands.handle_result(
        ParsedCommand("memory_list"),
        group_id="100",
        qq_id="100002",
    )
    entry = repository.list_group_ai_memories("100")[0]
    denied = await commands.handle_result(
        ParsedCommand("memory_delete", str(entry.memory_id)),
        group_id="100",
        qq_id="100002",
        sender_role="member",
    )
    owner_deleted = await commands.handle_result(
        ParsedCommand("memory_delete", str(entry.memory_id)),
        group_id="100",
        qq_id="100002",
        sender_role="owner",
    )

    assert "已加入本群 AI 长期记忆" in added.content
    assert "Alice：群里每周五晚上十点开黑" in listed.content
    assert denied.content == "操作失败：你只能删除自己添加的长期记忆"
    assert f"已忘记本群长期记忆 #{entry.memory_id}" in owner_deleted.content
    assert repository.list_group_ai_memories("100") == ()


@pytest.mark.asyncio
async def test_disabled_predictions_are_hidden_and_rejected(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    repository = BindingRepository(path)
    group_play = GroupPlayService(
        CommunityRepository(path),
        repository,
        prediction_enabled=False,
    )
    service = CommandService(
        repository,
        FakeApi(),  # type: ignore[arg-type]
        group_play=group_play,
    )

    assert "#竞猜" not in service.help_text()
    assert "自动发起" not in service.help_text()
    result = await service.handle_result(
        ParsedCommand("prediction_bet", "1 10"),
        group_id="100",
        qq_id="10001",
    )

    assert result.content == "操作失败：竞猜功能已关闭"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("76561198929215155", "76561198929215155"),
        (
            "https://steamcommunity.com/profiles/76561198929215155/",
            "76561198929215155",
        ),
        ("https://evil.example/profiles/76561198929215155", None),
        ("STEAM_0:1:123", None),
        ("7656119", None),
    ],
)
def test_parse_steam_id(value: str, expected: str | None) -> None:
    assert parse_steam_id(value) == expected


async def test_valorant_bind_and_query_self_member_and_direct_id(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "db.sqlite")
    valorant = FakeValorant()
    service = CommandService(
        repository,
        FakeApi(),  # type: ignore[arg-type]
        valorant=valorant,  # type: ignore[arg-type]
        query_cooldown_seconds=0,
    )

    first_bind = await service.handle(
        ParsedCommand("valorant_bind", "ExamplePlayer#12345"),
        group_id="100",
        qq_id="123456",
    )
    second_bind = await service.handle(
        ParsedCommand("valorant_bind", "Teammate#12345"),
        group_id="100",
        qq_id="123457",
    )
    own = await service.handle_result(
        ParsedCommand("valorant_stats"),
        group_id="100",
        qq_id="123456",
    )
    teammate = await service.handle_result(
        ParsedCommand("valorant_stats", "@123457"),
        group_id="100",
        qq_id="123456",
    )
    direct = await service.handle_result(
        ParsedCommand("valorant_stats", "Direct#99999"),
        group_id="100",
        qq_id="123456",
    )

    assert "ExamplePlayer#12345" in first_bind
    assert "Teammate#12345" in second_bind
    assert own.valorant is not None and own.valorant.riot_id == "ExamplePlayer#12345"
    assert teammate.valorant is not None
    assert teammate.valorant.riot_id == "Teammate#12345"
    assert direct.valorant is not None and direct.valorant.riot_id == "Direct#99999"
    assert valorant.queries == [
        "ExamplePlayer#12345",
        "Teammate#12345",
        "Direct#99999",
    ]


async def test_valorant_source_failure_uses_recent_stale_cache(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "db.sqlite")
    fetched_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    cached = ValorantSnapshot(
        riot_id="ExamplePlayer#12345",
        rank="黄金 1",
        record="9胜1负",
        win_rate="90%",
        kda="1.22",
        acs="222",
        rr_change="+174",
        source_updated_at="更新时间 2026-07-31 12:00:00",
        fetched_at=fetched_at,
        matches=(),
    )
    repository.bind_valorant("100", "123456", cached.riot_id)
    repository.save_valorant_cache(
        cached.riot_id,
        valorant_snapshot_to_payload(cached),
        fetched_at=fetched_at,
    )
    valorant = FakeValorant()
    valorant.error = ValorantQueryError("源站 502")
    service = CommandService(
        repository,
        FakeApi(),  # type: ignore[arg-type]
        valorant=valorant,  # type: ignore[arg-type]
        query_cooldown_seconds=0,
        valorant_cache_ttl_seconds=60,
        valorant_stale_ttl_seconds=86400,
    )

    result = await service.handle_result(
        ParsedCommand("valorant_stats"),
        group_id="100",
        qq_id="123456",
    )

    assert result.valorant is not None
    assert result.valorant.cache_status == "stale"
    assert "显示缓存数据" in result.content
    assert "源站 502" in result.content
    assert valorant.queries == ["ExamplePlayer#12345"]


async def test_help_only_documents_gp_for_source_specific_queries(
    service: CommandService,
) -> None:
    help_text = await service.handle(
        ParsedCommand("help"),
        group_id="100",
        qq_id="123456",
    )

    assert "#观战 [@群友/QQ号/SteamID64]" in help_text
    assert "#谁在打　" in help_text
    assert "#观战 [gp]" not in help_text
    assert "#谁在打 [gp]" not in help_text
    assert "观战和谁在打会自动识别完美或官匹" in help_text


async def test_bind_and_query_self(service: CommandService) -> None:
    service.repository.increment_unbound_message_count("100", "123456", "2026-07-02")
    result = await service.handle(
        ParsedCommand("bind", "76561198929215155"), group_id="100", qq_id="123456"
    )
    stats = await service.handle(ParsedCommand("stats"), group_id="100", qq_id="123456")

    assert "绑定成功" in result
    assert "Test Player" in stats
    assert "K/D：2" in stats
    assert service.repository.get_unbound_message_count("100", "123456", "2026-07-02") == 0


async def test_query_bound_group_member(service: CommandService) -> None:
    await service.handle(ParsedCommand("bind", "76561198929215155"), group_id="100", qq_id="123456")

    stats = await service.handle(ParsedCommand("stats", "@123456"), group_id="100", qq_id="999999")
    stats_by_qq = await service.handle(
        ParsedCommand("stats", "123456"), group_id="100", qq_id="999998"
    )
    other_group = await service.handle(
        ParsedCommand("stats", "@123456"), group_id="200", qq_id="999999"
    )

    assert "Test Player" in stats
    assert "Test Player" in stats_by_qq
    assert "还没有在本群绑定" in other_group


async def test_stats_result_exposes_normalized_card_data(service: CommandService) -> None:
    result = await service.handle_result(
        ParsedCommand("stats", "76561198929215155"),
        group_id="100",
        qq_id="999999",
    )

    assert result.stats is not None
    assert result.stats.name == "Test Player"
    assert result.stats.rating == "1.23"
    assert result.stats.win_rate == "60.0%"
    assert result.stats.source == "pvp"
    assert result.content.startswith("CS2 完美数据｜Test Player")
    assert service.api.queries == ["76561198929215155"]
    assert service.api.official_match_queries == []


@pytest.mark.parametrize(
    ("action", "argument"),
    [
        ("stats", "@222222"),
        ("stats", "gp @222222"),
        ("matches", "@222222"),
        ("matches", "gp @222222"),
    ],
)
async def test_personal_card_result_uses_target_group_badge(
    tmp_path: Path,
    action: str,
    argument: str,
) -> None:
    path = tmp_path / "badges.db"
    bindings = BindingRepository(path)
    community = CommunityRepository(path)
    group_play = GroupPlayService(community, bindings)
    api = FakeApi()
    commands = CommandService(
        bindings,
        api,  # type: ignore[arg-type]
        query_cooldown_seconds=0,
        group_play=group_play,
    )
    steam_id = "76561198929215155"
    bindings.bind("100", "222222", steam_id)
    community.add_coins(
        "100", "222222", 300, reason="test", reference_key="target-badge-credit"
    )
    group_play.buy("100", "222222", "5")
    group_play.use_custom_badge("100", "222222", "残局指挥官")

    result = await commands.handle_result(
        ParsedCommand(action, argument),  # type: ignore[arg-type]
        group_id="100",
        qq_id="999999",
    )

    assert result.member_badge == "残局指挥官"


async def test_stats_badge_uses_resolved_target_when_api_omits_steam_id(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing-steam-id.db"
    bindings = BindingRepository(path)
    community = CommunityRepository(path)
    group_play = GroupPlayService(community, bindings)
    api = FakeApi()
    commands = CommandService(
        bindings,
        api,  # type: ignore[arg-type]
        query_cooldown_seconds=0,
        group_play=group_play,
    )
    steam_id = "76561198929215155"
    bindings.bind("100", "222222", steam_id)
    community.add_coins(
        "100", "222222", 300, reason="test", reference_key="missing-id-credit"
    )
    group_play.buy("100", "222222", "5")
    group_play.use_custom_badge("100", "222222", "不丢道具")

    async def detail_stats_without_id(_steam_id: str) -> dict[str, Any]:
        return {"name": "No Steam ID"}

    api.detail_stats = detail_stats_without_id  # type: ignore[method-assign]
    result = await commands.handle_result(
        ParsedCommand("stats", "@222222"),
        group_id="100",
        qq_id="999999",
    )

    assert result.member_badge == "不丢道具"


async def test_unbound_direct_steam_query_does_not_create_badge_wallet(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unbound-steam.db"
    bindings = BindingRepository(path)
    community = CommunityRepository(path)
    commands = CommandService(
        bindings,
        FakeApi(),  # type: ignore[arg-type]
        query_cooldown_seconds=0,
        group_play=GroupPlayService(community, bindings),
    )

    result = await commands.handle_result(
        ParsedCommand("stats", "76561198929215155"),
        group_id="100",
        qq_id="999999",
    )

    assert result.member_badge == ""
    with community._connect() as connection:
        wallet_count = connection.execute("SELECT COUNT(*) FROM coin_wallets").fetchone()[0]
    assert wallet_count == 0


async def test_stats_gp_uses_only_official_matches(service: CommandService) -> None:
    steam_id = "76561198929215155"
    service.repository.bind("100", "123456", steam_id)
    service.repository.upsert_group_member_identity("100", "123456", "官匹牛牛")
    service.api.match_detail = AsyncMock(return_value={
        "players": [
            {"playerId": "76561198000000001", "nickName": "队友的游戏名"},
            {"playerId": steam_id, "nickName": "𝓖𝓪𝓶𝓮 🎯"},
        ],
    })
    service.api.official_matches = [
        {
            "matchId": "LOCAL@1",
            "dataSource": 1,
            "timeStamp": 2000,
            "mapName": "炼狱小镇",
            "team": 1,
            "winTeam": 1,
            "score1": 13,
            "score2": 8,
            "kill": 24,
            "assist": 5,
            "death": 12,
            "pwRating": 0,
            "rating": 1.4,
        },
        {
            "matchId": "LOCAL@2",
            "dataSource": 1,
            "timeStamp": 1900,
            "mapName": "荒漠迷城",
            "team": 2,
            "winTeam": 1,
            "score1": 13,
            "score2": 9,
            "kill": 16,
            "assist": 3,
            "death": 20,
            "pwRating": 0,
            "rating": 0.8,
        },
    ]

    result = await service.handle_result(
        ParsedCommand("stats", "gp"),
        group_id="100",
        qq_id="123456",
    )

    assert result.stats is not None
    assert result.stats.source == "official"
    assert result.stats.name == "𝓖𝓪𝓶𝓮 🎯"
    assert result.stats.matches == "2"
    assert result.stats.score == "1-1-0"
    assert result.stats.rating == "1.1"
    assert result.stats.adr == "-"
    assert result.stats.headshot_rate == "-"
    assert result.stats.rws == "-"
    assert result.content.startswith("CS2 官匹数据｜𝓖𝓪𝓶𝓮 🎯")
    assert "官匹牛牛" not in result.content
    service.api.match_detail.assert_awaited_once_with("LOCAL@1", data_source=1)
    assert service.api.queries == []
    assert service.api.match_queries == []
    assert service.api.official_match_queries == [(steam_id, 50)]


@pytest.mark.parametrize("action", ["stats", "matches"])
@pytest.mark.parametrize("has_game_name", [True, False])
async def test_official_card_identity_never_falls_back_to_qq_memory(
    service: CommandService, action: str, has_game_name: bool,
) -> None:
    steam_id = "76561198929215155"
    service.repository.bind("100", "123456", steam_id)
    service.repository.upsert_group_member_identity("100", "123456", "不是游戏ID")
    service.api.official_matches = [{"matchId": "LOCAL@new"}]
    service.api.match_detail = AsyncMock(return_value={"players": [
        {"playerId": steam_id, "nickName": "Game ID" if has_game_name else ""},
    ]})
    result = await service.handle_result(
        ParsedCommand(action, "gp @123456"), group_id="100", qq_id="999999",
    )
    snapshot = result.stats if action == "stats" else result.match_history
    assert snapshot is not None
    assert snapshot.name == ("Game ID" if has_game_name else steam_id)
    assert "不是游戏ID" not in result.content
    assert service.api.queries == []
    # Display changes never rename QQ members or alter bindings.
    assert service.repository.get_by_qq("100", "123456").steam_id == steam_id
    assert service.repository.list_group_member_memories("100")[0].nickname == "不是游戏ID"


async def test_conflicting_source_selectors_fail_before_any_query(
    service: CommandService,
) -> None:
    result = await service.handle_result(
        ParsedCommand("stats", "gp pw 76561198929215155"),
        group_id="100",
        qq_id="999999",
    )

    assert result.stats is None
    assert result.content == "操作失败：不能同时指定完美和官匹数据源"
    assert service.api.queries == []
    assert service.api.match_queries == []
    assert service.api.official_match_queries == []


async def test_match_history_returns_latest_ten_matches(service: CommandService) -> None:
    result = await service.handle_result(
        ParsedCommand("matches", "76561198929215155"),
        group_id="100",
        qq_id="999999",
    )

    assert result.match_history is not None
    assert result.match_history.name == "Test Player"
    assert result.match_history.total == 10
    assert (result.match_history.wins, result.match_history.losses) == (5, 5)
    assert result.match_history.elo_change == "+10"
    assert result.match_history.source == "pvp"
    assert result.content.startswith("CS2 完美近十场战绩｜Test Player")
    assert service.api.match_queries == [("76561198929215155", 10)]
    assert service.api.official_match_queries == []


async def test_match_history_gp_uses_only_official_matches(service: CommandService) -> None:
    service.api.official_matches = [
        {
            "matchId": "LOCAL@new",
            "dataSource": 1,
            "timeStamp": 2000,
            "endTime": "2026-07-02 20:00:00",
            "mapName": "炼狱小镇",
            "mode": "国服竞技",
            "team": 1,
            "winTeam": 1,
            "score1": 13,
            "score2": 8,
            "kill": 25,
            "assist": 5,
            "death": 12,
            "rating": 1.4,
        }
    ]

    result = await service.handle_result(
        ParsedCommand("matches", "gp 76561198929215155"),
        group_id="100",
        qq_id="999999",
    )

    assert result.match_history is not None
    assert result.match_history.source == "official"
    assert result.match_history.total == 1
    assert result.match_history.matches[0].match_id == "LOCAL@new"
    assert result.match_history.matches[0].mode == "国服竞技"
    assert result.match_history.elo_change == "-"
    assert result.content.startswith("CS2 官匹近十场战绩｜")
    assert "ELO" not in result.content
    assert service.api.match_queries == []
    assert service.api.official_match_queries == [("76561198929215155", 10)]


async def test_default_match_history_does_not_touch_failed_official_source(
    service: CommandService,
) -> None:
    service.api.official_error = True

    result = await service.handle_result(
        ParsedCommand("matches", "76561198929215155"),
        group_id="100",
        qq_id="999999",
    )

    assert result.match_history is not None
    assert result.match_history.total == 10
    assert all(
        match.match_id.startswith("PVP@")
        for match in result.match_history.matches
    )
    assert service.api.match_queries == [("76561198929215155", 10)]
    assert service.api.official_match_queries == []


async def test_official_match_history_failure_does_not_fall_back_to_pvp(
    service: CommandService,
) -> None:
    service.api.official_error = True

    result = await service.handle_result(
        ParsedCommand("matches", "gp 76561198929215155"),
        group_id="100",
        qq_id="999999",
    )

    assert result.match_history is None
    assert result.content.startswith("操作失败：")
    assert service.api.official_match_queries == [("76561198929215155", 10)]
    assert service.api.match_queries == []


async def test_watch_stage_keeps_game_name_and_only_adds_group_member_flag(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "watch.db")
    repository.bind("100", "123456", "76561198929215155")
    repository.upsert_group_member_identity("100", "123456", "牛牛")
    watch_stage = FakeWatchStage()
    watch_stage.snapshots["76561198929215155"] = WatchStageSnapshot(
        status="running",
        requested_steam_id="76561198929215155",
        requested_name="",
        platform="1",
        match_id="official-live-match",
        map_name="de_mirage",
        ct_score=2,
        terrorist_score=1,
        duration="03:20",
        warmup=False,
        updated_at="2026-07-27T00:00:00+00:00",
        players=(
            WatchStagePlayer(
                steam_id="76561198929215155",
                side="CT",
                nickname="Game Player 🎯",
                avatar_url="",
                kills=4,
                assists=1,
                deaths=2,
                score=9,
                adr=80,
                headshot="50%",
                alive=True,
                pvp_score=None,
                avg_rating=None,
                avg_we=None,
            ),
        ),
    )
    service = CommandService(
        repository,
        FakeApi(),  # type: ignore[arg-type]
        watch_stage=watch_stage,  # type: ignore[arg-type]
        query_cooldown_seconds=0,
    )

    result = await service.handle_result(
        ParsedCommand("spectate", "@123456"),
        group_id="100",
        qq_id="999999",
    )

    assert watch_stage.queries == [("76561198929215155", None)]
    assert result.watch_stage is not None
    assert result.watch_stage.requested_name == "Game Player 🎯"
    assert result.watch_stage.players[0].nickname == "Game Player 🎯"
    assert result.watch_stage.players[0].is_group_member is True
    assert result.watch_stage.platform == "1"
    assert result.content.startswith("👁️ 官匹观战｜荒漠迷城")

    legacy_gp = await service.handle_result(
        ParsedCommand("spectate", "gp @123456"),
        group_id="100",
        qq_id="999998",
    )

    assert watch_stage.queries[-1] == ("76561198929215155", None)
    assert legacy_gp.watch_stage is not None
    assert legacy_gp.watch_stage.platform == "1"
    assert legacy_gp.content == result.content


async def test_who_is_playing_groups_members_in_the_same_live_match(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "playing.db")
    first_steam = "76561198929215155"
    second_steam = "76561198929215158"
    repository.bind("100", "123456", first_steam)
    repository.bind("100", "123457", second_steam)
    repository.upsert_group_member_identity("100", "123456", "牛牛")
    repository.upsert_group_member_identity("100", "123457", "精灵")
    watch_stage = FakeWatchStage()
    players = tuple(
        WatchStagePlayer(
            steam_id=steam_id,
            side="CT",
            nickname=name,
            avatar_url="",
            kills=0,
            assists=0,
            deaths=0,
            score=0,
            adr=0,
            headshot="-",
            alive=True,
            pvp_score=None,
            avg_rating=None,
            avg_we=None,
        )
        for steam_id, name in (
            (first_steam, "牛牛"),
            (second_steam, "精灵"),
        )
    )
    for steam_id in (first_steam, second_steam):
        watch_stage.snapshots[steam_id] = WatchStageSnapshot(
            status="running",
            requested_steam_id=steam_id,
            requested_name="",
            platform="2",
            match_id="same-match",
            map_name="de_mirage",
            ct_score=0,
            terrorist_score=0,
            duration="00:40",
            warmup=True,
            updated_at="2026-07-27T00:00:00+00:00",
            players=players,
        )
    service = CommandService(
        repository,
        FakeApi(),  # type: ignore[arg-type]
        watch_stage=watch_stage,  # type: ignore[arg-type]
        query_cooldown_seconds=0,
    )

    result = await service.handle_result(
        ParsedCommand("playing"),
        group_id="100",
        qq_id="999999",
    )

    assert "当前有 1 场可观战对局" in result.content
    assert "荒漠迷城 · de_mirage" in result.content
    assert "群友：牛牛、精灵" in result.content
    assert f"#观战 {first_steam}" in result.content
    assert watch_stage.queries == [
        (first_steam, None),
        (second_steam, None),
    ]

    legacy_gp = await service.handle_result(
        ParsedCommand("playing", "gp"),
        group_id="100",
        qq_id="999998",
    )

    assert legacy_gp.content == result.content
    assert f"#观战 gp {first_steam}" not in legacy_gp.content
    assert watch_stage.queries[-2:] == [
        (first_steam, None),
        (second_steam, None),
    ]


async def test_who_is_playing_keeps_platforms_separate_and_uses_configured_prefix(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "playing-sources.db")
    pvp_steam = "76561198929215155"
    official_steam = "76561198929215158"
    repository.bind("100", "123456", pvp_steam)
    repository.bind("100", "123457", official_steam)
    repository.upsert_group_member_identity("100", "123456", "牛牛")
    repository.upsert_group_member_identity("100", "123457", "精灵")
    watch_stage = FakeWatchStage()

    def live_snapshot(steam_id: str, platform: str) -> WatchStageSnapshot:
        return WatchStageSnapshot(
            status="running",
            requested_steam_id=steam_id,
            requested_name="",
            platform=platform,
            match_id="shared-id",
            map_name="de_nuke",
            ct_score=3,
            terrorist_score=2,
            duration="05:00",
            warmup=False,
            updated_at="2026-07-27T00:00:00+00:00",
            players=(
                WatchStagePlayer(
                    steam_id=steam_id,
                    side="CT",
                    nickname=steam_id,
                    avatar_url="",
                    kills=1,
                    assists=0,
                    deaths=1,
                    score=2,
                    adr=50,
                    headshot="-",
                    alive=True,
                    pvp_score=None,
                    avg_rating=None,
                    avg_we=None,
                ),
            ),
        )

    watch_stage.snapshots[pvp_steam] = live_snapshot(pvp_steam, "2")
    watch_stage.snapshots[official_steam] = live_snapshot(official_steam, "1")
    service = CommandService(
        repository,
        FakeApi(),  # type: ignore[arg-type]
        prefix="/",
        watch_stage=watch_stage,  # type: ignore[arg-type]
        query_cooldown_seconds=0,
    )

    result = await service.handle_result(
        ParsedCommand("playing"),
        group_id="100",
        qq_id="999999",
    )

    assert "当前有 2 场可观战对局" in result.content
    assert "完美" in result.content
    assert "官匹" in result.content
    assert f"/观战 {pvp_steam}" in result.content
    assert f"/观战 {official_steam}" in result.content
    assert "#观战" not in result.content
    assert "观战 gp" not in result.content
    assert watch_stage.queries == [
        (pvp_steam, None),
        (official_steam, None),
    ]


async def test_game_status_lists_playing_online_and_hidden_members(
    service: CommandService,
) -> None:
    first_steam = "76561198929215155"
    second_steam = "76561198929215158"
    third_steam = "76561198929215159"
    service.repository.bind("100", "123456", first_steam)
    service.repository.bind("100", "123457", second_steam)
    service.repository.bind("100", "123458", third_steam)
    service.repository.upsert_group_member_identity("100", "123456", "牛牛")
    service.repository.upsert_group_member_identity("100", "123457", "精灵")
    service.repository.upsert_group_member_identity("100", "123458", "隐身牛")
    presence = service.steam_presence
    assert isinstance(presence, FakeSteamPresence)
    presence.presences = {
        first_steam: SteamPresence(
            first_steam,
            "Steam 牛牛",
            "730",
            "1.2.3.4:27015",
            "Counter-Strike 2",
            1,
        ),
        second_steam: SteamPresence(
            second_steam,
            "Steam 精灵",
            "",
            "",
            "",
            3,
        ),
        third_steam: SteamPresence(
            third_steam,
            "Steam 隐身牛",
            "",
            "",
            "",
            0,
        ),
    }

    result = await service.handle_result(
        ParsedCommand("game_status"),
        group_id="100",
        qq_id="999999",
    )

    assert presence.queries == [(first_steam, second_steam, third_steam)]
    assert result.content.startswith("🎮 Steam 游戏状态｜1/3 人正在游戏")
    assert "🎯 牛牛｜Counter-Strike 2" in result.content
    assert "🟡 精灵｜离开" in result.content
    assert "离线、隐身或状态不可见：1 人" in result.content
    assert "隐身牛｜" not in result.content
    assert result.game_status is not None
    assert result.game_status.total_count == 3
    assert result.game_status.playing_count == 1
    assert result.game_status.hidden_count == 1
    assert [entry.name for entry in result.game_status.entries] == ["牛牛", "精灵"]
    assert result.game_status.entries[0].game_name == "Counter-Strike 2"


async def test_game_status_can_query_one_bound_member(service: CommandService) -> None:
    steam_id = "76561198929215155"
    service.repository.bind("100", "123456", steam_id)
    service.repository.upsert_group_member_identity("100", "123456", "牛牛")
    presence = service.steam_presence
    assert isinstance(presence, FakeSteamPresence)
    presence.presences[steam_id] = SteamPresence(
        steam_id,
        "Steam 牛牛",
        "570",
        "",
        "Dota 2",
        1,
    )

    result = await service.handle_result(
        ParsedCommand("game_status", "@123456"),
        group_id="100",
        qq_id="999999",
    )

    assert presence.queries == [(steam_id,)]
    assert result.content == ("🎮 Steam 游戏状态｜牛牛\n状态：正在游戏\n游戏：Dota 2\nAppID：570")
    assert result.game_status is not None
    assert result.game_status.title == "牛牛 的游戏状态"
    assert result.game_status.entries[0].status_kind == "playing"
    assert result.game_status.entries[0].app_id == "570"


async def test_game_status_falls_back_to_watch_stage_for_group(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "db.sqlite")
    first_steam = "76561198929215155"
    second_steam = "76561198929215158"
    third_steam = "76561198929215159"
    repository.bind("100", "123456", first_steam)
    repository.bind("100", "123457", second_steam)
    repository.bind("100", "123458", third_steam)
    repository.upsert_group_member_identity("100", "123456", "牛牛")
    repository.upsert_group_member_identity("100", "123457", "精灵")
    repository.upsert_group_member_identity("100", "123458", "隐身牛")
    presence = FakeSteamPresence()
    presence.error = SteamPresenceError("Steam 实时状态查询超时")
    watch_stage = FakeWatchStage()
    watch_stage.snapshots[first_steam] = WatchStageSnapshot(
        status="running",
        requested_steam_id=first_steam,
        requested_name="",
        platform="2",
        match_id="PVP@123",
        map_name="de_mirage",
        ct_score=2,
        terrorist_score=1,
        duration="03:20",
        warmup=False,
        updated_at="2026-07-27T00:00:00+00:00",
        players=(
            WatchStagePlayer(
                steam_id=first_steam,
                side="CT",
                nickname="牛牛",
                avatar_url="",
                kills=1,
                assists=0,
                deaths=0,
                score=2,
                adr=50,
                headshot="-",
                alive=True,
                pvp_score=None,
                avg_rating=None,
                avg_we=None,
            ),
        ),
    )
    service = CommandService(
        repository,
        FakeApi(),  # type: ignore[arg-type]
        steam_presence=presence,  # type: ignore[arg-type]
        watch_stage=watch_stage,  # type: ignore[arg-type]
        query_cooldown_seconds=0,
    )

    result = await service.handle_result(
        ParsedCommand("game_status"),
        group_id="100",
        qq_id="999999",
    )

    assert presence.queries == [(first_steam, second_steam, third_steam)]
    assert watch_stage.queries == [
        (first_steam, None),
        (second_steam, None),
        (third_steam, None),
    ]
    assert result.content.startswith("🎮 游戏状态（观将台降级）")
    assert "Steam 官方接口当前不可达" in result.content
    assert "🎯 牛牛｜Counter-Strike 2｜荒漠迷城 · de_mirage｜CT 2:1 T｜03:20" in (result.content)
    assert "其余 2 人状态暂无法确认" in result.content
    assert "精灵｜" not in result.content
    assert "隐身牛｜" not in result.content
    assert result.game_status is not None
    assert result.game_status.degraded
    assert result.game_status.playing_count == 1
    assert result.game_status.hidden_count == 2
    assert result.game_status.entries[0].detail == "荒漠迷城 · de_mirage · CT 2:1 T · 03:20"


async def test_game_status_falls_back_to_watch_stage_for_one_member(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "db.sqlite")
    steam_id = "76561198929215155"
    repository.bind("100", "123456", steam_id)
    repository.upsert_group_member_identity("100", "123456", "牛牛")
    presence = FakeSteamPresence()
    presence.error = SteamPresenceError("Steam 实时状态查询超时")
    watch_stage = FakeWatchStage()
    watch_stage.snapshots[steam_id] = WatchStageSnapshot(
        status="running",
        requested_steam_id=steam_id,
        requested_name="",
        platform="2",
        match_id="PVP@123",
        map_name="de_dust2",
        ct_score=0,
        terrorist_score=0,
        duration="",
        warmup=True,
        updated_at="2026-07-27T00:00:00+00:00",
        players=(
            WatchStagePlayer(
                steam_id=steam_id,
                side="CT",
                nickname="牛牛",
                avatar_url="",
                kills=0,
                assists=0,
                deaths=0,
                score=0,
                adr=0,
                headshot="-",
                alive=True,
                pvp_score=None,
                avg_rating=None,
                avg_we=None,
            ),
        ),
    )
    service = CommandService(
        repository,
        FakeApi(),  # type: ignore[arg-type]
        steam_presence=presence,  # type: ignore[arg-type]
        watch_stage=watch_stage,  # type: ignore[arg-type]
        query_cooldown_seconds=0,
    )

    result = await service.handle_result(
        ParsedCommand("game_status", "@123456"),
        group_id="100",
        qq_id="999999",
    )

    assert presence.queries == [(steam_id,)]
    assert watch_stage.queries == [(steam_id, None)]
    assert result.content == (
        "🎮 游戏状态（观将台降级）｜牛牛\n"
        "⚠️ Steam 官方接口当前不可达\n"
        "🎯 观将台已检测到正在进行的 Counter-Strike 2 对局\n"
        "地图：炙热沙城Ⅱ · de_dust2\n"
        "比分：CT 0:0 T｜热身中"
    )
    assert result.game_status is not None
    assert result.game_status.degraded
    assert result.game_status.entries[0].detail == "炙热沙城Ⅱ · de_dust2 · CT 0:0 T · 热身中"


async def test_game_status_watch_stage_fallback_rejects_unrelated_match(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "db.sqlite")
    steam_id = "76561198929215155"
    repository.bind("100", "123456", steam_id)
    repository.upsert_group_member_identity("100", "123456", "牛牛")
    presence = FakeSteamPresence()
    presence.error = SteamPresenceError("Steam 实时状态查询超时")
    watch_stage = FakeWatchStage()
    watch_stage.snapshots[steam_id] = WatchStageSnapshot(
        status="running",
        requested_steam_id=steam_id,
        requested_name="",
        platform="1",
        match_id="stale-match",
        map_name="de_mirage",
        ct_score=7,
        terrorist_score=4,
        duration="18:00",
        warmup=False,
        updated_at="2026-07-27T00:00:00+00:00",
        players=(
            WatchStagePlayer(
                steam_id="76561198000000999",
                side="CT",
                nickname="其他玩家",
                avatar_url="",
                kills=10,
                assists=2,
                deaths=6,
                score=22,
                adr=80,
                headshot="40%",
                alive=True,
                pvp_score=None,
                avg_rating=None,
                avg_we=None,
            ),
        ),
    )
    service = CommandService(
        repository,
        FakeApi(),  # type: ignore[arg-type]
        steam_presence=presence,  # type: ignore[arg-type]
        watch_stage=watch_stage,  # type: ignore[arg-type]
        query_cooldown_seconds=0,
    )

    result = await service.handle_result(
        ParsedCommand("game_status", "@123456"),
        group_id="100",
        qq_id="999999",
    )

    assert result.game_status is not None
    assert result.game_status.playing_count == 0
    assert result.game_status.entries[0].status_kind == "unknown"
    assert "未检测到正在进行的 CS2 对局" in result.content


async def test_game_status_watch_stage_fallback_does_not_guess_offline_state(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "db.sqlite")
    steam_id = "76561198929215155"
    repository.bind("100", "123456", steam_id)
    repository.upsert_group_member_identity("100", "123456", "牛牛")
    presence = FakeSteamPresence()
    presence.error = SteamPresenceError("Steam 实时状态查询超时")
    watch_stage = FakeWatchStage()
    service = CommandService(
        repository,
        FakeApi(),  # type: ignore[arg-type]
        steam_presence=presence,  # type: ignore[arg-type]
        watch_stage=watch_stage,  # type: ignore[arg-type]
        query_cooldown_seconds=0,
    )

    result = await service.handle_result(
        ParsedCommand("game_status", "@123456"),
        group_id="100",
        qq_id="999999",
    )

    assert "未检测到正在进行的 CS2 对局" in result.content
    assert "其他游戏、离线或隐身状态暂无法确认" in result.content
    assert "状态：离线" not in result.content
    assert result.game_status is not None
    assert result.game_status.entries[0].status_kind == "unknown"


async def test_group_ranking_fetches_bound_members_and_sorts_metric(
    service: CommandService,
) -> None:
    service.repository.bind("100", "123456", "76561198929215155")
    service.repository.bind("100", "123457", "76561198929215158")
    service.repository.bind("200", "223456", "76561198929215159")

    result = await service.handle_result(
        ParsedCommand("ranking", "急停"),
        group_id="100",
        qq_id="999999",
    )

    assert result.ranking is not None
    assert result.ranking.source == "pvp"
    assert result.ranking.metric == "rapid_stop"
    assert [entry.name for entry in result.ranking.entries] == ["Player 8", "Player 5"]
    assert result.ranking.entries[0].rapid_stop == pytest.approx(78.0)
    assert result.content.startswith("CS2 群内完美急停成功率排行榜")
    assert service.api.v2_queries == [
        "76561198929215155",
        "76561198929215158",
    ]
    assert service.api.official_match_queries == []


async def test_group_ranking_gp_uses_only_supported_official_metrics(
    service: CommandService,
) -> None:
    first_steam = "76561198929215155"
    second_steam = "76561198929215158"
    service.repository.bind("100", "123456", first_steam)
    service.repository.bind("100", "123457", second_steam)
    service.repository.upsert_group_member_identity("100", "123456", "牛牛")
    service.repository.upsert_group_member_identity("100", "123457", "精灵")
    service.api.official_matches = [
        {
            "matchId": "LOCAL@official",
            "dataSource": 1,
            "team": 1,
            "winTeam": 1,
            "kill": 20,
            "death": 10,
            "pwRating": 0,
            "rating": 1.25,
        }
    ]

    result = await service.handle_result(
        ParsedCommand("ranking", "gp"),
        group_id="100",
        qq_id="999999",
    )

    assert result.ranking is not None
    assert result.ranking.source == "official"
    assert result.ranking.metric == "rating"
    assert {entry.name for entry in result.ranking.entries} == {first_steam, second_steam}
    assert all(entry.elo is None for entry in result.ranking.entries)
    assert all(entry.adr is None for entry in result.ranking.entries)
    assert all(entry.rapid_stop is None for entry in result.ranking.entries)
    assert result.content.startswith("CS2 群内官匹Rating排行榜")
    assert "ELO" not in result.content
    assert "急停" not in result.content
    assert service.api.v2_queries == []
    assert sorted(service.api.official_match_queries) == [
        (first_steam, 50),
        (second_steam, 50),
    ]


async def test_group_ranking_rejects_unavailable_official_metric_without_query(
    service: CommandService,
) -> None:
    service.repository.bind("100", "123456", "76561198929215155")

    result = await service.handle_result(
        ParsedCommand("ranking", "gp ELO"),
        group_id="100",
        qq_id="999999",
    )

    assert result.ranking is None
    assert result.content == "操作失败：官匹排行仅支持：Rating、胜率、K/D、场次"
    assert service.api.v2_queries == []
    assert service.api.official_match_queries == []


async def test_invalid_binding_returns_actionable_error(service: CommandService) -> None:
    result = await service.handle(
        ParsedCommand("bind", "not-a-steam-id"), group_id="100", qq_id="123456"
    )
    assert result.startswith("操作失败：")
    assert "SteamID64" in result


async def test_teamspeak_status_lists_online_members(service: CommandService) -> None:
    result = await service.handle(ParsedCommand("teamspeak"), group_id="100", qq_id="123456")

    assert "在线 1/32" in result
    assert "ts.example.com" in result
    assert "◆・综合大厅（1人）" in result
    assert "ExamplePlayer" in result


async def test_chemistry_queries_two_bound_members(service: CommandService) -> None:
    service.repository.bind("100", "123456", "76561198929215155")
    service.repository.bind("100", "123457", "76561198929215158")
    service.repository.upsert_group_member_identity("100", "123456", "牛牛")
    service.repository.upsert_group_member_identity("100", "123457", "精灵")

    result = await service.handle_result(
        ParsedCommand("chemistry", "@123457"),
        group_id="100",
        qq_id="123456",
    )

    assert result.chemistry is not None
    assert result.chemistry.source == "pvp"
    assert result.chemistry.shared_matches == 10
    assert "牛牛 × 精灵" in result.content
    assert service.api.match_queries == [
        ("76561198929215155", 50),
        ("76561198929215158", 50),
    ]
    assert service.api.official_match_queries == []


async def test_chemistry_gp_queries_only_official_matches(
    service: CommandService,
) -> None:
    first_steam = "76561198929215155"
    second_steam = "76561198929215158"
    service.repository.bind("100", "123456", first_steam)
    service.repository.bind("100", "123457", second_steam)
    service.repository.upsert_group_member_identity("100", "123456", "牛牛")
    service.repository.upsert_group_member_identity("100", "123457", "精灵")
    service.api.official_matches = [
        {
            "matchId": "LOCAL@shared",
            "dataSource": 1,
            "team": 1,
            "winTeam": 1,
            "pwRating": 0,
            "rating": 1.2,
        }
    ]

    result = await service.handle_result(
        ParsedCommand("chemistry", "gp @123457"),
        group_id="100",
        qq_id="123456",
    )

    assert result.chemistry is not None
    assert result.chemistry.source == "official"
    assert result.chemistry.shared_matches == 1
    assert result.content.startswith("🤝 官匹群友羁绊｜牛牛 × 精灵")
    assert "双方最近 50 场官匹比赛" in result.content
    assert service.api.match_queries == []
    assert service.api.official_match_queries == [
        (first_steam, 50),
        (second_steam, 50),
    ]


async def test_members_can_toggle_match_commentary(service: CommandService) -> None:
    disabled = await service.handle(ParsedCommand("commentary_off"), group_id="100", qq_id="123456")
    status = await service.handle(
        ParsedCommand("commentary_status"), group_id="100", qq_id="123456"
    )
    enabled = await service.handle(ParsedCommand("commentary_on"), group_id="100", qq_id="123456")

    assert "已关闭" in disabled
    assert "关闭" in status
    assert "已开启" in enabled
