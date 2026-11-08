"""Normalize and format completed PVP and official CS2 matches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pwcs2_bot.game_identity import game_name, game_profile, match_sort_key

MatchResult = Literal["win", "loss", "draw", "finished"]
MatchHistorySource = Literal["pvp", "official", "mixed"]


@dataclass(frozen=True, slots=True)
class MatchPlayerSnapshot:
    steam_id: str
    name: str
    avatar_url: str
    team: int
    team_id: str
    is_group_member: bool
    kills: str
    assists: str
    deaths: str
    kd: str
    rating: str
    adr: str
    we: str
    pvp_score: str
    score_change: str
    mvp: bool


@dataclass(frozen=True, slots=True)
class MatchSnapshot:
    match_id: str
    steam_id: str
    name: str
    avatar_url: str
    map_name: str
    mode: str
    result: MatchResult
    result_label: str
    own_score: str
    opponent_score: str
    team1_score: str
    team2_score: str
    team1_name: str
    team2_name: str
    kills: str
    assists: str
    deaths: str
    kd: str
    rating: str
    we: str
    pvp_score: str
    score_change: str
    duration: str
    ended_at: str
    k4: str
    k5: str
    mvp: bool
    players: tuple[MatchPlayerSnapshot, ...]
    source: Literal["pvp", "official"]


@dataclass(frozen=True, slots=True)
class MatchHistorySnapshot:
    steam_id: str
    name: str
    avatar_url: str
    total: int
    wins: int
    losses: int
    draws: int
    win_rate: str
    avg_rating: str
    elo_change: str
    matches: tuple[MatchSnapshot, ...]
    source: MatchHistorySource = "mixed"


def _text(value: Any, fallback: str = "-") -> str:
    if value is None:
        return fallback
    normalized = " ".join(str(value).split())
    return normalized or fallback


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _float(value)
    return None if number is None else int(number)


def _number(value: Any, digits: int = 2, fallback: str = "-") -> str:
    number = _float(value)
    if number is None:
        return fallback
    if number.is_integer():
        return str(int(number))
    return f"{number:.{digits}f}".rstrip("0").rstrip(".")


def _signed_number(value: Any) -> str:
    number = _float(value)
    if number is None:
        return "-"
    rendered = _number(number)
    return f"+{rendered}" if number > 0 else rendered


def _rating_value(data: dict[str, Any]) -> float | None:
    """Return the first meaningful rating across PVP and official schemas."""
    pw_rating = _float(data.get("pwRating"))
    rating = _float(data.get("rating"))
    if pw_rating is not None and pw_rating > 0:
        return pw_rating
    if rating is not None and rating > 0:
        return rating
    return None


def _is_official_match(
    match: dict[str, Any],
    base: dict[str, Any] | None = None,
) -> bool:
    base = base or {}
    if _integer(match.get("dataSource")) == 1 or _integer(base.get("dataSource")) == 1:
        return True
    match_id = _text(base.get("matchId") or match.get("matchId"), "").upper()
    return match_id.startswith(("LOCAL@", "MM@"))


def _player_snapshot(
    player: dict[str, Any],
    *,
    group_member_steam_ids: set[str],
    official: bool,
) -> MatchPlayerSnapshot:
    steam_id = _text(player.get("playerId") or player.get("steamId"), "未知")
    kills = _integer(player.get("kill") if "kill" in player else player.get("kills"))
    deaths = _integer(player.get("death") if "death" in player else player.get("deaths"))
    kd = kills / deaths if kills is not None and deaths else None
    return MatchPlayerSnapshot(
        steam_id=steam_id,
        name=game_name(player, steam_id),
        avatar_url=_text(player.get("avatar") or player.get("steamAvatar"), ""),
        team=_integer(player.get("team")) or 0,
        team_id=_text(player.get("teamId"), ""),
        is_group_member=steam_id in group_member_steam_ids,
        kills=_number(kills, 0),
        assists=_number(
            player.get("assist") if "assist" in player else player.get("assists"),
            0,
        ),
        deaths=_number(deaths, 0),
        kd=_number(kd),
        rating=_number(_rating_value(player)),
        adr=_number(player.get("adpr") if "adpr" in player else player.get("adr"), 1),
        we="-" if official else _number(player.get("we"), 1),
        pvp_score="-" if official else _number(player.get("pvpScore"), 0),
        score_change="-" if official else _signed_number(player.get("pvpScoreChange")),
        mvp=bool(player.get("mvp") or player.get("pvpMvp")),
    )


def build_match_snapshot(
    match: dict[str, Any],
    *,
    steam_id: str,
    profile: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
    group_member_steam_ids: set[str] | tuple[str, ...] | None = None,
) -> MatchSnapshot:
    profile = profile or {}
    detail = detail or {}
    base = detail.get("base") if isinstance(detail.get("base"), dict) else {}
    raw_players = detail.get("players") if isinstance(detail.get("players"), list) else []
    official = _is_official_match(match, base)
    members = {str(value) for value in (group_member_steam_ids or ())}
    members.add(steam_id)

    players = tuple(
        sorted(
            (
                _player_snapshot(
                    player,
                    group_member_steam_ids=members,
                    official=official,
                )
                for player in raw_players
                if isinstance(player, dict)
            ),
            key=lambda player: (
                player.team if player.team in {1, 2} else 99,
                -(_float(player.rating) or -1),
                player.name.casefold(),
            ),
        )
    )
    tracked_player = next((player for player in players if player.steam_id == steam_id), None)

    team = tracked_player.team if tracked_player is not None else _integer(match.get("team"))
    win_team = _integer(base.get("winTeam"))
    if win_team is None:
        win_team = _integer(match.get("winTeam"))
    score1 = _integer(base.get("score1"))
    score2 = _integer(base.get("score2"))
    if score1 is None:
        score1 = _integer(match.get("score1"))
    if score2 is None:
        score2 = _integer(match.get("score2"))

    if team == 1:
        own_score, opponent_score = score1, score2
    elif team == 2:
        own_score, opponent_score = score2, score1
    else:
        own_score, opponent_score = score1, score2

    if team in {1, 2} and win_team in {1, 2}:
        if team == win_team:
            result: MatchResult = "win"
            result_label = "胜利"
        else:
            result = "loss"
            result_label = "失败"
    elif own_score is not None and own_score == opponent_score:
        result = "draw"
        result_label = "平局"
    else:
        result = "finished"
        result_label = "比赛结束"

    kills = _integer(match.get("kill"))
    deaths = _integer(match.get("death"))
    kd = kills / deaths if kills is not None and deaths else None
    rating = _rating_value(match)
    duration = _number(base.get("duration", match.get("duration")), 0)
    if duration != "-":
        duration += " 分钟"

    tracked_name = (
        tracked_player.name
        if tracked_player is not None
        else game_profile(match, match_row=True)["name"] or game_name(profile, steam_id)
    )
    tracked_avatar = (
        tracked_player.avatar_url
        if tracked_player is not None
        else _text(profile.get("avatar") or match.get("steamAvatar"), "")
    )
    return MatchSnapshot(
        match_id=_text(base.get("matchId") or match.get("matchId"), "未知比赛"),
        steam_id=steam_id,
        name=tracked_name,
        avatar_url=tracked_avatar,
        map_name=_text(base.get("map") or match.get("mapName"), "未知地图"),
        mode=_text(
            base.get("mode") or base.get("mode2") or match.get("mode"),
            "CS2 官匹" if official else "完美平台比赛",
        ),
        result=result,
        result_label=result_label,
        own_score=_number(own_score, 0),
        opponent_score=_number(opponent_score, 0),
        team1_score=_number(score1, 0),
        team2_score=_number(score2, 0),
        team1_name=_text(base.get("team1Name"), "队伍 A"),
        team2_name=_text(base.get("team2Name"), "队伍 B"),
        kills=tracked_player.kills if tracked_player is not None else _number(kills, 0),
        assists=(
            tracked_player.assists
            if tracked_player is not None
            else _number(match.get("assist"), 0)
        ),
        deaths=tracked_player.deaths if tracked_player is not None else _number(deaths, 0),
        kd=tracked_player.kd if tracked_player is not None else _number(kd),
        rating=tracked_player.rating if tracked_player is not None else _number(rating),
        we=(
            tracked_player.we
            if tracked_player is not None
            else ("-" if official else _number(match.get("we")))
        ),
        pvp_score=(
            tracked_player.pvp_score
            if tracked_player is not None
            else ("-" if official else _number(match.get("pvpScore"), 0))
        ),
        score_change=(
            tracked_player.score_change
            if tracked_player is not None
            else ("-" if official else _signed_number(match.get("pvpScoreChange")))
        ),
        duration=duration,
        ended_at=_text(base.get("endTime") or match.get("endTime"), "未知时间"),
        k4=_number(match.get("k4"), 0),
        k5=_number(match.get("k5"), 0),
        mvp=bool(match.get("mvp") or match.get("pvpMvp")),
        players=players,
        source="official" if official else "pvp",
    )


def build_match_history(
    matches: list[dict[str, Any]],
    *,
    steam_id: str,
    profile: dict[str, Any] | None = None,
    limit: int = 10,
    source: MatchHistorySource | None = None,
) -> MatchHistorySnapshot:
    profile = profile or {}
    seen: set[str] = set()
    valid: list[dict[str, Any]] = []
    for raw_match in sorted(matches, key=match_sort_key, reverse=True):
        match_id = _text(raw_match.get("matchId"), "")
        if not match_id or match_id in seen:
            continue
        seen.add(match_id)
        valid.append(raw_match)
        if len(valid) >= limit:
            break

    snapshots = tuple(
        build_match_snapshot(
            raw_match,
            steam_id=steam_id,
            profile=profile,
            group_member_steam_ids={steam_id},
        )
        for raw_match in valid
    )
    if source is None:
        official_count = sum(_is_official_match(raw_match) for raw_match in valid)
        if valid and official_count == len(valid):
            source = "official"
        elif official_count == 0:
            source = "pvp"
        else:
            source = "mixed"
    wins = sum(match.result == "win" for match in snapshots)
    losses = sum(match.result == "loss" for match in snapshots)
    draws = sum(match.result == "draw" for match in snapshots)
    ratings = [
        value
        for raw_match in valid
        if (value := _rating_value(raw_match)) is not None
    ]
    score_changes = [
        value
        for raw_match in valid
        if not _is_official_match(raw_match)
        and (value := _float(raw_match.get("pvpScoreChange"))) is not None
    ]
    total = len(snapshots)
    decided = wins + losses + draws
    name = (game_profile(valid[0], match_row=True)["name"] if valid else "") or game_name(profile)
    avatar_url = _text(profile.get("avatar"), "")
    if snapshots:
        name = name or snapshots[0].name
        avatar_url = avatar_url or snapshots[0].avatar_url
    return MatchHistorySnapshot(
        steam_id=steam_id,
        name=name or steam_id,
        avatar_url=avatar_url,
        total=total,
        wins=wins,
        losses=losses,
        draws=draws,
        win_rate=_number(wins / decided * 100, 1) + "%" if decided else "-",
        avg_rating=_number(sum(ratings) / len(ratings)) if ratings else "-",
        elo_change=_signed_number(sum(score_changes)) if score_changes else "-",
        matches=snapshots,
        source=source,
    )


def format_match_snapshot(match: MatchSnapshot) -> str:
    if match.players:
        lines = [
            f"CS2 新比赛｜{match.map_name}｜{match.team1_score}:{match.team2_score}",
            f"{match.mode}｜{match.ended_at}",
        ]
        for team, label in ((1, match.team1_name), (2, match.team2_name)):
            lines.append(f"{label}（队伍 {team}）")
            for player in (item for item in match.players if item.team == team):
                marker = "★" if player.is_group_member else "·"
                lines.append(
                    f"{marker} {player.name}  "
                    f"{player.kills}/{player.assists}/{player.deaths}  "
                    f"Rating {player.rating}  ADR {player.adr}"
                )
        return "\n".join(lines)

    lines = [
        f"CS2 新比赛｜{match.name}｜{match.result_label}",
        f"{match.map_name}　{match.own_score}:{match.opponent_score}　{match.mode}",
        f"K/A/D：{match.kills}/{match.assists}/{match.deaths}　K/D：{match.kd}",
        f"Rating：{match.rating}",
    ]
    if match.source == "pvp":
        lines.extend(
            [
                f"WE：{match.we}　完美分：{match.pvp_score}（{match.score_change}）",
            ]
        )
    lines.extend([f"用时：{match.duration}", f"结束时间：{match.ended_at}"])
    return "\n".join(lines)


def format_match_history(history: MatchHistorySnapshot) -> str:
    source_name = {
        "pvp": "完美",
        "official": "官匹",
        "mixed": "综合",
    }[history.source]
    lines = [
        f"CS2 {source_name}近十场战绩｜{history.name}",
        f"{history.total} 场 {history.wins}胜{history.losses}负{history.draws}平　"
        f"胜率 {history.win_rate}　平均 Rating {history.avg_rating}"
        + (f"　ELO {history.elo_change}" if history.source != "official" else ""),
    ]
    if not history.matches:
        empty_source = "官匹" if history.source == "official" else "完美平台"
        lines.append(f"暂无可用的{empty_source}比赛记录")
        return "\n".join(lines)
    for index, match in enumerate(history.matches, start=1):
        source_metric = (
            f"Rating {match.rating}"
            if history.source == "official"
            else f"Rating {match.rating}｜ELO {match.score_change}"
        )
        lines.append(
            f"{index}. {match.result_label}｜{match.map_name} "
            f"{match.own_score}:{match.opponent_score}｜"
            f"{match.kills}/{match.assists}/{match.deaths}｜"
            f"{source_metric}｜{match.ended_at}"
        )
    return "\n".join(lines)
