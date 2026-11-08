import time
from pathlib import Path

import pytest

from pwcs2_bot.database import BindingRepository
from pwcs2_bot.match_formatting import (
    build_match_history,
    build_match_snapshot,
    format_match_history,
    format_match_snapshot,
)
from pwcs2_bot.match_notifier import MatchNotifier

_MATCH_TIME_BASE = int(time.time()) + 60


@pytest.fixture(autouse=True)
def refresh_match_clock() -> None:
    # Other modules can run for minutes after collection. Refresh per test so
    # +2 remains new and -3600 remains historical relative to its own baseline.
    global _MATCH_TIME_BASE
    _MATCH_TIME_BASE = int(time.time()) + 60


def _match(match_id: str, timestamp: int, *, team: int = 2, win_team: int = 2) -> dict:
    return {
        "matchId": match_id,
        "playerId": "76561198929215155",
        "mapName": "荒漠迷城",
        "mode": "天梯组排对局",
        "team": team,
        "winTeam": win_team,
        "score1": 8,
        "score2": 13,
        "kill": 15,
        "assist": 7,
        "death": 13,
        "pwRating": 1.14,
        "we": 8.84,
        "pvpScore": 1459,
        "pvpScoreChange": 28,
        "duration": 30,
        "endTime": "2026-06-30 21:30:00",
        "timeStamp": _MATCH_TIME_BASE + timestamp,
    }


def _detail(match_id: str) -> dict:
    steam_ids = [
        "76561198929215155",
        "76561198000000002",
        *[f"765611980000001{index:02d}" for index in range(8)],
    ]
    return {
        "base": {
            "matchId": match_id,
            "map": "荒漠迷城",
            "mode": "天梯组排对局",
            "score1": 8,
            "score2": 13,
            "winTeam": 2,
            "endTime": "2026-06-30 21:30:00",
        },
        "players": [
            {
                "playerId": player_id,
                "nickName": f"Player {index + 1}",
                "team": 1 if index < 5 else 2,
                "kill": 20 - index,
                "assist": 4,
                "death": 15,
                "pwRating": 1.2,
                "adpr": 80,
                "we": 10,
                "pvpScore": 1800,
            }
            for index, player_id in enumerate(steam_ids)
        ],
    }


class FakeApi:
    def __init__(self) -> None:
        self.matches = [_match("old", 1)]
        self.official_matches: list[dict] = []
        self.queries: list[tuple[str, int]] = []
        self.official_queries: list[tuple[str, int]] = []
        self.detail_queries: list[tuple[str, int]] = []

    async def match_list(self, steam_id: str, *, page_size: int) -> list[dict]:
        self.queries.append((steam_id, page_size))
        return self.matches

    async def official_match_list(
        self, steam_id: str, *, page_size: int
    ) -> list[dict]:
        self.official_queries.append((steam_id, page_size))
        return self.official_matches

    async def detail_stats(self, steam_id: str) -> dict:
        return {"name": "𝓗𝓾𝓷𝓰𝓮𝓻", "steamId": steam_id, "avatar": ""}

    async def match_detail(self, match_id: str, *, data_source: int) -> dict:
        self.detail_queries.append((match_id, data_source))
        return _detail(match_id)


def test_match_snapshot_uses_player_team_score_and_formats_text() -> None:
    snapshot = build_match_snapshot(
        _match("PVP@test", 2),
        steam_id="76561198929215155",
        profile={"name": "Player"},
    )

    assert snapshot.result == "win"
    assert snapshot.result_label == "胜利"
    assert (snapshot.own_score, snapshot.opponent_score) == ("13", "8")
    assert snapshot.score_change == "+28"
    assert snapshot.kd == "1.15"
    assert "CS2 新比赛｜Player｜胜利" in format_match_snapshot(snapshot)


def test_match_history_sorts_limits_and_summarizes() -> None:
    matches = [
        _match(str(index), index, team=1, win_team=1 if index % 2 else 2)
        for index in range(1, 13)
    ]
    matches.append(_match("12", 12))

    history = build_match_history(
        matches,
        steam_id="76561198929215155",
        profile={"name": "Recent Player", "avatar": "https://img.wmpvp.com/avatar.png"},
    )

    assert history.total == 10
    assert [match.match_id for match in history.matches[:2]] == ["12", "11"]
    assert (history.wins, history.losses) == (5, 5)
    assert history.avg_rating == "1.14"
    assert history.avatar_url.endswith("avatar.png")
    assert format_match_history(history).startswith("CS2 完美近十场战绩｜Recent Player")


