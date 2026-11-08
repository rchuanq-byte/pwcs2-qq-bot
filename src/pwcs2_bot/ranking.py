"""Normalize and format group leaderboards from Perfect World detailStats/v2."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pwcs2_bot.game_identity import game_name, game_profile, match_sort_key
from pwcs2_bot.query_source import QuerySource

RankingMetric = Literal[
    "elo",
    "rapid_stop",
    "rating",
    "win_rate",
    "adr",
    "headshot",
    "kd",
    "matches",
]

_METRIC_LABELS: dict[RankingMetric, str] = {
    "elo": "ELO",
    "rapid_stop": "急停成功率",
    "rating": "Rating",
    "win_rate": "胜率",
    "adr": "ADR",
    "headshot": "爆头率",
    "kd": "K/D",
    "matches": "场次",
}

_METRIC_ALIASES: dict[str, RankingMetric] = {
    "": "elo",
    "elo": "elo",
    "分数": "elo",
    "完美分": "elo",
    "急停": "rapid_stop",
    "急停率": "rapid_stop",
    "急停成功率": "rapid_stop",
    "rapidstop": "rapid_stop",
    "rating": "rating",
    "rat": "rating",
    "胜率": "win_rate",
    "winrate": "win_rate",
    "adr": "adr",
    "爆头": "headshot",
    "爆头率": "headshot",
    "hs": "headshot",
    "kd": "kd",
    "k/d": "kd",
    "场次": "matches",
    "比赛": "matches",
    "matches": "matches",
}


@dataclass(frozen=True, slots=True)
class RankingEntry:
    qq_id: str
    steam_id: str
    name: str
    season: str
    matches: float | None
    elo: float | None
    rating: float | None
    win_rate: float | None
    adr: float | None
    kd: float | None
    rapid_stop: float | None
    headshot: float | None
    school_rank: int | None


@dataclass(frozen=True, slots=True)
class RankingSnapshot:
    metric: RankingMetric
    metric_label: str
    entries: tuple[RankingEntry, ...]
    total_count: int
    failed_count: int
    source: QuerySource = "pvp"


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


def _percent_number(value: Any) -> float | None:
    number = _float(value)
    if number is None:
        return None
    if abs(number) <= 1:
        number *= 100
    return number


def _nested(data: dict[str, Any], *keys: str) -> Any:
    value: Any = data
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def parse_ranking_metric(
    argument: str,
    *,
    source: QuerySource = "pvp",
) -> RankingMetric:
    normalized = "".join(argument.casefold().split())
    metric = (
        ("rating" if source == "official" else "elo")
        if not normalized
        else _METRIC_ALIASES.get(normalized)
    )
    if metric is None:
        raise ValueError("排行类型支持：ELO、急停、Rating、胜率、ADR、爆头、K/D、场次")
    if source == "official" and metric not in {"rating", "win_rate", "kd", "matches"}:
        raise ValueError("官匹排行仅支持：Rating、胜率、K/D、场次")
    return metric


def build_ranking_entry(
    data: dict[str, Any],
    *,
    qq_id: str,
    steam_id: str,
) -> RankingEntry:
    rank_info = data.get("perfectRankInfo")
    school_rank = _integer(rank_info.get("rank")) if isinstance(rank_info, dict) else None
    kills = _float(data.get("kills"))
    deaths = _float(data.get("deaths"))
    kd = _float(data.get("kd"))
    if kd is None and kills is not None and deaths:
        kd = kills / deaths
    return RankingEntry(
        qq_id=qq_id,
        steam_id=_text(data.get("steamId"), steam_id),
        name=game_name(data, steam_id),
        season=_text(data.get("seasonId")),
        matches=_float(data.get("cnt")),
        elo=_float(data.get("pvpScore")),
        rating=_float(data.get("pwRating")),
        win_rate=_percent_number(data.get("winRate")),
        adr=_float(data.get("adr")),
        kd=kd,
        rapid_stop=_percent_number(
            _nested(
                data,
                "radar",
                "marksmanship",
                "detail",
                "rapid_stop_rate_raw",
            )
        ),
        headshot=_percent_number(data.get("headShotRatio")),
        school_rank=school_rank,
    )


def build_official_ranking_entry(
    matches: list[dict[str, Any]],
    *,
    qq_id: str,
    steam_id: str,
    name: str = "",
    limit: int = 50,
) -> RankingEntry | None:
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
    if not valid:
        return None

    wins = losses = draws = 0
    ratings: list[float] = []
    kills = deaths = 0
    for match in valid:
        team = _integer(match.get("team"))
        win_team = _integer(match.get("winTeam"))
        score1 = _integer(match.get("score1"))
        score2 = _integer(match.get("score2"))
        if score1 is not None and score1 == score2:
            draws += 1
        elif team in {1, 2} and win_team in {1, 2}:
            if team == win_team:
                wins += 1
            else:
                losses += 1
        elif (
            team in {1, 2}
            and score1 is not None
            and score2 is not None
            and score1 != score2
        ):
            own_score, opponent_score = (
                (score1, score2) if team == 1 else (score2, score1)
            )
            if own_score > opponent_score:
                wins += 1
            else:
                losses += 1
        pw_rating = _float(match.get("pwRating"))
        official_rating = _float(match.get("rating"))
        if pw_rating is not None and pw_rating > 0:
            ratings.append(pw_rating)
        elif official_rating is not None and official_rating > 0:
            ratings.append(official_rating)
        kills += _integer(match.get("kill")) or 0
        deaths += _integer(match.get("death")) or 0

    decided = wins + losses + draws
    return RankingEntry(
        qq_id=qq_id,
        steam_id=steam_id,
        name=game_profile(valid[0], match_row=True)["name"] or name or steam_id,
        season=f"最近 {len(valid)} 场官匹",
        matches=float(len(valid)),
        elo=None,
        rating=sum(ratings) / len(ratings) if ratings else None,
        win_rate=wins / decided * 100 if decided else None,
        adr=None,
        kd=kills / deaths if deaths else None,
        rapid_stop=None,
        headshot=None,
        school_rank=None,
    )


def build_ranking_snapshot(
    entries: list[RankingEntry],
    *,
    metric: RankingMetric,
    total_count: int,
    failed_count: int = 0,
    source: QuerySource = "pvp",
) -> RankingSnapshot:
    def sort_key(entry: RankingEntry) -> tuple[bool, float, float, str]:
        value = getattr(entry, metric)
        return (
            value is None,
            -(value if value is not None else 0),
            -(
                entry.elo
                if entry.elo is not None
                else entry.rating
                if entry.rating is not None
                else 0
            ),
            entry.name.casefold(),
        )

    return RankingSnapshot(
        metric=metric,
        metric_label=_METRIC_LABELS[metric],
        entries=tuple(sorted(entries, key=sort_key)),
        total_count=total_count,
        failed_count=failed_count,
        source=source,
    )


def _number(value: float | None, *, digits: int = 2) -> str:
    if value is None:
        return "-"
    if value.is_integer():
        return str(int(value))
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}%"


def format_metric(entry: RankingEntry, metric: RankingMetric) -> str:
    value = getattr(entry, metric)
    if metric in {"win_rate", "rapid_stop", "headshot"}:
        return _percent(value)
    return _number(value)


def format_ranking_snapshot(ranking: RankingSnapshot) -> str:
    source_name = "官匹" if ranking.source == "official" else "完美"
    lines = [f"CS2 群内{source_name}{ranking.metric_label}排行榜"]
    for index, entry in enumerate(ranking.entries[:20], start=1):
        if ranking.source == "official":
            lines.append(
                f"{index}. {entry.name}｜{ranking.metric_label} "
                f"{format_metric(entry, ranking.metric)}｜"
                f"Rating {_number(entry.rating)}｜K/D {_number(entry.kd)}｜"
                f"{_number(entry.matches, digits=0)} 场"
            )
        else:
            lines.append(
                f"{index}. {entry.name}｜{ranking.metric_label} "
                f"{format_metric(entry, ranking.metric)}｜"
                f"ELO {_number(entry.elo, digits=0)}｜急停 {_percent(entry.rapid_stop)}"
            )
    if len(ranking.entries) > 20:
        lines.append(f"仅显示前 20 名，共 {len(ranking.entries)} 名有效成员")
    if ranking.failed_count:
        lines.append(f"另有 {ranking.failed_count} 名成员查询失败，未计入排行")
    return "\n".join(lines)
