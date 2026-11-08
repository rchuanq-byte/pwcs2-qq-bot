"""Parse explicit Perfect World and official-match query selectors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

QuerySource = Literal["pvp", "official"]

_SOURCE_ALIASES: dict[str, QuerySource] = {
    "gp": "official",
    "官匹": "official",
    "official": "official",
    "mm": "official",
    "pw": "pvp",
    "完美": "pvp",
    "pvp": "pvp",
}


@dataclass(frozen=True, slots=True)
class SourceArgument:
    source: QuerySource
    argument: str


def parse_source_argument(
    argument: str,
    *,
    default: QuerySource = "pvp",
) -> SourceArgument:
    """Remove standalone source selectors while preserving the target argument."""
    source: QuerySource | None = None
    remaining: list[str] = []
    for token in argument.split():
        selected = _SOURCE_ALIASES.get(token.casefold())
        if selected is None:
            remaining.append(token)
            continue
        if source is not None and source != selected:
            raise ValueError("不能同时指定完美和官匹数据源")
        source = selected
    return SourceArgument(source or default, " ".join(remaining))
