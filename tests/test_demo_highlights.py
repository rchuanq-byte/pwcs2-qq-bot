import json
from dataclasses import replace
from pathlib import Path

from pwcs2_bot.demo_highlights import (
    DemoBlindEvent,
    DemoHighlightAnalyzer,
    DemoHighlightExtractor,
    DemoHurtEvent,
    DemoKillEvent,
    DemoMatchTimeline,
    Demoparser2Adapter,
    DemoRound,
    DemoWeaponFireEvent,
)


def _kill(
    second: float,
    victim: str,
    *,
    attacker: str = "76561198000000001",
    round_number: int = 7,
    weapon: str = "ak47",
    headshot: bool = False,
    own_alive: int | None = None,
    opponents_alive: int | None = None,
) -> DemoKillEvent:
    return DemoKillEvent(
        tick=round(second * 64),
        time_seconds=second,
        round_number=round_number,
        attacker_steam_id=attacker,
        victim_steam_id=victim,
        weapon=weapon,
        headshot=headshot,
        attacker_team="CT",
        victim_team="T",
        attacker_team_alive=own_alive,
        victim_team_alive=opponents_alive,
    )


def test_combined_ace_headshots_knife_and_clutch_has_stable_manifest() -> None:
    timeline = DemoMatchTimeline(
        match_id="perfect-123",
        map_name="de_mirage",
        tick_rate=64,
        kills=(
            _kill(100, "t1", headshot=True, own_alive=1, opponents_alive=5),
            _kill(104, "t2", headshot=True, own_alive=1, opponents_alive=4),
            _kill(108, "t3", headshot=True, own_alive=1, opponents_alive=3),
            _kill(112, "t4", weapon="weapon_knife", own_alive=1, opponents_alive=2),
            _kill(116, "t5", own_alive=1, opponents_alive=1),
        ),
        rounds=(DemoRound(7, 90, 130, "CT"),),
        duration_seconds=200,
    )

    first = DemoHighlightAnalyzer().analyze(timeline)[0]
    second = DemoHighlightAnalyzer().analyze(timeline)[0]

    assert first.tags == ("ace", "headshot_streak", "knife_kill", "clutch_clue")
    assert first.score == 100
    assert first.kill_count == 5
    assert first.headshot_streak == 3
    assert first.clutch_opponents == 5
    assert first.clutch_confirmed is True
    assert first.start_seconds == 92
    assert first.end_seconds == 126
    assert first.start_tick == 5888
    assert first.end_tick == 8064
    assert first.highlight_id == second.highlight_id
    assert first.to_json() == second.to_json()
    payload = json.loads(first.to_json())
    assert payload["schema_version"] == 1
    assert payload["window"] == {
        "end_seconds": 126.0,
        "end_tick": 8064,
        "start_seconds": 92.0,
        "start_tick": 5888,
    }
    assert payload["events"][3]["weapon"] == "knife"


def test_player_filter_and_score_order_are_deterministic() -> None:
    timeline = DemoMatchTimeline(
        match_id="match",
        map_name="de_nuke",
        tick_rate=64,
        kills=(
            _kill(10, "a", attacker="p1", round_number=1, weapon="knife"),
            _kill(12, "a2", attacker="p1", round_number=1),
            _kill(20, "b", attacker="p2", round_number=2),
            _kill(22, "c", attacker="p2", round_number=2),
            _kill(24, "d", attacker="p2", round_number=2),
        ),
    )

    all_windows = DemoHighlightAnalyzer().analyze(timeline)
    filtered = DemoHighlightAnalyzer().analyze(timeline, tracked_steam_ids=("p1",))

    assert [window.player_steam_id for window in all_windows] == ["p2", "p1"]
    assert all_windows[0].tags == ("3k",)
    assert filtered[0].tags == ("knife_kill",)


