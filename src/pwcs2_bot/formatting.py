"""Normalize and format Perfect World statistics for QQ replies."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal

from pwcs2_bot.game_identity import game_name, game_profile
from pwcs2_bot.match_formatting import match_sort_key


@dataclass(frozen=True, slots=True)
class MapSnapshot:
    name: str
    matches: str
    win_rate: str
    win_percent: float | None


@dataclass(frozen=True, slots=True)
class WeaponSnapshot:
    name: str
    kills: str


@dataclass(frozen=True, slots=True)
class StatsSnapshot:
    name: str
    avatar_url: str
    steam_id: str
    season: str
    score: str
    rating: str
    matches: str
    win_rate: str
    win_percent: float | None
    kd: str
    kills: str
    assists: str
    deaths: str
    adr: str
    headshot_rate: str
    headshot_percent: float | None
    rws: str
    mvp: str
    maps: tuple[MapSnapshot, ...]
    weapons: tuple[WeaponSnapshot, ...]
    summary: str
    source: Literal["pvp", "official"] = "pvp"


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


def _number(value: Any, digits: int = 2, fallback: str = "-") -> str:
    number = _float(value)
    if number is None:
        return fallback
    if number.is_integer():
        return str(int(number))
    return f"{number:.{digits}f}".rstrip("0").rstrip(".")


def _percent_number(value: Any) -> float | None:
    number = _float(value)
    if number is None:
        return None
    if abs(number) <= 1:
        number *= 100
    return number


def _percent(value: Any) -> str:
    number = _percent_number(value)
    return "-" if number is None else f"{number:.1f}%"


def build_stats_snapshot(data: dict[str, Any]) -> StatsSnapshot:
    kills_value = _float(data.get("kills"))
    deaths_value = _float(data.get("deaths"))
    kd_value = data.get("kd")
    if kd_value is None and kills_value is not None and deaths_value:
        kd_value = kills_value / deaths_value

    rating_value = data.get("pwRating")
    if rating_value is None:
        rating_value = data.get("rating")

    maps: list[MapSnapshot] = []
    raw_maps = data.get("hotMaps")
    if isinstance(raw_maps, list):
        for item in raw_maps[:3]:
            if not isinstance(item, dict):
                continue
            matches_value = item.get("totalMatch")
            wins_value = _float(item.get("winCount"))
            matches_number = _float(matches_value)
            map_win_ratio = None
            if wins_value is not None and matches_number:
                map_win_ratio = wins_value / matches_number
            maps.append(
                MapSnapshot(
                    name=_text(item.get("mapName") or item.get("map"), "未知地图"),
                    matches=_number(matches_value, 0),
                    win_rate=_percent(map_win_ratio),
                    win_percent=_percent_number(map_win_ratio),
                )
            )

    weapons: list[WeaponSnapshot] = []
    raw_weapons = data.get("hotWeapons")
    if isinstance(raw_weapons, list):
        for item in raw_weapons[:3]:
            if not isinstance(item, dict) or not item.get("weaponName"):
                continue
            weapons.append(
                WeaponSnapshot(
                    name=_text(item.get("weaponName")),
                    kills=_number(item.get("weaponKill"), 0),
                )
            )

    return StatsSnapshot(
        name=game_name(data, _text(data.get("steamId"), "未知玩家")),
        avatar_url=_text(data.get("avatar"), ""),
        steam_id=_text(data.get("steamId")),
        season=_text(data.get("seasonId")),
        score=_number(data.get("pvpScore"), 0),
        rating=_number(rating_value),
        matches=_number(data.get("cnt"), 0),
        win_rate=_percent(data.get("winRate")),
        win_percent=_percent_number(data.get("winRate")),
        kd=_number(kd_value),
        kills=_number(data.get("kills"), 0),
        assists=_number(data.get("assists"), 0),
        deaths=_number(data.get("deaths"), 0),
        adr=_number(data.get("adr")),
        headshot_rate=_percent(data.get("headShotRatio")),
        headshot_percent=_percent_number(data.get("headShotRatio")),
        rws=_number(data.get("rws")),
        mvp=_number(data.get("mvpCount"), 0),
        maps=tuple(maps),
        weapons=tuple(weapons),
        summary=_text(data.get("summary"), ""),
        source="pvp",
    )


def build_official_stats_snapshot(
    matches: list[dict[str, Any]],
    *,
    steam_id: str,
    profile: dict[str, Any] | None = None,
    limit: int = 50,
) -> StatsSnapshot:
    """Aggregate a player's recent official matches without inventing PVP metrics."""
    profile = profile or {}
    seen: set[str] = set()
    valid: list[dict[str, Any]] = []
    for match in sorted(matches, key=match_sort_key, reverse=True):
        match_id = _text(match.get("matchId"), "")
        if not match_id or match_id in seen:
            continue
        seen.add(match_id)
        valid.append(match)
        if len(valid) >= limit:
            break

    wins = losses = draws = 0
    ratings: list[float] = []
    kills = assists = deaths = 0
    map_matches: Counter[str] = Counter()
    map_wins: Counter[str] = Counter()
    map_decided: Counter[str] = Counter()
    for match in valid:
        team = _float(match.get("team"))
        win_team = _float(match.get("winTeam"))
        score1 = _float(match.get("score1"))
        score2 = _float(match.get("score2"))
        result = "finished"
        if score1 is not None and score1 == score2:
            result = "draw"
            draws += 1
        elif team in {1.0, 2.0} and win_team in {1.0, 2.0}:
            if team == win_team:
                result = "win"
                wins += 1
            else:
                result = "loss"
                losses += 1
        elif (
            team in {1.0, 2.0}
            and score1 is not None
            and score2 is not None
            and score1 != score2
        ):
            own_score, opponent_score = (
                (score1, score2) if team == 1.0 else (score2, score1)
            )
            if own_score > opponent_score:
                result = "win"
                wins += 1
            else:
                result = "loss"
                losses += 1
        rating = _float(match.get("pwRating"))
        official_rating = _float(match.get("rating"))
        if rating is not None and rating > 0:
            ratings.append(rating)
        elif official_rating is not None and official_rating > 0:
            ratings.append(official_rating)
        kills += int(_float(match.get("kill")) or 0)
        assists += int(_float(match.get("assist")) or 0)
        deaths += int(_float(match.get("death")) or 0)
        map_name = _text(match.get("mapName") or match.get("map"), "未知地图")
        map_matches[map_name] += 1
        if result in {"win", "loss", "draw"}:
            map_decided[map_name] += 1
        if result == "win":
            map_wins[map_name] += 1

    maps = tuple(
        MapSnapshot(
            name=map_name,
            matches=_number(count, 0),
            win_rate=_percent(
                map_wins[map_name] / map_decided[map_name]
                if map_decided[map_name]
                else None
            ),
            win_percent=_percent_number(
                map_wins[map_name] / map_decided[map_name]
                if map_decided[map_name]
                else None
            ),
        )
        for map_name, count in sorted(
            map_matches.items(),
            key=lambda item: (-item[1], item[0]),
        )[:3]
    )
    total = len(valid)
    first = valid[0] if valid else {}
    played = wins + losses + draws
    win_ratio = wins / played if played else None
    name = game_profile(first, match_row=True)["name"] or game_name(profile, steam_id)
    average_rating = sum(ratings) / len(ratings) if ratings else None
    kd = kills / deaths if deaths else None
    return StatsSnapshot(
        name=name,
        avatar_url=_text(
            profile.get("avatar") or first.get("steamAvatar"),
            "",
        ),
        steam_id=steam_id,
        season=f"最近 {total} 场官匹" if total else "暂无官匹记录",
        score=f"{wins}-{losses}-{draws}" if played else "-",
        rating=_number(average_rating),
        matches=_number(total, 0),
        win_rate=_percent(win_ratio),
        win_percent=_percent_number(win_ratio),
        kd=_number(kd),
        kills=_number(kills, 0),
        assists=_number(assists, 0),
        deaths=_number(deaths, 0),
        adr="-",
        headshot_rate="-",
        headshot_percent=None,
        rws="-",
        mvp="-",
        maps=maps,
        weapons=(),
        summary=(
            (
                f"最近 {total} 场官匹：{wins} 胜 {losses} 负 {draws} 平；"
                "官匹列表不提供 ADR、爆头率、RWS 和武器汇总。"
            )
            if played
            else (
                f"查询到最近 {total} 场官匹，但列表缺少可确认的胜负信息；"
                "官匹列表不提供 ADR、爆头率、RWS 和武器汇总。"
                if total
                else "暂未查询到最近官匹记录。"
            )
        ),
        source="official",
    )


