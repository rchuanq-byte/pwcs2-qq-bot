from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pwcs2_bot.auto_prediction import AutoPredictionService
from pwcs2_bot.community import CommunityRepository
from pwcs2_bot.database import BindingRepository
from pwcs2_bot.perfectworld import PerfectWorldApiError
from pwcs2_bot.steam_presence import SteamPresence
from pwcs2_bot.watch_stage import (
    WatchStageError,
    WatchStagePlayer,
    WatchStageSnapshot,
)

STEAM_ID = "76561198000000001"


def _match(
    match_id: str,
    ended_at: str,
    *,
    win: bool = True,
    data_source: int = 3,
) -> dict:
    return {
        "matchId": match_id,
        "steamName": "牛牛",
        "team": 1,
        "winTeam": 1 if win else 2,
        "score1": 13 if win else 8,
        "score2": 8 if win else 13,
        "kill": 20,
        "death": 10,
        "pwRating": 1.5,
        "endTime": ended_at,
        "dataSource": data_source,
    }


class FakeApi:
    def __init__(self) -> None:
        self.matches = [_match("old", "2026-07-21 20:00:00")]
        self.official_matches = [
            _match(
                "LOCAL@old",
                "2026-07-21 20:00:00",
                data_source=1,
            )
        ]
        self.details: dict[tuple[str, int], dict] = {}
        self.fail = False

    async def match_list(self, steam_id: str, *, page_size: int) -> list[dict]:
        if self.fail:
            raise PerfectWorldApiError("temporary")
        return self.matches[:page_size]

    async def official_match_list(
        self,
        steam_id: str,
        *,
        page_size: int,
    ) -> list[dict]:
        if self.fail:
            raise PerfectWorldApiError("temporary")
        return self.official_matches[:page_size]

    async def match_detail(self, match_id: str, *, data_source: int) -> dict:
        return self.details.get((match_id, data_source), {})


class FakePresence:
    configured = True

    def __init__(self) -> None:
        self.current = SteamPresence(STEAM_ID, "牛牛", "", "")

    async def summaries(self, steam_ids: tuple[str, ...]) -> dict[str, SteamPresence]:
        return {STEAM_ID: self.current}


class FakeWatchStage:
    enabled = True

    def __init__(self) -> None:
        self.current = _watch_snapshot()
        self.fail = False
        self.queries: list[str] = []

    async def snapshot(self, steam_id: str) -> WatchStageSnapshot:
        self.queries.append(steam_id)
        if self.fail:
            raise WatchStageError("temporary")
        return self.current