def test_single_knife_or_single_clutch_kill_is_not_a_highlight() -> None:
    single_knife = DemoMatchTimeline(
        match_id="single-knife",
        map_name="de_mirage",
        tick_rate=64,
        kills=(_kill(10, "enemy", attacker="player", weapon="knife"),),
    )
    single_clutch = DemoMatchTimeline(
        match_id="single-clutch",
        map_name="de_inferno",
        tick_rate=64,
        kills=(
            _kill(
                20,
                "enemy",
                attacker="player",
                own_alive=1,
                opponents_alive=3,
            ),
        ),
        rounds=(DemoRound(7, 10, 30, "CT"),),
    )

    assert DemoHighlightAnalyzer().analyze(single_knife, tracked_steam_ids=("player",)) == ()
    assert DemoHighlightAnalyzer().analyze(single_clutch, tracked_steam_ids=("player",)) == ()


def test_headshot_chain_resets_on_non_headshot_and_large_gap() -> None:
    no_highlight = DemoMatchTimeline(
        match_id="m1",
        map_name="de_dust2",
        tick_rate=64,
        kills=(
            _kill(1, "a", headshot=True),
            _kill(20, "b", headshot=True),
            _kill(22, "c", headshot=False),
        ),
    )
    window_without_chain = DemoHighlightAnalyzer().analyze(no_highlight)[0]
    assert window_without_chain.tags == ("3k",)
    assert window_without_chain.headshot_streak == 1

    two_taps = DemoMatchTimeline(
        match_id="m2",
        map_name="de_dust2",
        tick_rate=64,
        kills=(_kill(1, "a", headshot=True), _kill(5, "b", headshot=True)),
    )
    window = DemoHighlightAnalyzer().analyze(two_taps)[0]
    assert window.tags == ("headshot_streak",)
    assert window.score == 34


def test_clutch_keeps_final_kill_and_ignores_impossible_demo_bounds() -> None:
    timeline = DemoMatchTimeline(
        match_id="broken-perfectworld-bounds",
        map_name="de_vertigo",
        tick_rate=64,
        kills=(
            _kill(100, "t1", own_alive=1, opponents_alive=2),
            _kill(104, "t2", own_alive=1, opponents_alive=1),
        ),
        rounds=(DemoRound(7, start_seconds=110, end_seconds=95, winner_team="CT"),),
        duration_seconds=92,
    )

    window = DemoHighlightAnalyzer().analyze(timeline)[0]

    assert window.tags == ("clutch_clue",)
    assert [event.time_seconds for event in window.events] == [100, 104]
    assert window.start_seconds == 92
    assert window.end_seconds == 114
    assert window.start_tick == 5888
    assert window.end_tick == 7296


def test_close_range_full_mag_whiff_is_a_single_lowlight() -> None:
    player = "76561198000000123"
    death = _kill(
        100,
        player,
        attacker="76561198000000456",
        round_number=4,
    )
    death = replace(death, distance_meters=4.5)
    timeline = DemoMatchTimeline(
        match_id="whiff",
        map_name="de_mirage",
        tick_rate=64,
        kills=(death,),
        weapon_fires=tuple(
            DemoWeaponFireEvent(
                tick=round((96 + index * 0.25) * 64),
                time_seconds=96 + index * 0.25,
                round_number=4,
                player_steam_id=player,
                weapon="ak47",
            )
            for index in range(12)
        ),
        rounds=(DemoRound(4, 90, 110, "CT"),),
        duration_seconds=130,
    )

    windows = DemoHighlightAnalyzer().analyze(
        timeline,
        tracked_steam_ids=(player,),
    )

    assert len(windows) == 1
    assert windows[0].tags == ("lowlight_close_whiff",)
    assert windows[0].player_steam_id == player
    assert windows[0].kill_count == 0
    assert windows[0].start_seconds == 92
    assert windows[0].end_seconds == 110


