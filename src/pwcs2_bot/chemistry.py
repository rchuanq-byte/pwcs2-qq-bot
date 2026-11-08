"""Calculate playful pair chemistry from two players' recent matches."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Any

from pwcs2_bot.query_source import QuerySource


@dataclass(frozen=True, slots=True)
class ChemistrySnapshot:
    first_qq: str
    second_qq: str
    first_name: str
    second_name: str
    shared_matches: int
    same_team_matches: int
    same_team_wins: int
    same_team_win_rate: str
    versus_matches: int
    first_versus_wins: int
    second_versus_wins: int
    first_avg_rating: str
    second_avg_rating: str
    title: str
    source: QuerySource = "pvp"


@dataclass(frozen=True, slots=True)
class ChemistryNetworkSnapshot:
    week_start: str
    member_count: int
    relationship_count: int
    pairs: tuple[ChemistrySnapshot, ...]


def build_chemistry_network(
    matches_by_qq: dict[str, list[dict[str, Any]]],
    *,
    names_by_qq: dict[str, str],
    week_start: str,
    max_pairs: int = 12,
) -> ChemistryNetworkSnapshot:
    pairs: list[ChemistrySnapshot] = []
    for first_qq, second_qq in combinations(sorted(matches_by_qq), 2):
        snapshot = build_chemistry_snapshot(
            matches_by_qq[first_qq],
            matches_by_qq[second_qq],
            first_qq=first_qq,
            second_qq=second_qq,
            first_name=names_by_qq.get(first_qq) or f"QQ {first_qq}",
            second_name=names_by_qq.get(second_qq) or f"QQ {second_qq}",
        )
        if snapshot.shared_matches:
            pairs.append(snapshot)
    pairs.sort(
        key=lambda pair: (
            -pair.same_team_matches,
            -pair.same_team_wins,
            -pair.shared_matches,
            pair.first_name.casefold(),
            pair.second_name.casefold(),
        ),
    )
    return ChemistryNetworkSnapshot(
        week_start=week_start,
        member_count=len(matches_by_qq),
        relationship_count=len(pairs),
        pairs=tuple(pairs[:max_pairs]),
    )


def format_chemistry_network(snapshot: ChemistryNetworkSnapshot) -> str:
    lines = [
        f"🕸 牛牛关系网｜{snapshot.week_start} 当周",
        f"统计成员：{snapshot.member_count} 人｜共同比赛组合：{snapshot.relationship_count} 组",
    ]
    if not snapshot.pairs:
        lines.append("最近 50 场暂未发现群友共同比赛。")
        return "\n".join(lines)
    for index, pair in enumerate(snapshot.pairs[:8], start=1):
        lines.append(
            f"{index}. {pair.first_name} × {pair.second_name}｜{pair.title}｜"
            f"同队 {pair.same_team_matches} 场，胜率 {pair.same_team_win_rate}"
        )
    lines.append("数据范围：每位已绑定成员最近 50 场完美平台比赛")
    return "\n".join(lines)


def build_chemistry_snapshot(
    first_matches: list[dict[str, Any]],
    second_matches: list[dict[str, Any]],
    *,
    first_qq: str,
    second_qq: str,
    first_name: str,
    second_name: str,
    source: QuerySource = "pvp",
) -> ChemistrySnapshot:
    first_by_id = _matches_by_id(first_matches)
    second_by_id = _matches_by_id(second_matches)
    shared_ids = sorted(set(first_by_id) & set(second_by_id))
    same_team = 0
    same_team_wins = 0
    versus = 0
    first_versus_wins = 0
    second_versus_wins = 0
    first_ratings: list[float] = []
    second_ratings: list[float] = []

    for match_id in shared_ids:
        first = first_by_id[match_id]
        second = second_by_id[match_id]
        first_team = _integer(first.get("team"))
        second_team = _integer(second.get("team"))
        win_team = _integer(first.get("winTeam"))
        if win_team is None:
            win_team = _integer(second.get("winTeam"))
        if first_team in {1, 2} and first_team == second_team:
            same_team += 1
            if win_team == first_team:
                same_team_wins += 1
        elif first_team in {1, 2} and second_team in {1, 2}:
            versus += 1
            if win_team == first_team:
                first_versus_wins += 1
            elif win_team == second_team:
                second_versus_wins += 1
        if (rating := _rating(first)) is not None:
            first_ratings.append(rating)
        if (rating := _rating(second)) is not None:
            second_ratings.append(rating)

    win_rate_value = same_team_wins / same_team if same_team else None
    first_average = _average(first_ratings)
    second_average = _average(second_ratings)
    return ChemistrySnapshot(
        first_qq=first_qq,
        second_qq=second_qq,
        first_name=first_name,
        second_name=second_name,
        shared_matches=len(shared_ids),
        same_team_matches=same_team,
        same_team_wins=same_team_wins,
        same_team_win_rate=(
            f"{win_rate_value * 100:.1f}%" if win_rate_value is not None else "-"
        ),
        versus_matches=versus,
        first_versus_wins=first_versus_wins,
        second_versus_wins=second_versus_wins,
        first_avg_rating=_format_rating(first_average),
        second_avg_rating=_format_rating(second_average),
        title=_chemistry_title(
            shared=len(shared_ids),
            same_team=same_team,
            win_rate=win_rate_value,
            versus=versus,
            first_average=first_average,
            second_average=second_average,
        ),
        source=source,
    )


def format_chemistry_snapshot(snapshot: ChemistrySnapshot) -> str:
    source_name = "官匹" if snapshot.source == "official" else "完美平台"
    if snapshot.shared_matches == 0:
        return (
            f"🤝 {source_name}群友羁绊｜"
            f"{snapshot.first_name} × {snapshot.second_name}\n"
            "最近 50 场没有共同比赛｜称号：平行宇宙\n"
            "一起开几把后再来查吧。"
        )
    return "\n".join(
        [
            f"🤝 {source_name}群友羁绊｜"
            f"{snapshot.first_name} × {snapshot.second_name}",
            f"共同比赛：{snapshot.shared_matches} 场｜称号：{snapshot.title}",
            f"同队：{snapshot.same_team_matches} 场 {snapshot.same_team_wins} 胜｜"
            f"胜率 {snapshot.same_team_win_rate}",
            f"对手局：{snapshot.versus_matches} 场｜交手胜场 "
            f"{snapshot.first_name} {snapshot.first_versus_wins}:"
            f"{snapshot.second_versus_wins} {snapshot.second_name}",
            f"共同局平均 Rating：{snapshot.first_name} {snapshot.first_avg_rating}｜"
            f"{snapshot.second_name} {snapshot.second_avg_rating}",
            f"数据范围：双方最近 50 场{source_name}比赛",
        ]
    )


def _matches_by_id(matches: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for match in matches:
        match_id = str(match.get("matchId") or "").strip()
        if match_id and match_id not in result:
            result[match_id] = match
    return result


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


def _rating(match: dict[str, Any]) -> float | None:
    pw_rating = _float(match.get("pwRating"))
    rating = _float(match.get("rating"))
    if pw_rating is not None and pw_rating > 0:
        return pw_rating
    if rating is not None and rating > 0:
        return rating
    return None


def _average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _format_rating(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _chemistry_title(
    *,
    shared: int,
    same_team: int,
    win_rate: float | None,
    versus: int,
    first_average: float | None,
    second_average: float | None,
) -> str:
    if shared == 0:
        return "平行宇宙"
    if (
        versus >= 3
        and first_average is not None
        and second_average is not None
        and abs(first_average - second_average) < 0.08
    ):
        return "相爱相杀"
    if same_team >= 5 and win_rate is not None and win_rate >= 0.65:
        return "天选双排"
    if same_team >= 3 and win_rate is not None and win_rate <= 0.35:
        return "连败兄弟"
    if (
        same_team >= 3
        and first_average is not None
        and second_average is not None
        and abs(first_average - second_average) >= 0.2
    ):
        return "带飞与躺赢"
    if same_team >= 3:
        return "默契队友"
    if versus:
        return "宿命对手"
    return "刚刚认识"
