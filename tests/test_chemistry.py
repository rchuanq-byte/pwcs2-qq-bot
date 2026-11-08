from pwcs2_bot.chemistry import (
    build_chemistry_network,
    build_chemistry_snapshot,
    format_chemistry_network,
    format_chemistry_snapshot,
)


def _match(match_id: str, *, team: int, win_team: int, rating: float) -> dict:
    return {
        "matchId": match_id,
        "team": team,
        "winTeam": win_team,
        "pwRating": rating,
    }


def test_chemistry_counts_teammate_and_opponent_matches() -> None:
    first = [
        _match("a", team=1, win_team=1, rating=1.2),
        _match("b", team=2, win_team=1, rating=1.0),
        _match("c", team=1, win_team=1, rating=1.1),
    ]
    second = [
        _match("a", team=1, win_team=1, rating=1.1),
        _match("b", team=1, win_team=1, rating=1.2),
        _match("x", team=1, win_team=1, rating=1.3),
    ]

    result = build_chemistry_snapshot(
        first,
        second,
        first_qq="10001",
        second_qq="10002",
        first_name="牛牛",
        second_name="精灵",
    )

    assert result.shared_matches == 2
    assert (result.same_team_matches, result.same_team_wins) == (1, 1)
    assert result.same_team_win_rate == "100.0%"
    assert result.versus_matches == 1
    assert (result.first_versus_wins, result.second_versus_wins) == (0, 1)
    assert "牛牛 × 精灵" in format_chemistry_snapshot(result)


def test_chemistry_handles_no_shared_matches() -> None:
    result = build_chemistry_snapshot(
        [_match("a", team=1, win_team=1, rating=1.0)],
        [_match("b", team=1, win_team=1, rating=1.0)],
        first_qq="10001",
        second_qq="10002",
        first_name="A",
        second_name="B",
    )

    assert result.title == "平行宇宙"
    assert "没有共同比赛" in format_chemistry_snapshot(result)


def test_chemistry_uses_official_rating_when_pw_rating_is_zero() -> None:
    first = _match("official", team=1, win_team=1, rating=0)
    first["rating"] = 1.32
    second = _match("official", team=2, win_team=1, rating=0)
    second["rating"] = 0.94

    result = build_chemistry_snapshot(
        [first],
        [second],
        first_qq="10001",
        second_qq="10002",
        first_name="A",
        second_name="B",
        source="official",
    )

    assert result.source == "official"
    assert result.first_avg_rating == "1.32"
    assert result.second_avg_rating == "0.94"
    text = format_chemistry_snapshot(result)
    assert text.startswith("🤝 官匹群友羁绊｜A × B")
    assert "数据范围：双方最近 50 场官匹比赛" in text


def test_chemistry_default_remains_perfect_only() -> None:
    shared = _match("shared", team=1, win_team=1, rating=1.1)
    result = build_chemistry_snapshot(
        [shared],
        [shared],
        first_qq="10001",
        second_qq="10002",
        first_name="A",
        second_name="B",
    )

    assert result.source == "pvp"
    text = format_chemistry_snapshot(result)
    assert text.startswith("🤝 完美平台群友羁绊｜A × B")
    assert "数据范围：双方最近 50 场完美平台比赛" in text


def test_group_network_selects_strongest_recent_pairs() -> None:
    network = build_chemistry_network(
        {
            "10001": [
                _match("a", team=1, win_team=1, rating=1.2),
                _match("b", team=1, win_team=1, rating=1.1),
            ],
            "10002": [
                _match("a", team=1, win_team=1, rating=1.0),
                _match("b", team=1, win_team=1, rating=1.1),
            ],
            "10003": [_match("a", team=2, win_team=1, rating=1.0)],
        },
        names_by_qq={"10001": "A", "10002": "B", "10003": "C"},
        week_start="2026-07-20",
    )

    assert network.member_count == 3
    assert network.pairs[0].same_team_matches == 2
    assert (network.pairs[0].first_name, network.pairs[0].second_name) == ("A", "B")
    assert "A × B" in format_chemistry_network(network)
