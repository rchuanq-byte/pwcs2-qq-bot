"""Read and format current game presence from the official Steam Web API."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlparse

import httpx

_RETRYABLE_HTTP_STATUS_CODES = {408, 425, 429}
_STEAM_PROXY_SCHEMES = {"http", "https"}


def _normalize_proxy_url(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith("${") and normalized.endswith("}"):
        return ""
    if not normalized:
        return ""
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in normalized
    ):
        raise ValueError("Steam presence proxy URL is invalid")
    try:
        parsed = urlparse(normalized)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Steam presence proxy URL is invalid") from exc
    if (
        parsed.scheme.casefold() not in _STEAM_PROXY_SCHEMES
        or not hostname
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Steam presence proxy URL must be an HTTP(S) URL without path, query, or fragment"
        )
    return normalized


class SteamPresenceError(RuntimeError):
    """A safe-to-log Steam presence lookup failure."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class SteamPresence:
    steam_id: str
    persona_name: str
    game_id: str
    server_ip: str
    game_name: str = ""
    persona_state: int = 0
    stale: bool = False

    @property
    def is_cs2_server(self) -> bool:
        return self.game_id == "730" and bool(self.server_ip)

    @property
    def is_playing(self) -> bool:
        return bool(self.game_id or self.game_name)

    @property
    def display_game(self) -> str:
        if self.game_name:
            return self.game_name
        if self.game_id == "730":
            return "Counter-Strike 2"
        if self.game_id:
            return f"Steam AppID {self.game_id}"
        return ""


@dataclass(frozen=True, slots=True)
class GameStatusEntry:
    """One visible row on the group Steam presence card."""

    name: str
    steam_id: str
    status_label: str
    status_kind: str
    game_name: str = ""
    detail: str = ""
    app_id: str = ""


@dataclass(frozen=True, slots=True)
class GameStatusSnapshot:
    """Renderer-friendly game-status data shared by Steam and fallback sources."""

    entries: tuple[GameStatusEntry, ...]
    total_count: int
    playing_count: int
    hidden_count: int = 0
    stale: bool = False
    degraded: bool = False
    title: str = "群内游戏状态"
    source_label: str = "STEAM OFFICIAL"
    note: str = ""


class SteamPresenceSnapshot(dict[str, SteamPresence]):
    """One batch response, including whether it came from stale cache."""

    def __init__(
        self,
        values: dict[str, SteamPresence] | None = None,
        *,
        stale: bool = False,
    ) -> None:
        super().__init__(values or {})
        self.stale = stale


class SteamPresenceClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.steampowered.com",
        timeout_seconds: float = 10,
        retry_attempts: int = 2,
        cache_ttl_seconds: float = 45,
        stale_ttl_seconds: float = 900,
        failure_cooldown_seconds: float = 15,
        max_cache_entries: int = 1000,
        proxy_url: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        normalized_key = api_key.strip()
        if normalized_key.startswith("${") and normalized_key.endswith("}"):
            normalized_key = ""
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Steam presence API base URL must use HTTPS")
        if retry_attempts < 1 or retry_attempts > 3:
            raise ValueError("Steam presence retry_attempts must be between 1 and 3")
        if cache_ttl_seconds < 0 or stale_ttl_seconds < cache_ttl_seconds:
            raise ValueError("Steam presence cache TTL values are invalid")
        if failure_cooldown_seconds < 0:
            raise ValueError("Steam presence failure cooldown must not be negative")
        if max_cache_entries < 1:
            raise ValueError("Steam presence max_cache_entries must be positive")
        self.api_key = normalized_key
        self.retry_attempts = retry_attempts
        self.cache_ttl_seconds = cache_ttl_seconds
        self.stale_ttl_seconds = stale_ttl_seconds
        self.failure_cooldown_seconds = failure_cooldown_seconds
        self.max_cache_entries = max_cache_entries
        self._clock = clock
        self._cache: dict[str, SteamPresence] = {}
        self._checked_at: dict[str, float] = {}
        self._unavailable_until = 0.0
        self._request_lock = asyncio.Lock()
        normalized_proxy_url = _normalize_proxy_url(proxy_url)
        common_client_options = {
            "base_url": base_url.rstrip("/"),
            "timeout": httpx.Timeout(timeout_seconds),
            "headers": {"User-Agent": "pwcs2-qq-bot/0.1"},
            "trust_env": False,
        }
        self._proxy_client = (
            httpx.AsyncClient(
                **common_client_options,
                proxy=normalized_proxy_url,
            )
            if normalized_proxy_url
            else None
        )
        self._direct_client = httpx.AsyncClient(
            **common_client_options,
            transport=transport,
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def close(self) -> None:
        if self._proxy_client is not None:
            await self._proxy_client.aclose()
        await self._direct_client.aclose()

    async def summaries(
        self,
        steam_ids: list[str] | tuple[str, ...],
    ) -> SteamPresenceSnapshot:
        if not self.configured:
            raise SteamPresenceError("服务器未配置 STEAM_WEB_API_KEY")
        unique_ids = tuple(dict.fromkeys(value.strip() for value in steam_ids if value.strip()))
        if not unique_ids:
            return SteamPresenceSnapshot()
        if any(not value.isdigit() for value in unique_ids):
            raise ValueError("SteamID64 必须为数字")

        self._prune_cache()
        cached = self._cached(unique_ids, max_age=self.cache_ttl_seconds)
        if cached is not None:
            return cached

        async with self._request_lock:
            cached = self._cached(unique_ids, max_age=self.cache_ttl_seconds)
            if cached is not None:
                return cached
            if self._clock() < self._unavailable_until:
                stale = self._cached(
                    unique_ids,
                    max_age=self.stale_ttl_seconds,
                    mark_stale=True,
                )
                if stale is not None:
                    return stale
                raise SteamPresenceError("Steam 实时状态接口暂时不可用")
            try:
                result = await self._fetch_summaries(unique_ids)
            except SteamPresenceError as exc:
                if not exc.retryable:
                    raise
                self._unavailable_until = self._clock() + self.failure_cooldown_seconds
                stale = self._cached(
                    unique_ids,
                    max_age=self.stale_ttl_seconds,
                    mark_stale=True,
                )
                if stale is not None:
                    return stale
                raise
            self._unavailable_until = 0.0
            checked_at = self._clock()
            for steam_id in unique_ids:
                self._checked_at[steam_id] = checked_at
                presence = result.get(steam_id)
                if presence is None:
                    self._cache.pop(steam_id, None)
                else:
                    self._cache[steam_id] = presence
            self._prune_cache(now=checked_at)
            return result

    async def _fetch_summaries(
        self,
        unique_ids: tuple[str, ...],
    ) -> SteamPresenceSnapshot:
        result = SteamPresenceSnapshot()
        for start in range(0, len(unique_ids), 100):
            batch = unique_ids[start : start + 100]
            payload = await self._fetch_batch(batch)
            players = (
                payload.get("response", {}).get("players") if isinstance(payload, dict) else None
            )
            if not isinstance(players, list):
                raise SteamPresenceError("Steam 实时状态返回格式异常")
            for player in players:
                if not isinstance(player, dict):
                    continue
                steam_id = str(player.get("steamid") or "").strip()
                if not steam_id:
                    continue
                server_ip = self._normalize_server_ip(player.get("gameserverip"))
                result[steam_id] = SteamPresence(
                    steam_id=steam_id,
                    persona_name=str(player.get("personaname") or "").strip(),
                    game_id=str(player.get("gameid") or "").strip(),
                    server_ip=server_ip,
                    game_name=str(player.get("gameextrainfo") or "").strip(),
                    persona_state=self._persona_state(player.get("personastate")),
                )
        return result

    def _cached(
        self,
        steam_ids: tuple[str, ...],
        *,
        max_age: float,
        mark_stale: bool = False,
    ) -> SteamPresenceSnapshot | None:
        now = self._clock()
        if any(
            steam_id not in self._checked_at or now - self._checked_at[steam_id] > max_age
            for steam_id in steam_ids
        ):
            return None
        return SteamPresenceSnapshot(
            {
                steam_id: replace(presence, stale=mark_stale)
                for steam_id in steam_ids
                if (presence := self._cache.get(steam_id)) is not None
            },
            stale=mark_stale,
        )

    def _prune_cache(self, *, now: float | None = None) -> None:
        current = self._clock() if now is None else now
        expired = [
            steam_id
            for steam_id, checked_at in self._checked_at.items()
            if current - checked_at > self.stale_ttl_seconds
        ]
        for steam_id in expired:
            self._checked_at.pop(steam_id, None)
            self._cache.pop(steam_id, None)
        excess = len(self._checked_at) - self.max_cache_entries
        if excess <= 0:
            return
        oldest = sorted(self._checked_at, key=self._checked_at.__getitem__)[:excess]
        for steam_id in oldest:
            self._checked_at.pop(steam_id, None)
            self._cache.pop(steam_id, None)

    async def _fetch_batch(self, batch: tuple[str, ...]) -> Any:
        if self._proxy_client is not None:
            try:
                return await self._fetch_batch_with_client(
                    self._proxy_client,
                    batch,
                    attempts=1,
                )
            except SteamPresenceError as exc:
                if not exc.retryable:
                    raise
            return await self._fetch_batch_with_client(
                self._direct_client,
                batch,
                attempts=max(1, self.retry_attempts - 1),
            )
        return await self._fetch_batch_with_client(
            self._direct_client,
            batch,
            attempts=self.retry_attempts,
        )

    async def _fetch_batch_with_client(
        self,
        client: httpx.AsyncClient,
        batch: tuple[str, ...],
        *,
        attempts: int,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = await client.get(
                    "/ISteamUser/GetPlayerSummaries/v2/",
                    params={"steamids": ",".join(batch)},
                    headers={"x-webapi-key": self.api_key},
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status = exc.response.status_code
                retryable = status >= 500 or status in _RETRYABLE_HTTP_STATUS_CODES
                if not retryable or attempt + 1 >= attempts:
                    raise SteamPresenceError(
                        f"Steam 实时状态接口返回 HTTP {status}",
                        retryable=retryable,
                    ) from exc
            except httpx.TimeoutException as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    raise SteamPresenceError("Steam 实时状态查询超时") from exc
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    raise SteamPresenceError("Steam 实时状态查询失败") from exc
            except ValueError as exc:
                raise SteamPresenceError("Steam 实时状态返回格式异常") from exc
            await asyncio.sleep(0.5)
        raise SteamPresenceError("Steam 实时状态查询失败") from last_error

    @staticmethod
    def _normalize_server_ip(value: Any) -> str:
        server_ip = str(value or "").strip()
        if server_ip in {"0.0.0.0", "0.0.0.0:0", "[::]", "[::]:0"}:
            return ""
        return server_ip

    @staticmethod
    def _persona_state(value: Any) -> int:
        try:
            state = int(value)
        except (TypeError, ValueError):
            return 0
        return state if state in range(7) else 0


_PERSONA_STATES = {
    0: ("⚫", "离线或隐身"),
    1: ("🟢", "在线"),
    2: ("🔴", "忙碌"),
    3: ("🟡", "离开"),
    4: ("🌙", "打盹"),
    5: ("🔄", "想交易"),
    6: ("🎯", "想一起玩"),
}

_PERSONA_STATUS_KINDS = {
    0: "offline",
    1: "online",
    2: "busy",
    3: "away",
    4: "snooze",
    5: "trade",
    6: "looking",
}


def _game_status_entry(name: str, presence: SteamPresence) -> GameStatusEntry:
    display_name = name.strip() or presence.persona_name or presence.steam_id
    if presence.is_playing:
        return GameStatusEntry(
            name=display_name,
            steam_id=presence.steam_id,
            status_label="正在游戏",
            status_kind="playing",
            game_name=presence.display_game,
            app_id=presence.game_id,
        )
    return GameStatusEntry(
        name=display_name,
        steam_id=presence.steam_id,
        status_label=_PERSONA_STATES[presence.persona_state][1],
        status_kind=_PERSONA_STATUS_KINDS[presence.persona_state],
    )


def build_player_game_status_snapshot(
    name: str,
    presence: SteamPresence | None,
    *,
    stale: bool = False,
) -> GameStatusSnapshot:
    display_name = name.strip() or (
        presence.persona_name if presence is not None else "未知玩家"
    )
    cache_is_stale = stale or bool(presence is not None and presence.stale)
    if presence is None:
        entry = GameStatusEntry(
            name=display_name,
            steam_id="",
            status_label="状态不可见",
            status_kind="unknown",
        )
        return GameStatusSnapshot(
            entries=(entry,),
            total_count=1,
            playing_count=0,
            hidden_count=1,
            stale=cache_is_stale,
            title=f"{display_name} 的游戏状态",
            note="Steam 未返回公开状态，可能处于隐身或资料不可见",
        )
    entry = _game_status_entry(display_name, presence)
    return GameStatusSnapshot(
        entries=(entry,),
        total_count=1,
        playing_count=int(presence.is_playing),
        hidden_count=int(not presence.is_playing and presence.persona_state == 0),
        stale=cache_is_stale,
        title=f"{display_name} 的游戏状态",
    )


def build_group_game_status_snapshot(
    entries: list[tuple[str, SteamPresence]],
    *,
    total_count: int,
    stale: bool = False,
) -> GameStatusSnapshot:
    playing = sorted(
        (entry for entry in entries if entry[1].is_playing),
        key=lambda entry: (entry[1].display_game.casefold(), entry[0].casefold()),
    )
    online = sorted(
        (
            entry
            for entry in entries
            if not entry[1].is_playing and entry[1].persona_state != 0
        ),
        key=lambda entry: entry[0].casefold(),
    )
    hidden_count = sum(
        not presence.is_playing and presence.persona_state == 0
        for _name, presence in entries
    ) + max(0, total_count - len(entries))
    visible = tuple(
        _game_status_entry(name, presence) for name, presence in (*playing, *online)
    )
    return GameStatusSnapshot(
        entries=visible,
        total_count=total_count,
        playing_count=len(playing),
        hidden_count=hidden_count,
        stale=stale or any(presence.stale for _name, presence in entries),
        note="当前没人公开显示正在游戏" if not playing else "",
    )


def format_player_game_status(
    name: str,
    presence: SteamPresence | None,
    *,
    stale: bool = False,
) -> str:
    display_name = name.strip() or (presence.persona_name if presence is not None else "未知玩家")
    lines = [f"🎮 Steam 游戏状态｜{display_name}"]
    if presence is None:
        lines.append("状态：Steam 未返回公开状态，可能处于隐身或资料不可见")
        if stale:
            lines.append("⚠️ Steam 接口暂时不可用，显示最近一次缓存状态")
        return "\n".join(lines)
    cache_is_stale = stale or presence.stale
    if presence.is_playing:
        lines.extend(("状态：正在游戏", f"游戏：{presence.display_game}"))
        if presence.game_id:
            lines.append(f"AppID：{presence.game_id}")
        if cache_is_stale:
            lines.append("⚠️ Steam 接口暂时不可用，显示最近一次缓存状态")
        return "\n".join(lines)
    _emoji, state = _PERSONA_STATES[presence.persona_state]
    lines.append(f"状态：{state}")
    if cache_is_stale:
        lines.append("⚠️ Steam 接口暂时不可用，显示最近一次缓存状态")
    return "\n".join(lines)


def format_group_game_status(
    entries: list[tuple[str, SteamPresence]],
    *,
    total_count: int,
    stale: bool = False,
) -> str:
    playing = sorted(
        (entry for entry in entries if entry[1].is_playing),
        key=lambda entry: (entry[1].display_game.casefold(), entry[0].casefold()),
    )
    online = sorted(
        (entry for entry in entries if not entry[1].is_playing and entry[1].persona_state != 0),
        key=lambda entry: entry[0].casefold(),
    )
    offline_count = sum(
        not presence.is_playing and presence.persona_state == 0 for _name, presence in entries
    )
    unavailable_count = max(0, total_count - len(entries))
    lines = [f"🎮 Steam 游戏状态｜{len(playing)}/{total_count} 人正在游戏"]
    if stale or any(presence.stale for _name, presence in entries):
        lines.append("⚠️ Steam 接口暂时不可用，以下为最近一次缓存状态")
    if playing:
        lines.extend(f"🎯 {name}｜{presence.display_game}" for name, presence in playing)
    else:
        lines.append("当前没人公开显示正在游戏")
    lines.extend(
        f"{_PERSONA_STATES[presence.persona_state][0]} {name}｜"
        f"{_PERSONA_STATES[presence.persona_state][1]}"
        for name, presence in online
    )
    hidden_count = offline_count + unavailable_count
    if hidden_count:
        lines.append(f"⚫ 离线、隐身或状态不可见：{hidden_count} 人")
    return "\n".join(lines)
