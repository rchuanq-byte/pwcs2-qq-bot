"""Normalize CS2 demo events and select deterministic highlight windows.

The scoring code deliberately depends on a small, parser-neutral timeline model.
Production can use :class:`Demoparser2Adapter`, while tests and alternate demo
providers can inject any object implementing :class:`DemoEventParser`.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

HIGHLIGHT_SCHEMA_VERSION = 1

_KNIFE_WEAPONS = frozenset(
    {
        "bayonet",
        "butterfly",
        "falchion",
        "ggknife",
        "gut",
        "huntsman",
        "karambit",
        "knife",
        "knife_t",
        "kukri",
        "m9_bayonet",
        "navaja",
        "nomad",
        "paracord",
        "push",
        "skeleton",
        "stiletto",
        "survival_bowie",
        "talon",
        "ursus",
    }
)


class DemoParseError(RuntimeError):
    """Raised when a demo backend cannot produce a normalized timeline."""


class DemoParserUnavailableError(DemoParseError):
    """Raised when an optional demo parser is not installed."""


@dataclass(frozen=True, slots=True)
class DemoKillEvent:
    """One normalized ``player_death`` event.

    Alive counts refer to the instant immediately before the kill.  They are
    optional because not every parser/provider exposes tick-state snapshots.
    """

    tick: int
    time_seconds: float
    round_number: int
    attacker_steam_id: str
    victim_steam_id: str
    weapon: str
    headshot: bool = False
    attacker_team: str = ""
    victim_team: str = ""
    attacker_team_alive: int | None = None
    victim_team_alive: int | None = None
    distance_meters: float | None = None


@dataclass(frozen=True, slots=True)
class DemoWeaponFireEvent:
    tick: int
    time_seconds: float
    round_number: int
    player_steam_id: str
    weapon: str


@dataclass(frozen=True, slots=True)
class DemoHurtEvent:
    tick: int
    time_seconds: float
    round_number: int
    attacker_steam_id: str
    victim_steam_id: str
    weapon: str
    damage_health: int


@dataclass(frozen=True, slots=True)
class DemoBlindEvent:
    tick: int
    time_seconds: float
    round_number: int
    attacker_steam_id: str
    victim_steam_id: str
    blind_duration: float


@dataclass(frozen=True, slots=True)
class DemoRound:
    round_number: int
    start_seconds: float | None = None
    end_seconds: float | None = None
    winner_team: str = ""


@dataclass(frozen=True, slots=True)
class DemoMatchTimeline:
    match_id: str
    map_name: str
    tick_rate: float
    kills: tuple[DemoKillEvent, ...]
    weapon_fires: tuple[DemoWeaponFireEvent, ...] = ()
    hurts: tuple[DemoHurtEvent, ...] = ()
    blinds: tuple[DemoBlindEvent, ...] = ()
    rounds: tuple[DemoRound, ...] = ()
    duration_seconds: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class DemoEventParser(Protocol):
    """Parser boundary used by :class:`DemoHighlightExtractor`."""

    def parse(self, demo_path: str | Path) -> DemoMatchTimeline:
        """Parse one local demo into normalized, chronological events."""


@dataclass(frozen=True, slots=True)
class HighlightPolicy:
    pre_roll_seconds: float = 8.0
    post_roll_seconds: float = 10.0
    max_window_seconds: float = 55.0
    headshot_chain_seconds: float = 12.0
    minimum_headshot_chain: int = 2
    minimum_lowlight_score: int = 65
    lowlight_lookback_seconds: float = 5.0
    self_flash_death_seconds: float = 6.0


@dataclass(frozen=True, slots=True)
class HighlightWindow:
    """Stable render manifest for one player in one round."""

    highlight_id: str
    match_id: str
    map_name: str
    player_steam_id: str
    round_number: int
    tags: tuple[str, ...]
    start_seconds: float
    end_seconds: float
    start_tick: int
    end_tick: int
    score: int
    score_breakdown: Mapping[str, int]
    kill_count: int
    headshot_count: int
    knife_kill_count: int
    headshot_streak: int
    clutch_opponents: int | None
    clutch_confirmed: bool
    events: tuple[DemoKillEvent, ...]

    def to_payload(self) -> dict[str, Any]:
        """Return the versioned, renderer-facing JSON object."""
        return {
            "schema_version": HIGHLIGHT_SCHEMA_VERSION,
            "highlight_id": self.highlight_id,
            "match_id": self.match_id,
            "map_name": self.map_name,
            "player_steam_id": self.player_steam_id,
            "round_number": self.round_number,
            "tags": list(self.tags),
            "window": {
                "start_seconds": _round_seconds(self.start_seconds),
                "end_seconds": _round_seconds(self.end_seconds),
                "start_tick": self.start_tick,
                "end_tick": self.end_tick,
            },
            "score": self.score,
            "score_breakdown": dict(sorted(self.score_breakdown.items())),
            "stats": {
                "kills": self.kill_count,
                "headshots": self.headshot_count,
                "knife_kills": self.knife_kill_count,
                "headshot_streak": self.headshot_streak,
            },
            "clutch": (
                None
                if self.clutch_opponents is None
                else {
                    "opponents": self.clutch_opponents,
                    "confirmed": self.clutch_confirmed,
                }
            ),
            "events": [
                {
                    "tick": event.tick,
                    "time_seconds": _round_seconds(event.time_seconds),
                    "victim_steam_id": event.victim_steam_id,
                    "weapon": _normalize_weapon(event.weapon),
                    "headshot": event.headshot,
                }
                for event in self.events
            ],
        }

    def to_json(self) -> str:
        """Serialize canonically so hashes and persisted jobs are reproducible."""
        return json.dumps(
            self.to_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


class DemoHighlightAnalyzer:
    """Detect high-signal moments from a parser-neutral demo timeline."""

    _TAG_ORDER = (
        "3k",
        "4k",
        "ace",
        "headshot_streak",
        "knife_kill",
        "clutch_clue",
        "lowlight_close_whiff",
        "lowlight_self_flash",
        "lowlight_self_damage",
        "lowlight_knife_fail",
        "lowlight_advantage_throw",
        "lowlight_teamkill",
        "lowlight_world_death",
    )

    def __init__(self, policy: HighlightPolicy | None = None) -> None:
        self.policy = policy or HighlightPolicy()

    def analyze(
        self,
        timeline: DemoMatchTimeline,
        *,
        tracked_steam_ids: Sequence[str] | None = None,
    ) -> tuple[HighlightWindow, ...]:
        tracked = (
            None
            if tracked_steam_ids is None
            else {str(value).strip() for value in tracked_steam_ids if str(value).strip()}
        )
        grouped: dict[tuple[int, str], list[DemoKillEvent]] = defaultdict(list)
        for event in sorted(
            timeline.kills,
            key=lambda item: (item.round_number, item.time_seconds, item.tick),
        ):
            attacker = event.attacker_steam_id.strip()
            if not attacker or attacker == event.victim_steam_id.strip():
                continue
            if tracked is not None and attacker not in tracked:
                continue
            if (
                event.attacker_team
                and event.victim_team
                and _normalize_team(event.attacker_team) == _normalize_team(event.victim_team)
            ):
                continue
            grouped[(event.round_number, attacker)].append(event)

        rounds = {item.round_number: item for item in timeline.rounds}
        windows: list[HighlightWindow] = []
        for (round_number, steam_id), kills in grouped.items():
            window = self._analyze_player_round(
                timeline,
                rounds.get(round_number),
                steam_id,
                round_number,
                kills,
            )
            if window is not None:
                windows.append(window)
        lowlights = self._analyze_lowlights(timeline, tracked)
        if lowlights:
            # At most one funny failure is eligible per match.  This keeps the
            # group useful even when several members have an unfortunate map.
            windows.append(
                min(
                    lowlights,
                    key=lambda item: (
                        -item.score,
                        item.round_number,
                        item.start_seconds,
                        item.player_steam_id,
                    ),
                )
            )
        return tuple(
            sorted(
                windows,
                key=lambda item: (
                    -item.score,
                    item.round_number,
                    item.start_seconds,
                    item.player_steam_id,
                ),
            )
        )

    def _analyze_lowlights(
        self,
        timeline: DemoMatchTimeline,
        tracked: set[str] | None,
    ) -> list[HighlightWindow]:
        rounds = {item.round_number: item for item in timeline.rounds}
        candidates: list[HighlightWindow] = []
        for death in timeline.kills:
            victim = death.victim_steam_id.strip()
            attacker = death.attacker_steam_id.strip()
            friendly_fire = bool(
                attacker
                and attacker != victim
                and death.attacker_team
                and death.victim_team
                and _normalize_team(death.attacker_team) == _normalize_team(death.victim_team)
            )
            if friendly_fire and (tracked is None or attacker in tracked):
                candidates.append(
                    self._build_lowlight_window(
                        timeline,
                        rounds.get(death.round_number),
                        attacker,
                        death,
                        {"lowlight_teamkill": 78},
                    )
                )
            if not victim or (tracked is not None and victim not in tracked):
                continue
            score_breakdown = self._victim_lowlight_scores(
                timeline,
                rounds.get(death.round_number),
                death,
            )
            if not score_breakdown:
                continue
            candidate = self._build_lowlight_window(
                timeline,
                rounds.get(death.round_number),
                victim,
                death,
                score_breakdown,
            )
            if candidate.score >= max(1, int(self.policy.minimum_lowlight_score)):
                candidates.append(candidate)
        return candidates

    def _victim_lowlight_scores(
        self,
        timeline: DemoMatchTimeline,
        round_info: DemoRound | None,
        death: DemoKillEvent,
    ) -> dict[str, int]:
        player = death.victim_steam_id
        tick_rate = timeline.tick_rate if timeline.tick_rate > 0 else 64.0
        lookback_ticks = round(max(0.5, self.policy.lowlight_lookback_seconds) * tick_rate)
        render_lookback_ticks = round(max(0.0, self.policy.pre_roll_seconds) * tick_rate)

        # A lowlight clip must not masquerade as a one-kill highlight.  If the
        # tracked player scored an enemy kill anywhere in the visible lead-in,
        # leave the match to the normal multi-kill rules instead.
        if any(
            event.attacker_steam_id == player
            and event.victim_steam_id != player
            and _same_or_unknown_round(event.round_number, death.round_number)
            and 0 <= death.tick - event.tick <= render_lookback_ticks
            for event in timeline.kills
        ):
            return {}

        recent_fires = [
            event
            for event in timeline.weapon_fires
            if event.player_steam_id == player
            and _same_or_unknown_round(event.round_number, death.round_number)
            and 0 <= death.tick - event.tick <= lookback_ticks
        ]
        gun_fires = [event for event in recent_fires if _is_firearm(event.weapon)]
        landed_hits = [
            event
            for event in timeline.hurts
            if event.attacker_steam_id == player
            and event.victim_steam_id != player
            and event.damage_health > 0
            and _same_or_unknown_round(event.round_number, death.round_number)
            and 0 <= death.tick - event.tick <= lookback_ticks
        ]
        knife_swings = sum(_is_knife(event.weapon) for event in recent_fires)
        scores: dict[str, int] = {}

        if (
            death.distance_meters is not None
            and death.distance_meters <= 10.0
            and len(gun_fires) >= 10
            and not landed_hits
        ):
            scores["lowlight_close_whiff"] = min(84, 60 + (len(gun_fires) - 8) * 4)

        if (
            death.distance_meters is not None
            and death.distance_meters <= 8.0
            and knife_swings >= 2
        ):
            scores["lowlight_knife_fail"] = min(84, 66 + knife_swings * 3)

        flash_ticks = round(max(0.5, self.policy.self_flash_death_seconds) * tick_rate)
        self_flashes = [
            event
            for event in timeline.blinds
            if event.attacker_steam_id == player
            and event.victim_steam_id == player
            and event.blind_duration >= 1.5
            and _same_or_unknown_round(event.round_number, death.round_number)
            and 0 <= death.tick - event.tick <= flash_ticks
        ]
        if self_flashes:
            duration = max(event.blind_duration for event in self_flashes)
            scores["lowlight_self_flash"] = min(84, 66 + round(duration * 3))

        self_damage = sum(
            event.damage_health
            for event in timeline.hurts
            if event.attacker_steam_id == player
            and event.victim_steam_id == player
            and event.damage_health > 0
            and _is_self_damage_weapon(event.weapon)
            and _same_or_unknown_round(event.round_number, death.round_number)
            and 0 <= death.tick - event.tick <= flash_ticks
        )
        if self_damage >= 8:
            scores["lowlight_self_damage"] = min(84, 64 + self_damage // 4)

        weapon = _normalize_weapon(death.weapon)
        self_or_world_death = death.attacker_steam_id == player or (
            not death.attacker_steam_id and weapon == "world"
        )
        if (
            weapon not in {"planted_c4", "c4"}
            and self_or_world_death
        ):
            if weapon in {"hegrenade", "inferno", "molotov", "incgrenade"}:
                scores["lowlight_self_damage"] = max(
                    scores.get("lowlight_self_damage", 0),
                    76,
                )
            else:
                scores["lowlight_world_death"] = 76

        winner = _normalize_team(round_info.winner_team if round_info else "")
        victim_team = _normalize_team(death.victim_team)
        if (
            death.attacker_team_alive == 1
            and death.victim_team_alive is not None
            and death.victim_team_alive >= 3
            and winner
            and victim_team
            and winner != victim_team
        ):
            scores["lowlight_advantage_throw"] = 82
        return scores

    def _build_lowlight_window(
        self,
        timeline: DemoMatchTimeline,
        round_info: DemoRound | None,
        player_steam_id: str,
        death: DemoKillEvent,
        score_breakdown: Mapping[str, int],
    ) -> HighlightWindow:
        tags = tuple(tag for tag in self._TAG_ORDER if tag in score_breakdown)
        start_seconds, end_seconds = self._window_bounds(death, death, round_info, timeline)
        tick_rate = timeline.tick_rate if timeline.tick_rate > 0 else 64.0
        start_tick = max(
            0,
            death.tick - round(max(0.0, death.time_seconds - start_seconds) * tick_rate),
        )
        end_tick = max(
            death.tick,
            death.tick + round(max(0.0, end_seconds - death.time_seconds) * tick_rate),
        )
        score = min(100, max(score_breakdown.values()) + max(0, len(tags) - 1) * 4)
        identity = {
            "schema_version": HIGHLIGHT_SCHEMA_VERSION,
            "match_id": timeline.match_id,
            "player_steam_id": player_steam_id,
            "round_number": death.round_number,
            "tags": tags,
            "ticks": [death.tick],
        }
        highlight_id = hashlib.sha256(
            json.dumps(identity, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()[:24]
        return HighlightWindow(
            highlight_id=highlight_id,
            match_id=timeline.match_id,
            map_name=timeline.map_name,
            player_steam_id=player_steam_id,
            round_number=death.round_number,
            tags=tags,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            start_tick=start_tick,
            end_tick=end_tick,
            score=score,
            score_breakdown=dict(score_breakdown),
            kill_count=0,
            headshot_count=0,
            knife_kill_count=0,
            headshot_streak=0,
            clutch_opponents=None,
            clutch_confirmed=False,
            events=(death,),
        )

    def _analyze_player_round(
        self,
        timeline: DemoMatchTimeline,
        round_info: DemoRound | None,
        steam_id: str,
        round_number: int,
        kills: list[DemoKillEvent],
    ) -> HighlightWindow | None:
        kill_count = len(kills)
        tags: list[str] = []
        score_breakdown: dict[str, int] = {}
        anchors: list[DemoKillEvent] = []

        if kill_count >= 5:
            tags.append("ace")
            score_breakdown["ace"] = 92
            anchors.extend(kills)
        elif kill_count == 4:
            tags.append("4k")
            score_breakdown["4k"] = 78
            anchors.extend(kills)
        elif kill_count == 3:
            tags.append("3k")
            score_breakdown["3k"] = 60
            anchors.extend(kills)

        best_headshot_chain = self._best_headshot_chain(kills)
        if len(best_headshot_chain) >= max(2, self.policy.minimum_headshot_chain):
            tags.append("headshot_streak")
            score_breakdown["headshot_streak"] = min(
                68,
                34 + (len(best_headshot_chain) - 2) * 10,
            )
            anchors.extend(best_headshot_chain)

        knife_kills = [event for event in kills if _is_knife(event.weapon)]
        if knife_kills and kill_count >= 2:
            tags.append("knife_kill")
            score_breakdown["knife_kill"] = min(55, 38 + (len(knife_kills) - 1) * 8)
            anchors.extend(knife_kills)

        clutch_events = [
            event
            for event in kills
            if event.attacker_team_alive == 1
            and event.victim_team_alive is not None
            and event.victim_team_alive >= 2
        ]
        clutch_opponents: int | None = None
        clutch_confirmed = False
        if clutch_events and kill_count >= 2:
            tags.append("clutch_clue")
            clutch_opponents = max(int(event.victim_team_alive or 0) for event in clutch_events)
            winner = _normalize_team(round_info.winner_team if round_info else "")
            attacker_team = _normalize_team(clutch_events[0].attacker_team)
            clutch_confirmed = bool(winner and attacker_team and winner == attacker_team)
            score_breakdown["clutch_clue"] = min(
                82,
                48 + max(0, clutch_opponents - 2) * 8 + (12 if clutch_confirmed else 0),
            )
            # The last elimination in a clutch normally reports one opponent
            # alive, so it is not itself a ``clutch_event``.  Keep every later
            # kill in the round after the first 1vX clue; otherwise the render
            # window can end exactly on the penultimate/final kill tick.
            clutch_start = clutch_events[0]
            anchors.extend(
                event
                for event in kills
                if (event.time_seconds, event.tick)
                >= (clutch_start.time_seconds, clutch_start.tick)
            )

        if not tags:
            return None

        unique_anchors = _unique_events(anchors)
        first = unique_anchors[0]
        last = unique_anchors[-1]
        start_seconds, end_seconds = self._window_bounds(first, last, round_info, timeline)
        tick_rate = timeline.tick_rate if timeline.tick_rate > 0 else 64.0
        start_tick = max(
            0,
            first.tick - round(max(0.0, first.time_seconds - start_seconds) * tick_rate),
        )
        end_tick = max(
            last.tick,
            last.tick + round(max(0.0, end_seconds - last.time_seconds) * tick_rate),
        )
        score = min(
            100,
            max(score_breakdown.values()) + max(0, len(tags) - 1) * 4,
        )
        ordered_tags = tuple(tag for tag in self._TAG_ORDER if tag in tags)
        identity = {
            "schema_version": HIGHLIGHT_SCHEMA_VERSION,
            "match_id": timeline.match_id,
            "player_steam_id": steam_id,
            "round_number": round_number,
            "tags": ordered_tags,
            "ticks": [event.tick for event in unique_anchors],
        }
        highlight_id = hashlib.sha256(
            json.dumps(identity, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()[:24]
        return HighlightWindow(
            highlight_id=highlight_id,
            match_id=timeline.match_id,
            map_name=timeline.map_name,
            player_steam_id=steam_id,
            round_number=round_number,
            tags=ordered_tags,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            start_tick=start_tick,
            end_tick=end_tick,
            score=score,
            score_breakdown=score_breakdown,
            kill_count=kill_count,
            headshot_count=sum(event.headshot for event in kills),
            knife_kill_count=len(knife_kills),
            headshot_streak=len(best_headshot_chain),
            clutch_opponents=clutch_opponents,
            clutch_confirmed=clutch_confirmed,
            events=tuple(unique_anchors),
        )

    def _best_headshot_chain(self, kills: list[DemoKillEvent]) -> list[DemoKillEvent]:
        best: list[DemoKillEvent] = []
        current: list[DemoKillEvent] = []
        for event in kills:
            if not event.headshot:
                current = []
                continue
            if (
                current
                and event.time_seconds - current[-1].time_seconds
                > self.policy.headshot_chain_seconds
            ):
                current = []
            current.append(event)
            if len(current) > len(best):
                best = list(current)
        return best

    def _window_bounds(
        self,
        first: DemoKillEvent,
        last: DemoKillEvent,
        round_info: DemoRound | None,
        timeline: DemoMatchTimeline,
    ) -> tuple[float, float]:
        start = max(0.0, first.time_seconds - max(0.0, self.policy.pre_roll_seconds))
        end = last.time_seconds + max(0.0, self.policy.post_roll_seconds)
        if (
            round_info
            and round_info.start_seconds is not None
            and 0.0 <= round_info.start_seconds <= first.time_seconds
        ):
            start = max(start, round_info.start_seconds)
        # Round-end timestamps are not a safe upper bound: some Perfect World
        # demos expose round-relative values through demoparser2, and a valid
        # end immediately after the final kill would still cut its animation.
        # Only a plausible whole-demo duration may shorten the requested tail.
        if (
            timeline.duration_seconds is not None
            and timeline.duration_seconds >= last.time_seconds
        ):
            end = min(end, timeline.duration_seconds)

        # Treat the configured maximum as a soft cap: reduce handles first, but
        # never cut away a kill that caused the highlight.
        anchor_span = max(0.0, last.time_seconds - first.time_seconds)
        maximum = max(0.0, self.policy.max_window_seconds)
        if maximum and end - start > maximum and anchor_span <= maximum:
            spare = maximum - anchor_span
            # Preserve the aftermath before pre-roll when a long multikill has
            # to be squeezed into the render budget.
            after = min(end - last.time_seconds, spare)
            before = min(first.time_seconds - start, spare - after)
            start = first.time_seconds - before
            end = last.time_seconds + after
        return _round_seconds(start), _round_seconds(max(last.time_seconds, end))


class DemoHighlightExtractor:
    """Convenience facade combining an injected parser and the analyzer."""

    def __init__(
        self,
        parser: DemoEventParser,
        analyzer: DemoHighlightAnalyzer | None = None,
    ) -> None:
        self.parser = parser
        self.analyzer = analyzer or DemoHighlightAnalyzer()

    def extract(
        self,
        demo_path: str | Path,
        *,
        match_id: str | None = None,
        tracked_steam_ids: Sequence[str] | None = None,
    ) -> tuple[HighlightWindow, ...]:
        timeline = self.parser.parse(demo_path)
        if match_id is not None and match_id.strip() and timeline.match_id != match_id.strip():
            timeline = replace(timeline, match_id=match_id.strip())
        return self.analyzer.analyze(timeline, tracked_steam_ids=tracked_steam_ids)


class Demoparser2Adapter:
    """Optional adapter for ``demoparser2`` without making it a core dependency.

    ``backend_factory`` is intentionally injectable.  Tests can supply a fake
    object returning record lists, while production lazily imports the native
    extension only in the dedicated parser worker.
    """

    def __init__(self, backend_factory: Callable[[str], Any] | None = None) -> None:
        self._backend_factory = backend_factory

    def parse(self, demo_path: str | Path) -> DemoMatchTimeline:
        path = Path(demo_path).expanduser().resolve()
        if not path.is_file():
            raise DemoParseError(f"demo file does not exist: {path}")
        backend = self._make_backend(path)
        try:
            header = dict(backend.parse_header() or {})
            deaths = _records(
                backend.parse_event(
                    "player_death",
                    other=["total_rounds_played", "is_warmup_period"],
                )
            )
        except Exception as exc:
            raise DemoParseError(f"unable to parse player_death events: {exc}") from exc
        tick_rate = _tick_rate(header)
        deaths = [row for row in deaths if not _as_bool(row.get("is_warmup_period"))]
        death_ticks = sorted({_as_int(row.get("tick")) for row in deaths})
        state_by_tick = self._alive_state_by_tick(backend, death_ticks)
        kills = tuple(
            event
            for event in (self._kill_from_row(row, tick_rate, state_by_tick) for row in deaths)
            if event is not None
        )
        weapon_fires = tuple(
            event
            for event in (
                self._weapon_fire_from_row(row, tick_rate)
                for row in self._optional_event_rows(backend, "weapon_fire")
            )
            if event is not None
        )
        hurts = tuple(
            event
            for event in (
                self._hurt_from_row(row, tick_rate)
                for row in self._optional_event_rows(backend, "player_hurt")
            )
            if event is not None
        )
        blinds = tuple(
            event
            for event in (
                self._blind_from_row(row, tick_rate)
                for row in self._optional_event_rows(backend, "player_blind")
            )
            if event is not None
        )
        rounds = self._rounds(backend, tick_rate)
        duration = _duration_seconds(header, kills, rounds)
        match_id = str(header.get("match_id") or path.stem).strip()
        map_name = str(header.get("map_name") or header.get("map") or "").strip()
        safe_metadata = {
            key: value
            for key, value in header.items()
            if isinstance(value, (str, int, float, bool)) or value is None
        }
        return DemoMatchTimeline(
            match_id=match_id,
            map_name=map_name,
            tick_rate=tick_rate,
            kills=tuple(sorted(kills, key=lambda item: (item.tick, item.time_seconds))),
            weapon_fires=tuple(sorted(weapon_fires, key=lambda item: item.tick)),
            hurts=tuple(sorted(hurts, key=lambda item: item.tick)),
            blinds=tuple(sorted(blinds, key=lambda item: item.tick)),
            rounds=rounds,
            duration_seconds=duration,
            metadata=safe_metadata,
        )

    def _make_backend(self, path: Path) -> Any:
        factory = self._backend_factory
        if factory is None:
            try:
                from demoparser2 import DemoParser  # type: ignore[import-not-found]
            except ImportError as exc:
                raise DemoParserUnavailableError(
                    "demoparser2 is not installed in this worker"
                ) from exc
            factory = DemoParser
        return factory(str(path))

    @staticmethod
    def _optional_event_rows(backend: Any, name: str) -> list[Mapping[str, Any]]:
        try:
            rows = _records(
                backend.parse_event(
                    name,
                    other=["total_rounds_played", "is_warmup_period"],
                )
            )
        except Exception:
            try:
                rows = _records(backend.parse_event(name))
            except Exception:
                return []
        return [row for row in rows if not _as_bool(row.get("is_warmup_period"))]

    @staticmethod
    def _alive_state_by_tick(
        backend: Any,
        ticks: list[int],
    ) -> dict[int, dict[str, Any]]:
        if not ticks:
            return {}
        try:
            rows = _records(backend.parse_ticks(["is_alive", "team_name"], ticks=ticks))
        except Exception:
            # Clutch inference is optional; useful kill highlights must survive a
            # parser version that cannot expose tick properties for a new patch.
            return {}
        state: dict[int, dict[str, Any]] = defaultdict(lambda: {"teams": {}, "players": {}})
        for row in rows:
            tick = _as_int(row.get("tick"))
            team = _normalize_team(_first(row, "team_name", "team"))
            steam_id = _steam_id(_first(row, "steamid", "steam_id"))
            alive = _as_bool(_first(row, "is_alive", "alive"))
            if team and alive:
                teams = state[tick]["teams"]
                teams[team] = int(teams.get(team, 0)) + 1
            if steam_id:
                state[tick]["players"][steam_id] = {"team": team, "alive": alive}
        return dict(state)

    @staticmethod
    def _kill_from_row(
        row: Mapping[str, Any],
        tick_rate: float,
        state_by_tick: Mapping[int, Mapping[str, Any]],
    ) -> DemoKillEvent | None:
        tick = _as_int(row.get("tick"))
        attacker = _steam_id(_first(row, "attacker_steamid", "attacker_steam_id", "attacker_xuid"))
        victim = _steam_id(
            _first(row, "user_steamid", "victim_steamid", "victim_steam_id", "userid")
        )
        if not victim:
            return None
        snapshot = state_by_tick.get(tick, {})
        players = snapshot.get("players", {}) if isinstance(snapshot, Mapping) else {}
        teams = snapshot.get("teams", {}) if isinstance(snapshot, Mapping) else {}
        attacker_team = _normalize_team(
            _first(row, "attacker_team_name", "attacker_team") or _nested_team(players, attacker)
        )
        victim_team = _normalize_team(
            _first(row, "user_team_name", "victim_team_name", "victim_team")
            or _nested_team(players, victim)
        )
        own_alive = _optional_int(_first(row, "attacker_team_alive", "attacker_alive_count"))
        opponent_alive = _optional_int(_first(row, "victim_team_alive", "opponent_alive_count"))
        if own_alive is None and attacker_team:
            own_alive = _optional_int(teams.get(attacker_team))
        if opponent_alive is None and victim_team:
            after_count = _optional_int(teams.get(victim_team))
            if after_count is not None:
                victim_state = players.get(victim, {}) if isinstance(players, Mapping) else {}
                victim_alive = (
                    _as_bool(victim_state.get("alive"))
                    if isinstance(victim_state, Mapping)
                    else False
                )
                opponent_alive = after_count + (0 if victim_alive else 1)
        raw_round = _first(row, "total_rounds_played", "round_number", "round")
        round_number = _as_int(raw_round) + (1 if "total_rounds_played" in row else 0)
        raw_time = _first(row, "time_seconds", "seconds", "time")
        time_seconds = _as_float(raw_time, tick / tick_rate if tick_rate else 0.0)
        return DemoKillEvent(
            tick=tick,
            time_seconds=max(0.0, time_seconds),
            round_number=max(1, round_number),
            attacker_steam_id=attacker,
            victim_steam_id=victim,
            weapon=_normalize_weapon(_first(row, "weapon", "weapon_name")),
            headshot=_as_bool(_first(row, "headshot", "is_headshot")),
            attacker_team=attacker_team,
            victim_team=victim_team,
            attacker_team_alive=own_alive,
            victim_team_alive=opponent_alive,
            distance_meters=_optional_float(_first(row, "distance", "distance_meters")),
        )

    @staticmethod
    def _weapon_fire_from_row(
        row: Mapping[str, Any],
        tick_rate: float,
    ) -> DemoWeaponFireEvent | None:
        player = _steam_id(_first(row, "user_steamid", "steamid", "user_xuid"))
        if not player:
            return None
        tick = max(0, _as_int(row.get("tick")))
        return DemoWeaponFireEvent(
            tick=tick,
            time_seconds=_event_seconds(row, tick, tick_rate),
            round_number=_optional_event_round_number(row),
            player_steam_id=player,
            weapon=_normalize_weapon(_first(row, "weapon", "weapon_name")),
        )

    @staticmethod
    def _hurt_from_row(
        row: Mapping[str, Any],
        tick_rate: float,
    ) -> DemoHurtEvent | None:
        attacker = _steam_id(
            _first(row, "attacker_steamid", "attacker_steam_id", "attacker_xuid")
        )
        victim = _steam_id(
            _first(row, "user_steamid", "victim_steamid", "victim_steam_id")
        )
        if not attacker or not victim:
            return None
        tick = max(0, _as_int(row.get("tick")))
        return DemoHurtEvent(
            tick=tick,
            time_seconds=_event_seconds(row, tick, tick_rate),
            round_number=_optional_event_round_number(row),
            attacker_steam_id=attacker,
            victim_steam_id=victim,
            weapon=_normalize_weapon(_first(row, "weapon", "weapon_name")),
            damage_health=max(0, _as_int(_first(row, "dmg_health", "damage_health"))),
        )

    @staticmethod
    def _blind_from_row(
        row: Mapping[str, Any],
        tick_rate: float,
    ) -> DemoBlindEvent | None:
        attacker = _steam_id(
            _first(row, "attacker_steamid", "attacker_steam_id", "attacker_xuid")
        )
        victim = _steam_id(
            _first(row, "user_steamid", "victim_steamid", "victim_steam_id")
        )
        if not attacker or not victim:
            return None
        tick = max(0, _as_int(row.get("tick")))
        return DemoBlindEvent(
            tick=tick,
            time_seconds=_event_seconds(row, tick, tick_rate),
            round_number=_optional_event_round_number(row),
            attacker_steam_id=attacker,
            victim_steam_id=victim,
            blind_duration=max(
                0.0,
                _as_float(_first(row, "blind_duration", "duration")),
            ),
        )

    @staticmethod
    def _rounds(backend: Any, tick_rate: float) -> tuple[DemoRound, ...]:
        try:
            starts = _records(
                backend.parse_event(
                    "round_start",
                    other=["total_rounds_played", "is_warmup_period"],
                )
            )
        except Exception:
            starts = []
        try:
            ends = _records(
                backend.parse_event(
                    "round_end",
                    other=["total_rounds_played", "is_warmup_period"],
                )
            )
        except Exception:
            ends = []
        starts = [row for row in starts if not _as_bool(row.get("is_warmup_period"))]
        ends = [row for row in ends if not _as_bool(row.get("is_warmup_period"))]
        values: dict[int, dict[str, Any]] = defaultdict(dict)
        for index, row in enumerate(starts, start=1):
            number = _event_round_number(row, index)
            tick = _as_int(row.get("tick"))
            values[number]["start_seconds"] = _as_float(
                _first(row, "time_seconds", "seconds", "time"),
                tick / tick_rate if tick_rate else 0.0,
            )
        for index, row in enumerate(ends, start=1):
            number = _event_round_number(row, index)
            tick = _as_int(row.get("tick"))
            values[number]["end_seconds"] = _as_float(
                _first(row, "time_seconds", "seconds", "time"),
                tick / tick_rate if tick_rate else 0.0,
            )
            values[number]["winner_team"] = _normalize_team(
                _first(row, "winner", "winner_team", "team")
            )
        return tuple(
            DemoRound(round_number=number, **payload) for number, payload in sorted(values.items())
        )


def _records(value: Any) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if hasattr(value, "to_dicts"):
        value = value.to_dicts()
    elif hasattr(value, "to_dict"):
        try:
            value = value.to_dict(orient="records")
        except TypeError:
            value = value.to_dict("records")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise DemoParseError("parser returned a non-tabular event payload")
    return [item for item in value if isinstance(item, Mapping)]


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "y"}
    return bool(value)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return _as_int(value)


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    result = _as_float(value, float("nan"))
    return result if result == result else None


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if result == result else default


def _steam_id(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    return "" if text in {"0", "None", "nan"} else text


def _normalize_team(value: Any) -> str:
    text = str(value or "").strip().casefold()
    if text in {"2", "t", "terrorist", "terrorists"}:
        return "T"
    if text in {"3", "ct", "counterterrorist", "counter-terrorist"}:
        return "CT"
    return str(value or "").strip().upper()


def _normalize_weapon(value: Any) -> str:
    text = str(value or "unknown").strip().casefold().replace("-", "_")
    return text.removeprefix("weapon_") or "unknown"


def _is_knife(weapon: str) -> bool:
    normalized = _normalize_weapon(weapon)
    return normalized in _KNIFE_WEAPONS or "knife" in normalized or "bayonet" in normalized


def _is_firearm(weapon: str) -> bool:
    normalized = _normalize_weapon(weapon)
    if normalized == "unknown" or _is_knife(normalized):
        return False
    return not any(
        marker in normalized
        for marker in (
            "grenade",
            "flashbang",
            "smoke",
            "molotov",
            "incgrenade",
            "inferno",
            "decoy",
            "c4",
            "taser",
        )
    )


def _is_self_damage_weapon(weapon: str) -> bool:
    normalized = _normalize_weapon(weapon)
    return normalized in {"hegrenade", "inferno", "molotov", "incgrenade"}


def _nested_team(players: Any, steam_id: str) -> str:
    if not isinstance(players, Mapping):
        return ""
    player = players.get(steam_id)
    if not isinstance(player, Mapping):
        return ""
    return str(player.get("team") or "")


def _tick_rate(header: Mapping[str, Any]) -> float:
    explicit = _as_float(_first(header, "tick_rate", "tickrate"))
    if explicit > 0:
        return explicit
    ticks = _as_float(_first(header, "playback_ticks", "ticks"))
    duration = _as_float(_first(header, "playback_time", "duration_seconds"))
    if ticks > 0 and duration > 0:
        return ticks / duration
    return 64.0


def _duration_seconds(
    header: Mapping[str, Any],
    kills: Sequence[DemoKillEvent],
    rounds: Sequence[DemoRound],
) -> float | None:
    explicit = _as_float(_first(header, "playback_time", "duration_seconds"), -1.0)
    if explicit >= 0:
        return explicit
    ends = [item.end_seconds for item in rounds if item.end_seconds is not None]
    if ends:
        return max(ends)
    if kills:
        return max(item.time_seconds for item in kills)
    return None


def _event_round_number(row: Mapping[str, Any], fallback: int) -> int:
    if "total_rounds_played" in row:
        return max(1, _as_int(row.get("total_rounds_played")) + 1)
    return max(1, _as_int(_first(row, "round_number", "round"), fallback))


def _optional_event_round_number(row: Mapping[str, Any]) -> int:
    if "total_rounds_played" in row:
        return max(1, _as_int(row.get("total_rounds_played")) + 1)
    raw = _first(row, "round_number", "round")
    return 0 if raw is None else max(1, _as_int(raw))


def _event_seconds(row: Mapping[str, Any], tick: int, tick_rate: float) -> float:
    raw = _first(row, "time_seconds", "seconds", "time")
    return max(0.0, _as_float(raw, tick / tick_rate if tick_rate else 0.0))


def _same_or_unknown_round(candidate: int, target: int) -> bool:
    return candidate <= 0 or target <= 0 or candidate == target


def _unique_events(events: Sequence[DemoKillEvent]) -> list[DemoKillEvent]:
    unique: dict[tuple[int, str], DemoKillEvent] = {}
    for event in events:
        unique[(event.tick, event.victim_steam_id)] = event
    return sorted(unique.values(), key=lambda item: (item.time_seconds, item.tick))


def _round_seconds(value: float) -> float:
    return round(float(value), 3)