def _watch_snapshot(
    *,
    match_id: str = "",
    platform: str = "2",
    warmup: bool = False,
    ct_score: int | None = None,
    terrorist_score: int | None = None,
    include_target: bool = True,
    player_count: int = 1,
) -> WatchStageSnapshot:
    players = tuple(
        WatchStagePlayer(
            steam_id=(
                STEAM_ID
                if index == 0 and include_target
                else str(int(STEAM_ID) + index + (0 if include_target else 100))
            ),
            side="CT" if index < max(1, player_count // 2) else "TERRORIST",
            nickname="牛牛" if index == 0 and include_target else f"玩家{index + 1}",
            avatar_url="",
            kills=0,
            assists=0,
            deaths=0,
            score=0,
            adr=0.0,
            headshot="-",
            alive=True,
            pvp_score=None,
            avg_rating=None,
            avg_we=None,
        )
        for index in range(player_count if match_id else 0)
    )
    if match_id and not players and include_target:
        players = (
            WatchStagePlayer(
                steam_id=STEAM_ID,
                side="CT",
                nickname="牛牛",
                avatar_url="",
                kills=0,
                assists=0,
                deaths=0,
                score=0,
                adr=0.0,
                headshot="-",
                alive=True,
                pvp_score=None,
                avg_rating=None,
                avg_we=None,
            ),
        )
    return WatchStageSnapshot(
        status="running" if match_id else "no_active_match",
        requested_steam_id=STEAM_ID,
        requested_name="",
        platform=platform,
        match_id=match_id,
        map_name="de_mirage" if match_id else "",
        ct_score=ct_score,
        terrorist_score=terrorist_score,
        duration="00:30" if match_id else "",
        warmup=warmup,
        updated_at="2026-07-22T11:00:00+00:00",
        players=players,
    )


def _official_detail(match_id: str, *, include_target: bool = True) -> dict:
    players = []
    for index in range(10):
        steam_id = (
            STEAM_ID
            if index == 0 and include_target
            else str(int(STEAM_ID) + index + (0 if include_target else 100))
        )
        players.append(
            {
                "playerId": steam_id,
                "nickName": "牛牛" if steam_id == STEAM_ID else f"玩家{index + 1}",
                "team": 1 if index < 5 else 2,
                "kill": 20 - index,
                "assist": 5,
                "death": 10 + index,
                "rating": 1.2,
                "adpr": 80,
            }
        )
    return {
        "base": {
            "matchId": match_id,
            "map": "炼狱小镇",
            "mode": "国服竞技",
            "score1": 13,
            "score2": 8,
            "winTeam": 1,
            "endTime": "2026-07-22 12:00:00",
        },
        "players": players,
    }


def _service(
    tmp_path: Path,
    *,
    configured: bool = True,
) -> tuple[
    CommunityRepository,
    BindingRepository,
    FakeApi,
    FakePresence,
    list[str],
    AutoPredictionService,
]:
    path = tmp_path / "bindings.db"
    bindings = BindingRepository(path)
    bindings.bind("group", "10001", STEAM_ID)
    community = CommunityRepository(path)
    api = FakeApi()
    presence = FakePresence()
    presence.configured = configured
    sent: list[str] = []

    async def send(_group_id: str, content: str) -> None:
        sent.append(content)

    service = AutoPredictionService(
        community,
        bindings,
        api,
        presence,  # type: ignore[arg-type]
        send,
        initial_delay_seconds=0,
        allowed_groups=["group"],
    )
    return community, bindings, api, presence, sent, service


@pytest.mark.asyncio
async def test_steam_server_signal_only_updates_state_without_opening_prediction(
    tmp_path: Path,
) -> None:
    community, _bindings, _api, presence, sent, service = _service(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)

    assert await service.poll_once(now=now) == (0, 0)
    presence.current = SteamPresence(STEAM_ID, "牛牛", "730", "1.2.3.4:27015")
    assert await service.poll_once(now=now + timedelta(seconds=30)) == (0, 0)

    state = community.steam_presence_state("group", STEAM_ID)
    assert state is not None
    assert (state.game_id, state.server_ip) == ("730", "1.2.3.4:27015")
    assert community.active_prediction("group") is None
    assert sent == []


@pytest.mark.asyncio
async def test_first_observation_does_not_open_mid_game_or_duplicate_same_server(
    tmp_path: Path,
) -> None:
    community, _bindings, _api, presence, _sent, service = _service(tmp_path)
    now = datetime(2026, 7, 22, 11, 0, tzinfo=UTC)
    presence.current = SteamPresence(STEAM_ID, "牛牛", "730", "1.2.3.4:27015")

    assert await service.poll_once(now=now) == (0, 0)
    assert await service.poll_once(now=now + timedelta(seconds=30)) == (0, 0)
    assert community.active_prediction("group") is None


@pytest.mark.asyncio
async def test_cs2_launch_without_server_ip_only_updates_state(
    tmp_path: Path,
) -> None:
    community, _bindings, _api, presence, sent, service = _service(tmp_path)
    now = datetime(2026, 7, 22, 11, 0, tzinfo=UTC)

    assert await service.poll_once(now=now) == (0, 0)
    presence.current = SteamPresence(STEAM_ID, "牛牛", "730", "")
    assert await service.poll_once(now=now + timedelta(seconds=30)) == (0, 0)

    state = community.steam_presence_state("group", STEAM_ID)
    assert state is not None and state.game_id == "730" and not state.server_ip
    assert community.active_prediction("group") is None
    assert sent == []


@pytest.mark.asyncio
async def test_first_observation_without_server_ip_only_builds_baseline(
    tmp_path: Path,
) -> None:
    community, _bindings, _api, presence, _sent, service = _service(tmp_path)
    now = datetime(2026, 7, 22, 11, 0, tzinfo=UTC)
    presence.current = SteamPresence(STEAM_ID, "牛牛", "730", "")

    assert await service.poll_once(now=now) == (0, 0)
    assert await service.poll_once(now=now + timedelta(seconds=30)) == (0, 0)
    assert community.active_prediction("group") is None


@pytest.mark.asyncio
async def test_steam_presence_does_not_query_match_baseline(tmp_path: Path) -> None:
    community, _bindings, api, presence, _sent, service = _service(tmp_path)
    now = datetime(2026, 7, 22, 11, 0, tzinfo=UTC)
    await service.poll_once(now=now)
    presence.current = SteamPresence(STEAM_ID, "牛牛", "730", "1.2.3.4:27015")
    api.fail = True

    assert await service.poll_once(now=now + timedelta(seconds=30)) == (0, 0)
    state = community.steam_presence_state("group", STEAM_ID)
    assert state is not None and state.server_ip == "1.2.3.4:27015"
    assert community.active_prediction("group") is None


@pytest.mark.asyncio
async def test_live_prediction_expires_and_refunds_bets(tmp_path: Path) -> None:
    community, _bindings, _api, _presence, sent, service = _service(
        tmp_path,
        configured=False,
    )
    watch_stage = FakeWatchStage()
    service.watch_stage = watch_stage  # type: ignore[assignment]
    now = datetime(2026, 7, 22, 11, 0, tzinfo=UTC)
    await service.poll_once(now=now)
    watch_stage.current = _watch_snapshot(
        match_id="PVP@1001",
        ct_score=1,
        terrorist_score=0,
    )
    await service.poll_once(now=now + timedelta(seconds=30))
    prediction = community.active_prediction("group")
    assert prediction is not None
    community.place_bet("group", "bettor", 1, 40)

    assert await service.poll_once(now=now + timedelta(hours=5)) == (0, 1)

    assert community.wallet("group", "bettor")[0] == 100
    run = community.auto_prediction_by_id(prediction.prediction_id)
    assert run is not None and run.status == "expired" and run.result_sent
    assert "4 小时" in sent[-1]


@pytest.mark.asyncio
async def test_legacy_daily_prediction_is_cancelled_even_without_steam_key(
    tmp_path: Path,
) -> None:
    community, _bindings, _api, _presence, sent, service = _service(
        tmp_path, configured=False
    )
    prediction, _run = community.create_auto_prediction(
        "group",
        "2026-07-22",
        "10001",
        STEAM_ID,
        "牛牛",
        "old",
        1,
        "result",
        None,
        "牛牛下一场能否获胜？",
        ("能", "不能"),
        duration_minutes=30,
        expires_at="2026-07-23T11:00:00+00:00",
    )
    community.place_bet("group", "bettor", 1, 40)

    assert await service.poll_once(now=datetime(2026, 7, 22, 11, tzinfo=UTC)) == (0, 0)
    run = community.auto_prediction_by_id(prediction.prediction_id)
    assert run is not None and run.status == "void" and run.result_sent
    assert community.wallet("group", "bettor")[0] == 100
    assert "策略已切换为开局竞猜" in sent[-1]


def test_unbind_clears_persisted_presence_state(tmp_path: Path) -> None:
    community, bindings, _api, _presence, _sent, _service_instance = _service(tmp_path)
    community.set_steam_presence_state(
        "group",
        STEAM_ID,
        "730",
        "1.2.3.4:27015",
        observed_at="2026-07-22T11:00:00+00:00",
    )
    community.set_watch_stage_state(
        "group",
        STEAM_ID,
        "live-match",
        observed_at="2026-07-22T11:00:00+00:00",
    )

    assert bindings.unbind("group", "10001") is True
    assert community.steam_presence_state("group", STEAM_ID) is None
    assert community.watch_stage_state("group", STEAM_ID) is None


@pytest.mark.asyncio
async def test_watch_stage_early_score_opens_prediction_and_settles_match(
    tmp_path: Path,
) -> None:
    community, _bindings, api, presence, sent, service = _service(
        tmp_path,
        configured=False,
    )
    watch_stage = FakeWatchStage()
    service.watch_stage = watch_stage  # type: ignore[assignment]
    now = datetime.now(UTC).replace(microsecond=0)

    assert await service.poll_once(now=now) == (0, 0)
    watch_stage.current = _watch_snapshot(
        match_id="9001",
        warmup=False,
        ct_score=2,
        terrorist_score=1,
    )
    assert await service.poll_once(now=now + timedelta(seconds=60)) == (1, 0)
    prediction = community.active_prediction("group")
    assert prediction is not None
    run = community.auto_prediction_by_id(prediction.prediction_id)
    assert run is not None
    assert run.prediction_date == f"live:watch:{STEAM_ID}:PVP@9001"
    assert "完美观将台检测到 牛牛 已进入新对局" in sent[-1]

    assert await service.poll_once(now=now + timedelta(seconds=120)) == (0, 0)
    assert community.active_prediction("group") == prediction

    api.matches = [
        _match("PVP@9999", now.strftime("%Y-%m-%d %H:%M:%S")),
        _match("old", "2026-07-21 20:00:00"),
    ]
    await service.observe_matches("group", STEAM_ID, api.matches)
    assert community.active_prediction("group") == prediction

    api.matches.insert(
        0,
        _match("9001", (now + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")),
    )
    await service.observe_matches("group", STEAM_ID, api.matches)
    assert community.active_prediction("group") is None
    settled = community.auto_prediction_by_id(prediction.prediction_id)
    assert settled is not None and settled.status == "settled"
    assert settled.result_match_id == "9001"


@pytest.mark.asyncio
async def test_watch_stage_rejects_non_pvp_ip_and_missing_target_signals(
    tmp_path: Path,
) -> None:
    community, _bindings, _api, _presence, sent, service = _service(
        tmp_path,
        configured=False,
    )
    watch_stage = FakeWatchStage()
    service.watch_stage = watch_stage  # type: ignore[assignment]
    now = datetime(2026, 7, 22, 11, 0, tzinfo=UTC)

    assert await service.poll_once(now=now) == (0, 0)

    rejected = (
        _watch_snapshot(
            match_id="PVP@7001",
            platform="1",
            ct_score=1,
            terrorist_score=0,
        ),
        _watch_snapshot(
            match_id="1.2.3.4:27015",
            platform="2",
            ct_score=1,
            terrorist_score=0,
        ),
        _watch_snapshot(
            match_id="PVP@7002",
            platform="2",
            ct_score=1,
            terrorist_score=0,
            include_target=False,
        ),
    )
    for index, snapshot in enumerate(rejected, 1):
        watch_stage.current = snapshot
        assert await service.poll_once(
            now=now + timedelta(seconds=index * 60)
        ) == (0, 0)
        assert community.active_prediction("group") is None

    assert sent == []
    state = community.watch_stage_state("group", STEAM_ID)
    assert state is not None and state.match_id == "PVP@7002"


@pytest.mark.asyncio
async def test_official_watch_stage_opens_and_only_source_one_roster_settles(
    tmp_path: Path,
) -> None:
    community, _bindings, api, _presence, sent, service = _service(
        tmp_path,
        configured=False,
    )
    watch_stage = FakeWatchStage()
    service.watch_stage = watch_stage  # type: ignore[assignment]
    now = datetime.now(UTC).replace(microsecond=0)

    assert await service.poll_once(now=now) == (0, 0)
    watch_stage.current = _watch_snapshot(
        match_id="183.60.122.75:27043",
        platform="1",
        ct_score=1,
        terrorist_score=0,
        player_count=10,
    )
    assert await service.poll_once(now=now + timedelta(seconds=30)) == (1, 0)

    prediction = community.active_prediction("group")
    assert prediction is not None
    run = community.auto_prediction_by_id(prediction.prediction_id)
    assert run is not None
    assert run.prediction_date.startswith(f"live:official:{STEAM_ID}:")
    assert "已进入官匹" in sent[-1]
    assert "出现官匹战绩后自动开奖" in sent[-1]

    new_match = _match(
        "LOCAL@new",
        (now + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
        data_source=1,
    )
    api.official_matches.insert(0, new_match)
    api.details[("LOCAL@new", 1)] = _official_detail("LOCAL@new")

    await service.observe_matches("group", STEAM_ID, [new_match], 3)
    assert community.active_prediction("group") == prediction

    await service.observe_matches("group", STEAM_ID, api.official_matches, 1)
    assert community.active_prediction("group") is None
    settled = community.auto_prediction_by_id(prediction.prediction_id)
    assert settled is not None
    assert settled.status == "settled"
    assert settled.result_match_id == "LOCAL@new"


@pytest.mark.asyncio
async def test_official_prediction_waits_for_complete_matching_roster(
    tmp_path: Path,
) -> None:
    community, _bindings, api, _presence, _sent, service = _service(
        tmp_path,
        configured=False,
    )
    watch_stage = FakeWatchStage()
    service.watch_stage = watch_stage  # type: ignore[assignment]
    now = datetime.now(UTC).replace(microsecond=0)

    await service.poll_once(now=now)
    watch_stage.current = _watch_snapshot(
        match_id="1.2.3.4:27015",
        platform="1",
        warmup=True,
        ct_score=0,
        terrorist_score=0,
        player_count=10,
    )
    await service.poll_once(now=now + timedelta(seconds=30))
    prediction = community.active_prediction("group")
    assert prediction is not None

    new_match = _match(
        "LOCAL@pending",
        (now + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
        data_source=1,
    )
    api.official_matches.insert(0, new_match)
    api.details[("LOCAL@pending", 1)] = {
        **_official_detail("LOCAL@pending"),
        "players": _official_detail("LOCAL@pending")["players"][:9],
    }
    await service.observe_matches("group", STEAM_ID, api.official_matches, 1)
    assert community.active_prediction("group") == prediction

    api.details[("LOCAL@pending", 1)] = _official_detail(
        "LOCAL@pending",
        include_target=False,
    )
    await service.observe_matches("group", STEAM_ID, api.official_matches, 1)
    assert community.active_prediction("group") == prediction

    api.details[("LOCAL@pending", 1)] = _official_detail("LOCAL@pending")
    await service.observe_matches("group", STEAM_ID, api.official_matches, 1)
    assert community.active_prediction("group") is None


@pytest.mark.asyncio
async def test_official_same_server_score_reset_starts_a_new_session(
    tmp_path: Path,
) -> None:
    community, _bindings, _api, _presence, _sent, service = _service(
        tmp_path,
        configured=False,
    )
    watch_stage = FakeWatchStage()
    service.watch_stage = watch_stage  # type: ignore[assignment]
    now = datetime(2026, 7, 22, 11, 0, tzinfo=UTC)

    await service.poll_once(now=now)
    watch_stage.current = _watch_snapshot(
        match_id="1.2.3.4:27015",
        platform="1",
        ct_score=1,
        terrorist_score=0,
        player_count=10,
    )
    assert await service.poll_once(now=now + timedelta(seconds=30)) == (1, 0)
    first = community.active_prediction("group")
    assert first is not None
    community.cancel_auto_prediction(
        first.prediction_id,
        status="void",
        result_value="测试结束第一局",
    )

    watch_stage.current = _watch_snapshot(
        match_id="1.2.3.4:27015",
        platform="1",
        ct_score=8,
        terrorist_score=5,
        player_count=10,
    )
    assert await service.poll_once(now=now + timedelta(seconds=60)) == (0, 0)

    watch_stage.current = _watch_snapshot(
        match_id="1.2.3.4:27015",
        platform="1",
        ct_score=0,
        terrorist_score=1,
        player_count=10,
    )
    assert await service.poll_once(now=now + timedelta(seconds=90)) == (1, 0)
    second = community.active_prediction("group")
    assert second is not None and second.prediction_id != first.prediction_id


@pytest.mark.asyncio
async def test_watch_stage_first_observation_and_midgame_do_not_open(
    tmp_path: Path,
) -> None:
    community, _bindings, _api, _presence, _sent, service = _service(
        tmp_path,
        configured=False,
    )
    watch_stage = FakeWatchStage()
    watch_stage.current = _watch_snapshot(
        match_id="PVP@8001",
        ct_score=5,
        terrorist_score=4,
    )
    service.watch_stage = watch_stage  # type: ignore[assignment]
    now = datetime(2026, 7, 22, 11, 0, tzinfo=UTC)

    assert await service.poll_once(now=now) == (0, 0)
    assert community.active_prediction("group") is None
    watch_stage.current = _watch_snapshot()
    assert await service.poll_once(now=now + timedelta(seconds=60)) == (0, 0)
    watch_stage.current = _watch_snapshot(
        match_id="8002",
        ct_score=3,
        terrorist_score=1,
    )
    assert await service.poll_once(now=now + timedelta(seconds=120)) == (0, 0)
    assert community.active_prediction("group") is None
    state = community.watch_stage_state("group", STEAM_ID)
    assert state is not None and state.match_id == "8002"
    watch_stage.current = _watch_snapshot()
    assert await service.poll_once(now=now + timedelta(seconds=180)) == (0, 0)
    watch_stage.current = _watch_snapshot(match_id="PVP@8003")
    assert await service.poll_once(now=now + timedelta(seconds=240)) == (0, 0)
    assert community.active_prediction("group") is None


@pytest.mark.asyncio
async def test_watch_stage_failure_does_not_use_steam_presence_to_open(
    tmp_path: Path,
) -> None:
    community, _bindings, _api, presence, sent, service = _service(tmp_path)
    watch_stage = FakeWatchStage()
    watch_stage.fail = True
    service.watch_stage = watch_stage  # type: ignore[assignment]
    now = datetime(2026, 7, 22, 11, 0, tzinfo=UTC)

    assert await service.poll_once(now=now) == (0, 0)
    presence.current = SteamPresence(STEAM_ID, "牛牛", "730", "1.2.3.4:27015")
    assert await service.poll_once(now=now + timedelta(seconds=60)) == (0, 0)
    assert community.active_prediction("group") is None
    assert sent == []
    state = community.steam_presence_state("group", STEAM_ID)
    assert state is not None and state.server_ip == "1.2.3.4:27015"
