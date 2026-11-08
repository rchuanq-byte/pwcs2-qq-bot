from pwcs2_bot.formatting import (
    build_official_stats_snapshot,
    build_stats_snapshot,
    format_stats,
    format_stats_snapshot,
)


def test_format_stats_handles_partial_payload() -> None:
    result = format_stats(
        {
            "name": "Alice",
            "steamId": "76561198929215155",
            "kills": 12,
            "deaths": 6,
            "winRate": 62,
            "hotMaps": [
                {"mapName": "荒漠迷城", "totalMatch": 10, "winCount": 7},
            ],
            "hotWeapons": [{"weaponName": "AK47", "weaponKill": 99}],
        }
    )

    assert "CS2 完美数据｜Alice" in result
    assert "K/D：2" in result
    assert "胜率：62.0%" in result
    assert "荒漠迷城 10场/70.0%" in result
    assert "AK47 99杀" in result


def test_build_stats_snapshot_keeps_avatar_url() -> None:
    snapshot = build_stats_snapshot(
        {
            "name": "Player",
            "avatar": "https://img.wmpvp.com/pvp/avatar.webp",
        }
    )

    assert snapshot.avatar_url == "https://img.wmpvp.com/pvp/avatar.webp"


def test_official_stats_aggregate_only_reliable_recent_match_fields() -> None:
    snapshot = build_official_stats_snapshot(
        [
            {
                "matchId": "LOCAL@new",
                "dataSource": 1,
                "timeStamp": 200,
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
                "pvpScore": 0,
                "we": 0,
            },
            {
                "matchId": "LOCAL@old",
                "dataSource": 1,
                "timeStamp": 100,
                "mapName": "炼狱小镇",
                "team": 2,
                "winTeam": 1,
                "score1": 13,
                "score2": 9,
                "kill": 16,
                "assist": 3,
                "death": 20,
                "pwRating": 0,
                "rating": 0.8,
                "pvpScore": 0,
                "we": 0,
            },
            {
                "matchId": "LOCAL@new",
                "dataSource": 1,
                "timeStamp": 50,
                "kill": 999,
                "death": 1,
                "rating": 9.9,
            },
        ],
        steam_id="76561198929215155",
        profile={
            "name": "Official Player",
            "avatar": "https://img.wmpvp.com/avatar.png",
        },
    )

    assert snapshot.source == "official"
    assert snapshot.name == "Official Player"
    assert snapshot.season == "最近 2 场官匹"
    assert snapshot.score == "1-1-0"
    assert snapshot.matches == "2"
    assert snapshot.win_rate == "50.0%"
    assert snapshot.rating == "1.1"
    assert snapshot.kd == "1.25"
    assert (snapshot.kills, snapshot.assists, snapshot.deaths) == ("40", "8", "32")
    assert snapshot.maps[0].name == "炼狱小镇"
    assert snapshot.maps[0].matches == "2"
    assert snapshot.adr == "-"
    assert snapshot.headshot_rate == "-"
    assert snapshot.rws == "-"
    assert snapshot.mvp == "-"
    assert snapshot.weapons == ()

    result = format_stats_snapshot(snapshot)
    assert result.startswith("CS2 官匹数据｜Official Player")
    assert "范围：最近 2 场官匹" in result
    assert "平均 Rating：1.1" in result
    assert "ADR" in snapshot.summary
    assert "完美分" not in result
    assert "赛季：" not in result


def test_official_stats_do_not_invent_kd_when_deaths_are_zero() -> None:
    snapshot = build_official_stats_snapshot(
        [
            {
                "matchId": "LOCAL@zero-deaths",
                "dataSource": 1,
                "team": 1,
                "winTeam": 1,
                "kill": 20,
                "death": 0,
                "rating": 1.9,
            }
        ],
        steam_id="76561198929215155",
    )

    assert snapshot.kd == "-"


def test_stats_prefers_game_name_and_missing_name_falls_back_to_steam_id() -> None:
    steam_id = "76561198929215155"
    snapshot = build_official_stats_snapshot(
        [{"matchId": "LOCAL@1", "steamName": "Game ID"}],
        steam_id=steam_id,
        profile={"name": "Old QQ Name"},
    )
    assert snapshot.name == "Game ID"
    assert build_stats_snapshot({"steamId": steam_id}).name == steam_id
    assert build_stats_snapshot({"steamNick": "Game ID", "name": "Platform ID"}).name == "Game ID"