def test_combined_self_flash_and_failed_knife_scores_one_lowlight() -> None:
    player = "76561198000000123"
    enemy = "76561198000000456"
    death = DemoKillEvent(
        tick=6400,
        time_seconds=100,
        round_number=4,
        attacker_steam_id=enemy,
        victim_steam_id=player,
        weapon="ak47",
        headshot=False,
        attacker_team="CT",
        victim_team="T",
        distance_meters=3.0,
    )
    timeline = DemoMatchTimeline(
        match_id="knife-flash",
        map_name="de_inferno",
        tick_rate=64,
        kills=(death,),
        weapon_fires=tuple(
            DemoWeaponFireEvent(
                tick=round(second * 64),
                time_seconds=second,
                round_number=4,
                player_steam_id=player,
                weapon="knife",
            )
            for second in (97.0, 98.0, 99.0)
        ),
        hurts=(
            DemoHurtEvent(
                tick=6200,
                time_seconds=96.875,
                round_number=4,
                attacker_steam_id=player,
                victim_steam_id=player,
                weapon="hegrenade",
                damage_health=12,
            ),
        ),
        blinds=(
            DemoBlindEvent(
                tick=6208,
                time_seconds=97,
                round_number=4,
                attacker_steam_id=player,
                victim_steam_id=player,
                blind_duration=4.0,
            ),
        ),
    )

    windows = DemoHighlightAnalyzer().analyze(timeline, tracked_steam_ids=(player,))

    assert len(windows) == 1
    assert windows[0].tags == (
        "lowlight_self_flash",
        "lowlight_self_damage",
        "lowlight_knife_fail",
    )
    assert windows[0].score >= 80


def test_lowlight_is_suppressed_when_lead_in_contains_a_player_kill() -> None:
    player = "76561198000000123"
    enemy = "76561198000000456"
    timeline = DemoMatchTimeline(
        match_id="not-a-one-kill-highlight",
        map_name="de_ancient",
        tick_rate=64,
        kills=(
            DemoKillEvent(
                tick=round(96 * 64),
                time_seconds=96,
                round_number=4,
                attacker_steam_id=player,
                victim_steam_id="76561198000000789",
                weapon="ak47",
                headshot=False,
                attacker_team="T",
                victim_team="CT",
            ),
            DemoKillEvent(
                tick=round(100 * 64),
                time_seconds=100,
                round_number=4,
                attacker_steam_id=enemy,
                victim_steam_id=player,
                weapon="ak47",
                headshot=False,
                attacker_team="CT",
                victim_team="T",
                distance_meters=4.0,
            ),
        ),
        weapon_fires=tuple(
            DemoWeaponFireEvent(
                tick=round((97 + index * 0.2) * 64),
                time_seconds=97 + index * 0.2,
                round_number=4,
                player_steam_id=player,
                weapon="ak47",
            )
            for index in range(12)
        ),
    )

    assert DemoHighlightAnalyzer().analyze(timeline, tracked_steam_ids=(player,)) == ()


def test_world_death_is_lowlight_but_c4_death_is_not() -> None:
    player = "76561198000000123"
    world_death = DemoKillEvent(
        tick=6400,
        time_seconds=100,
        round_number=4,
        attacker_steam_id="",
        victim_steam_id=player,
        weapon="world",
        headshot=False,
    )
    c4_death = DemoKillEvent(
        tick=12800,
        time_seconds=200,
        round_number=8,
        attacker_steam_id=player,
        victim_steam_id=player,
        weapon="planted_c4",
        headshot=False,
    )
    timeline = DemoMatchTimeline(
        match_id="world",
        map_name="de_nuke",
        tick_rate=64,
        kills=(world_death, c4_death),
    )

    windows = DemoHighlightAnalyzer().analyze(timeline, tracked_steam_ids=(player,))

    assert len(windows) == 1
    assert windows[0].tags == ("lowlight_world_death",)


class _FakeTable:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def to_dict(self, orient: str = "records") -> list[dict[str, object]]:
        assert orient == "records"
        return self.rows