async def test_notifier_baselines_then_sends_new_match_once_to_each_group(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    steam_id = "76561198929215155"
    repository.bind("group-1", "qq-1", steam_id)
    repository.bind("group-2", "qq-2", steam_id)
    api = FakeApi()
    sent: list[tuple[str, str]] = []

    async def send(group_id: str, match) -> None:
        sent.append((group_id, match.match_id))

    notifier = MatchNotifier(
        repository,
        api,
        send,
        initial_delay_seconds=0,
    )

    await notifier.poll_once()
    assert sent == []

    api.matches = [_match("new", 2), _match("old", 1)]
    await notifier.poll_once()
    await notifier.poll_once()

    assert sorted(sent) == [("group-1", "new"), ("group-2", "new")]
    assert api.queries[0] == (steam_id, 5)
    assert api.detail_queries == [("new", 3)]


async def test_notifier_retries_when_sending_fails(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    steam_id = "76561198929215155"
    repository.bind("group", "qq", steam_id)
    api = FakeApi()
    attempts = 0

    async def send(_group_id: str, _match) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary OneBot failure")

    notifier = MatchNotifier(repository, api, send, initial_delay_seconds=0)
    await notifier.poll_once()
    api.matches = [_match("new", 2), _match("old", 1)]

    await notifier.poll_once()
    await notifier.poll_once()

    assert attempts == 2
    assert repository.is_match_notified("group", steam_id, "new")


async def test_notifier_waits_for_complete_detail_before_marking_match(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    steam_id = "76561198929215155"
    repository.bind("group", "qq", steam_id)
    api = FakeApi()
    sent: list[str] = []
    detail_ready = False

    async def delayed_detail(match_id: str, *, data_source: int) -> dict:
        api.detail_queries.append((match_id, data_source))
        if not detail_ready:
            return {"base": {"matchId": match_id}, "players": []}
        return _detail(match_id)

    api.match_detail = delayed_detail  # type: ignore[method-assign]

    async def send(_group_id: str, match) -> None:
        sent.append(match.match_id)

    notifier = MatchNotifier(repository, api, send, initial_delay_seconds=0)
    await notifier.poll_once()
    api.matches = [_match("new", 2), _match("old", 1)]

    await notifier.poll_once()

    assert sent == []
    assert not repository.is_match_notified("group", steam_id, "new")

    detail_ready = True
    await notifier.poll_once()

    assert sent == ["new"]
    assert repository.is_match_notified("group", steam_id, "new")
    assert api.detail_queries == [("new", 3), ("new", 3)]


async def test_failed_group_only_attempts_once_per_poll_and_recovers_without_loss(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    first_steam = "76561198929215155"
    second_steam = "76561198000000002"
    repository.bind("failed-group", "qq-1", first_steam)
    repository.bind("failed-group", "qq-2", second_steam)
    repository.bind("healthy-group", "qq-3", first_steam)
    api = FakeApi()
    api.official_matches = [_match("LOCAL@old", 1)]
    attempted: list[tuple[str, str]] = []
    observed: list[tuple[str, str, int]] = []
    failing = True

    async def send(group_id: str, match) -> None:
        attempted.append((group_id, match.match_id))
        if group_id == "failed-group" and failing:
            raise RuntimeError("temporary QQ send failure")

    async def observe(group_id: str, steam_id: str, _matches, source: int) -> None:
        observed.append((group_id, steam_id, source))

    notifier = MatchNotifier(repository, api, send, observe_matches=observe)
    await notifier.poll_once()
    observed.clear()
    api.matches = [_match("new", 2), _match("old", 1)]
    api.official_matches = [_match("LOCAL@new", 2), _match("LOCAL@old", 1)]

    await notifier.poll_once()

    # Both players and data sources are still observed, but an offline group
    # incurs only one send attempt; its healthy neighbor is not held back.
    assert sum(group == "failed-group" for group, _match_id in attempted) == 1
    assert sorted(
        match_id for group, match_id in attempted if group == "healthy-group"
    ) == ["LOCAL@new", "new"]
    assert len(observed) == 6
    for source, match_id in ((3, "new"), (1, "LOCAL@new")):
        assert not repository.is_group_source_match_notified(
            "failed-group", source, match_id
        )
        assert repository.is_group_source_match_notified(
            "healthy-group", source, match_id
        )

    failing = False
    attempted.clear()
    await notifier.poll_once()

    assert sorted(attempted) == [
        ("failed-group", "LOCAL@new"),
        ("failed-group", "new"),
    ]
    for source, match_id in ((3, "new"), (1, "LOCAL@new")):
        assert repository.is_group_source_match_notified(
            "failed-group", source, match_id
        )
    attempted.clear()
    await notifier.poll_once()
    assert attempted == []


async def test_notifier_drains_backlog_without_marking_unsent_matches(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    steam_id = "76561198929215155"
    repository.bind("group", "qq", steam_id)
    api = FakeApi()
    sent: list[str] = []

    async def send(_group_id: str, match) -> None:
        sent.append(match.match_id)

    notifier = MatchNotifier(
        repository,
        api,
        send,
        initial_delay_seconds=0,
        max_notifications_per_poll=2,
    )
    await notifier.poll_once()
    api.matches = [
        _match(f"new-{index}", index + 1)
        for index in reversed(range(1, 6))
    ] + [_match("old", 1)]

    await notifier.poll_once()
    assert sent == ["new-1", "new-2"]
    assert not repository.is_match_notified("group", steam_id, "new-3")

    await notifier.poll_once()
    await notifier.poll_once()

    assert sent == ["new-1", "new-2", "new-3", "new-4", "new-5"]
    assert all(
        repository.is_match_notified("group", steam_id, f"new-{index}")
        for index in range(1, 6)
    )


async def test_notifier_does_not_replay_history_when_page_size_expands(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    steam_id = "76561198929215155"
    repository.bind("group", "qq", steam_id)
    api = FakeApi()
    sent: list[str] = []

    async def send(_group_id: str, match) -> None:
        sent.append(match.match_id)

    notifier = MatchNotifier(repository, api, send, initial_delay_seconds=0)
    await notifier.poll_once()
    api.matches = [
        _match("new", 2),
        _match("old", 1),
        _match("historical-outside-old-page", -120),
    ]

    await notifier.poll_once()

    assert sent == ["new"]
    assert not repository.is_match_notified(
        "group",
        steam_id,
        "historical-outside-old-page",
    )


async def test_notifier_exposes_each_players_match_list_to_observer(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    steam_id = "76561198929215155"
    repository.bind("group", "qq", steam_id)
    api = FakeApi()
    observed: list[tuple[str, str, list[str], int]] = []

    async def send(_group_id: str, _match) -> None:
        return None

    async def observe(
        group_id: str,
        observed_steam: str,
        matches: list[dict],
        data_source: int,
    ) -> None:
        observed.append(
            (
                group_id,
                observed_steam,
                [str(match["matchId"]) for match in matches],
                data_source,
            )
        )

    notifier = MatchNotifier(
        repository,
        api,
        send,
        initial_delay_seconds=0,
        observe_matches=observe,
    )
    await notifier.poll_once()

    assert observed == [
        ("group", steam_id, ["old"], 3),
        ("group", steam_id, [], 1),
    ]


async def test_notifier_baselines_official_matches_independently_then_sends_new_game(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    steam_id = "76561198929215155"
    repository.bind("group", "qq", steam_id)
    repository.initialize_match_tracking("group", steam_id, ["old"])
    api = FakeApi()
    api.official_matches = [
        {**_match("LOCAL@old", 1), "dataSource": 1},
    ]
    sent: list[tuple[str, str]] = []

    async def send(group_id: str, match) -> None:
        sent.append((group_id, match.match_id))

    notifier = MatchNotifier(repository, api, send, initial_delay_seconds=0)

    await notifier.poll_once()
    assert sent == []
    assert repository.is_group_source_match_notified(
        "group",
        1,
        "LOCAL@old",
    )
    assert not repository.is_group_match_notified("group", "LOCAL@old")

    api.official_matches = [
        {**_match("LOCAL@new", 2), "dataSource": 1},
        {**_match("LOCAL@old", 1), "dataSource": 1},
    ]
    await notifier.poll_once()
    await notifier.poll_once()

    assert sent == [("group", "LOCAL@new")]
    assert repository.is_group_source_match_notified(
        "group",
        1,
        "LOCAL@new",
    )
    assert api.detail_queries == [("LOCAL@new", 1)]


async def test_notifier_sends_one_scoreboard_for_two_group_members_in_same_match(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.bind("group", "qq-1", "76561198929215155")
    repository.bind("group", "qq-2", "76561198000000002")
    api = FakeApi()
    sent = []

    async def send(group_id: str, match) -> None:
        sent.append((group_id, match))

    notifier = MatchNotifier(repository, api, send, initial_delay_seconds=0)
    await notifier.poll_once()
    api.matches = [_match("new", 2), _match("old", 1)]

    await notifier.poll_once()

    assert len(sent) == 1
    group_id, snapshot = sent[0]
    assert group_id == "group"
    assert len(snapshot.players) == 10
    assert sum(player.is_group_member for player in snapshot.players) == 2
    assert repository.is_group_match_notified("group", "new")
