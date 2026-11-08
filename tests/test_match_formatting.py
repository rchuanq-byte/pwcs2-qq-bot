from pwcs2_bot.match_formatting import (
    build_match_history,
    build_match_snapshot,
    format_match_history,
)

STEAM_ID = "76561197960266101"


def test_match_and_history_prefer_actual_game_names_over_fallback_profile() -> None:
    raw = {"matchId": "PVP@identity", "steamName": "In Game ID"}
    profile = {"name": "Old QQ Name"}
    match = build_match_snapshot(raw, steam_id=STEAM_ID, profile=profile)
    history = build_match_history([raw], steam_id=STEAM_ID, profile=profile)
    assert match.name == history.name == history.matches[0].name == "In Game ID"
    detail = {"players": [
        {"playerId": STEAM_ID, "nickName": "In Match ID", "team": 1},
    ]}
    match = build_match_snapshot(raw, steam_id=STEAM_ID, profile=profile, detail=detail)
    assert match.name == match.players[0].name == "In Match ID"


def _official_match() -> dict:
    return {
        "matchId": "LOCAL@official-test",
        "dataSource": 1,
        "timeStamp": 200,
        "team": 1,
        "winTeam": 2,
        "score1": 11,
        "score2": 13,
        "kill": 26,
        "assist": 3,
        "death": 12,
        "pwRating": 0.0,
        "rating": 1.6,
        "we": 0.0,
        "pvpScore": 0,
        "pvpScoreChange": 0,
    }


def test_official_match_uses_rating_and_hides_perfect_only_placeholders() -> None:
    raw = _official_match()
    snapshot = build_match_snapshot(
        raw,
        steam_id=STEAM_ID,
        detail={
            "base": {
                "matchId": raw["matchId"],
                "score1": 11,
                "score2": 13,
                "winTeam": 2,
            },
            "players": [
                {
                    "playerId": STEAM_ID,
                    "nickName": "Official Player",
                    "team": 1,
                    "kill": 26,
                    "assist": 3,
                    "death": 12,
                    "pwRating": 0.0,
                    "rating": 1.6,
                    "adpr": 97,
                    "we": 0.0,
                    "pvpScore": 0,
                    "pvpScoreChange": 0,
                }
            ],
        },
    )

    assert snapshot.mode == "CS2 官匹"
    assert snapshot.rating == "1.6"
    assert snapshot.we == "-"
    assert snapshot.pvp_score == "-"
    assert snapshot.score_change == "-"
    assert snapshot.players[0].rating == "1.6"
    assert snapshot.players[0].adr == "97"
    assert snapshot.players[0].we == "-"
    assert snapshot.players[0].pvp_score == "-"
    assert snapshot.players[0].score_change == "-"


def test_mixed_history_keeps_pvp_metrics_out_of_official_matches() -> None:
    pvp = {
        "matchId": "PVP@perfect-test",
        "timeStamp": 100,
        "team": 1,
        "winTeam": 1,
        "score1": 13,
        "score2": 8,
        "kill": 20,
        "assist": 5,
        "death": 10,
        "pwRating": 1.2,
        "rating": 9.9,
        "we": 10.5,
        "pvpScore": 1900,
        "pvpScoreChange": 12,
    }

    history = build_match_history(
        [_official_match(), pvp],
        steam_id=STEAM_ID,
    )

    assert history.source == "mixed"
    assert history.avg_rating == "1.4"
    assert history.elo_change == "+12"
    assert history.matches[0].rating == "1.6"
    assert history.matches[0].score_change == "-"
    assert history.matches[1].rating == "1.2"
    assert history.matches[1].we == "10.5"
    assert history.matches[1].pvp_score == "1900"
    assert history.matches[1].score_change == "+12"


def test_official_only_history_does_not_report_placeholder_elo_zero() -> None:
    history = build_match_history([_official_match()], steam_id=STEAM_ID)

    assert history.source == "official"
    assert history.avg_rating == "1.6"
    assert history.elo_change == "-"
    text = format_match_history(history)
    assert text.startswith(f"CS2 官匹近十场战绩｜{STEAM_ID}")
    assert "ELO" not in text
    assert "Rating 1.6" in text


def test_pvp_only_history_keeps_perfect_source_and_metrics() -> None:
    history = build_match_history(
        [
            {
                "matchId": "PVP@perfect",
                "timeStamp": 300,
                "team": 1,
                "winTeam": 1,
                "score1": 13,
                "score2": 8,
                "kill": 20,
                "assist": 5,
                "death": 10,
                "pwRating": 1.2,
                "we": 10,
                "pvpScoreChange": 12,
            }
        ],
        steam_id=STEAM_ID,
        profile={"name": "Perfect Player"},
    )

    assert history.source == "pvp"
    assert history.elo_change == "+12"
    text = format_match_history(history)
    assert text.startswith("CS2 完美近十场战绩｜Perfect Player")
    assert "ELO +12" in text