class _FakeDemoparserBackend:
    def parse_header(self) -> dict[str, object]:
        return {
            "map_name": "de_ancient",
            "playback_ticks": 6400,
            "playback_time": 100,
            "patch_version": "fixture",
        }

    def parse_event(
        self,
        name: str,
        *,
        other: list[str] | None = None,
    ) -> _FakeTable:
        assert other == ["total_rounds_played", "is_warmup_period"]
        if name == "player_death":
            return _FakeTable(
                [
                    {
                        "tick": 100,
                        "total_rounds_played": 0,
                        "is_warmup_period": True,
                        "attacker_steamid": 76561198000000001,
                        "user_steamid": 76561198000000009,
                        "weapon": "weapon_knife",
                    },
                    {
                        "tick": 640,
                        "total_rounds_played": 0,
                        "is_warmup_period": False,
                        "attacker_steamid": 76561198000000001,
                        "user_steamid": 76561198000000002,
                        "attacker_team_name": "CT",
                        "user_team_name": "TERRORIST",
                        "weapon": "weapon_knife",
                        "headshot": False,
                    },
                    {
                        "tick": 700,
                        "total_rounds_played": 0,
                        "is_warmup_period": False,
                        "attacker_steamid": 76561198000000001,
                        "user_steamid": 76561198000000004,
                        "attacker_team_name": "CT",
                        "user_team_name": "TERRORIST",
                        "weapon": "ak47",
                        "headshot": False,
                    },
                    {
                        "tick": 760,
                        "total_rounds_played": 0,
                        "is_warmup_period": False,
                        "attacker_steamid": 76561198000000001,
                        "user_steamid": 76561198000000005,
                        "attacker_team_name": "CT",
                        "user_team_name": "TERRORIST",
                        "weapon": "ak47",
                        "headshot": False,
                    },
                ]
            )
        if name == "round_start":
            return _FakeTable(
                [
                    {
                        "tick": 0,
                        "total_rounds_played": 0,
                        "is_warmup_period": False,
                    }
                ]
            )
        if name == "round_end":
            return _FakeTable(
                [
                    {
                        "tick": 1280,
                        "total_rounds_played": 0,
                        "is_warmup_period": False,
                        "winner": "CT",
                    }
                ]
            )
        raise AssertionError(name)

    def parse_ticks(self, props: list[str], *, ticks: list[int]) -> _FakeTable:
        assert props == ["is_alive", "team_name"]
        assert ticks == [640, 700, 760]
        rows: list[dict[str, object]] = []
        for tick in ticks:
            rows.extend(
                [
                    {
                        "tick": tick,
                        "steamid": 76561198000000001,
                        "team_name": "CT",
                        "is_alive": True,
                    },
                    {
                        "tick": tick,
                        "steamid": 76561198000000003,
                        "team_name": "TERRORIST",
                        "is_alive": True,
                    },
                ]
            )
        return _FakeTable(rows)


def test_demoparser2_adapter_is_optional_and_backend_is_injectable(tmp_path: Path) -> None:
    demo = tmp_path / "fixture.dem"
    demo.write_bytes(b"fixture")
    adapter = Demoparser2Adapter(lambda _: _FakeDemoparserBackend())

    timeline = adapter.parse(demo)
    windows = DemoHighlightExtractor(adapter).extract(demo, match_id="match-id")

    assert timeline.tick_rate == 64
    assert timeline.map_name == "de_ancient"
    assert len(timeline.kills) == 3
    assert timeline.kills[0].round_number == 1
    assert timeline.kills[0].weapon == "knife"
    assert timeline.kills[0].attacker_team_alive == 1
    assert timeline.kills[0].victim_team_alive == 2
    assert windows[0].match_id == "match-id"
    assert windows[0].tags == ("3k", "knife_kill", "clutch_clue")
    assert windows[0].clutch_confirmed is True
