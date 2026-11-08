"""Game/API identities only. QQ cards and member-memory names are never fallbacks."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from pwcs2_bot.perfectworld import PerfectWorldApiError
from pwcs2_bot.steam_presence import SteamPresenceError


def game_name(data: dict[str, Any], fallback: str = "") -> str:
    for key in ("steamName", "steamNick", "nickName", "nickname", "name", "personaname"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return fallback


def game_profile(data: dict[str, Any], *, match_row: bool = False) -> dict[str, str]:
    # A generic match-row "name" may be a map/match label, not a player identity.
    names = {k: v for k, v in data.items() if k != "name"} if match_row else data
    profile = {"name": game_name(names)}
    for key in ("steamAvatar", "avatar", "avatarUrl"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            profile["avatar"] = value.strip()
            break
    return profile


def match_sort_key(match: dict[str, Any]) -> tuple[int, str]:
    try:
        raw_timestamp = match.get("timeStamp")
        timestamp = 0 if isinstance(raw_timestamp, bool) else int(float(raw_timestamp or 0))
    except (ValueError, TypeError, OverflowError):
        timestamp = 0
    if timestamp <= 0:
        end_time = str(match.get("endTime") or "").strip()
        try:
            timestamp = int(datetime.strptime(end_time, "%Y-%m-%d %H:%M:%S").timestamp())
        except ValueError:
            timestamp = 0
    return timestamp, str(match.get("matchId") or "").strip()


async def resolve_game_profile(
    api: Any,
    steam_id: str,
    matches: list[dict[str, Any]],
    *,
    source: str = "pvp",
    steam_presence: Any = None,
) -> dict[str, str]:
    """Enrich names without mixing match stats or preventing a card on lookup failure.

    Prefer the newest game's list/detail identity. A missing detail player never
    borrows a teammate's name. Reuse existing API caches/locks and bound optional
    enrichment to eight seconds; no QQ database or second network client is used.
    """
    ordered = sorted(matches, key=match_sort_key, reverse=True)
    if ordered:
        profile = game_profile(ordered[0], match_row=True)
        if profile["name"]:
            return profile
    try:
        async with asyncio.timeout(8):
            detail_query = getattr(api, "match_detail", None)
            if ordered and ordered[0].get("matchId") and callable(detail_query):
                try:
                    detail = await asyncio.wait_for(
                        detail_query(
                            str(ordered[0]["matchId"]),
                            data_source=1 if source == "official" else 3,
                        ),
                        timeout=3,
                    )
                    rows = detail.get("players") if isinstance(detail, dict) else []
                    for row in rows or []:
                        if not isinstance(row, dict):
                            continue
                        if str(row.get("playerId") or row.get("steamId") or "") != steam_id:
                            continue
                        profile = game_profile(row)
                        if profile["name"]:
                            return profile
                except (PerfectWorldApiError, ValueError, TimeoutError):
                    pass
            # Current Perfect World profile names are game data, not QQ memory.
            profile_query = getattr(api, "detail_stats", None)
            if source != "official" and callable(profile_query):
                try:
                    data = await asyncio.wait_for(profile_query(steam_id), 3)
                    profile = game_profile(data) if isinstance(data, dict) else {}
                    if profile.get("name"):
                        return profile
                except (PerfectWorldApiError, ValueError, TimeoutError):
                    pass
            if steam_presence is not None and steam_presence.configured:
                try:
                    summaries = await steam_presence.summaries((steam_id,))
                    identity = summaries.get(steam_id)
                    if identity is not None and identity.persona_name.strip():
                        return {"name": identity.persona_name.strip()}
                except (SteamPresenceError, ValueError):
                    pass
    except TimeoutError:
        pass
    return {"name": steam_id}
