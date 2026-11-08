import pytest

from pwcs2_bot.ranking import (
    build_official_ranking_entry,
    build_ranking_entry,
    build_ranking_snapshot,
    format_ranking_snapshot,
    parse_ranking_metric,
)


def _data(name: str, elo: int, rapid_stop: str | None) -> dict:
    return {
        "name": name,
        "steamId": "76561198929215155",
        "seasonId": "S24",
        "cnt": 21,
        "pvpScore": elo,
        "pwRating": 1.17,
        "winRate": 0.52,
        "adr": 82.3,
        "headShotRatio": 0.48,
        "perfectRankInfo": {"rank": 47},
        "radar": {
            "marksmanship": {
                "detail": {"rapid_stop_rate_raw": rapid_stop}
            }
        },
    }


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("", "elo"),
        ("ELO", "elo"),
        ("急停成功率", "rapid_stop"),
        ("Rating", "rating"),
        ("胜率", "win_rate"),
        ("ADR", "adr"),
        ("爆头", "headshot"),
        ("K/D", "kd"),
        ("场次", "matches"),
    ],
)
def test_parse_ranking_metric(argument: str, expected: str) -> None:
    assert parse_ranking_metric(argument) == expected


def test_ranking_normalizes_rapid_stop_and_sorts_missing_values_last() -> None:
    first = build_ranking_entry(
        _data("First", 1800, "0.830"),
        qq_id="100001",
        steam_id="76561198929215155",
    )
    second = build_ranking_entry(
        _data("Second", 2000, "0.710"),
        qq_id="100002",
        steam_id="76561198929215156",
    )
    missing = build_ranking_entry(
        _data("Missing", 2200, None),
        qq_id="100003",
        steam_id="76561198929215157",
    )

    ranking = build_ranking_snapshot(
        [second, missing, first],
        metric="rapid_stop",
        total_count=3,
    )

    assert first.rapid_stop == 83.0
    assert first.school_rank == 47
    assert [entry.name for entry in ranking.entries] == ["First", "Second", "Missing"]
    text = format_ranking_snapshot(ranking)
    assert text.startswith("CS2 群内完美急停成功率排行榜")
    assert "急停成功率 83.0%" in text
    assert "ELO 1800" in text


def test_official_ranking_defaults_to_rating_and_rejects_pvp_only_metrics() -> None:
    assert parse_ranking_metric("", source="official") == "rating"
    assert parse_ranking_metric("胜率", source="official") == "win_rate"
    assert parse_ranking_metric("K/D", source="official") == "kd"
    assert parse_ranking_metric("场次", source="official") == "matches"

    for unsupported in ("ELO", "急停", "ADR", "爆头"):
        with pytest.raises(ValueError, match="官匹排行仅支持"):
            parse_ranking_metric(unsupported, source="official")


def test_official_ranking_uses_recent_match_aggregates_without_placeholders() -> None:
    entry = build_official_ranking_entry(
        [
            {
                "matchId": "LOCAL@1",
                "team": 1,
                "winTeam": 1,
                "kill": 24,
                "death": 12,
                "pwRating": 0,
                "rating": 1.4,
                "pvpScore": 0,
                "we": 0,
            },
            {
                "matchId": "LOCAL@2",
                "team": 2,
                "winTeam": 1,
                "kill": 16,
                "death": 20,
                "pwRating": 0,
                "rating": 0.8,
                "pvpScore": 0,
                "we": 0,
            },
            {
                "matchId": "LOCAL@1",
                "team": 1,
                "winTeam": 1,
                "kill": 999,
                "death": 1,
                "rating": 9.9,
            },
        ],
        qq_id="100001",
        steam_id="76561198929215155",
        name="Official Player",
    )

    assert entry is not None
    assert entry.season == "最近 2 场官匹"
    assert entry.matches == 2
    assert entry.rating == pytest.approx(1.1)
    assert entry.win_rate == pytest.approx(50)
    assert entry.kd == pytest.approx(1.25)
    assert entry.elo is None
    assert entry.adr is None
    assert entry.rapid_stop is None
    assert entry.headshot is None
    assert entry.school_rank is None

    ranking = build_ranking_snapshot(
        [entry],
        metric="rating",
        total_count=1,
        source="official",
    )
    text = format_ranking_snapshot(ranking)
    assert text.startswith("CS2 群内官匹Rating排行榜")
    assert "Rating 1.1" in text
    assert "K/D 1.25" in text
    assert "ELO" not in text
    assert "急停" not in text


def test_official_ranking_name_comes_from_game_data() -> None:
    entry = build_official_ranking_entry(
        [{"matchId": "LOCAL@1", "steamName": "In Game ID"}],
        steam_id="76561198929215155", qq_id="100001", name="Old QQ Name",
    )
    assert entry is not None
    assert entry.name == "In Game ID"