def format_stats_snapshot(stats: StatsSnapshot) -> str:
    if stats.source == "official":
        lines = [
            f"CS2 官匹数据｜{stats.name}",
            f"SteamID：{stats.steam_id}",
            f"范围：{stats.season}　胜-负-平：{stats.score}　"
            f"平均 Rating：{stats.rating}",
            f"场次：{stats.matches}　胜率：{stats.win_rate}　K/D：{stats.kd}",
            f"K/A/D：{stats.kills}/{stats.assists}/{stats.deaths}",
        ]
        if stats.maps:
            labels = [
                f"{item.name} {item.matches}场/{item.win_rate}"
                for item in stats.maps
            ]
            lines.append("常用地图：" + "；".join(labels))
        if stats.summary:
            lines.append(f"说明：{stats.summary}")
        return "\n".join(lines)

    lines = [
        f"CS2 完美数据｜{stats.name}",
        f"SteamID：{stats.steam_id}",
        f"赛季：{stats.season}　分数：{stats.score}　Rating：{stats.rating}",
        f"场次：{stats.matches}　胜率：{stats.win_rate}　K/D：{stats.kd}",
        f"K/A/D：{stats.kills}/{stats.assists}/{stats.deaths}　ADR：{stats.adr}",
        f"爆头率：{stats.headshot_rate}　RWS：{stats.rws}　MVP：{stats.mvp}",
    ]
    if stats.maps:
        labels = []
        for item in stats.maps:
            label = f"{item.name} {item.matches}场"
            if item.win_rate != "-":
                label += f"/{item.win_rate}"
            labels.append(label)
        lines.append("常用地图：" + "；".join(labels))
    if stats.weapons:
        labels = [f"{item.name} {item.kills}杀" for item in stats.weapons]
        lines.append("常用武器：" + "；".join(labels))
    if stats.summary:
        lines.append(f"评价：{stats.summary}")
    return "\n".join(lines)


def format_stats(data: dict[str, Any]) -> str:
    return format_stats_snapshot(build_stats_snapshot(data))
