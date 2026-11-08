"""Async client for Perfect World's detailStats endpoint."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any
from weakref import WeakValueDictionary

import httpx


class PerfectWorldApiError(RuntimeError):
    """A user-displayable Perfect World API failure."""


class PerfectWorldClient:
    def __init__(
        self,
        token: str,
        *,
        app_version: str = "3.5.4.172",
        base_url: str = "https://api.wmpvp.com",
        my_steam_id: str = "",
        timeout_seconds: float = 10.0,
        cache_ttl_seconds: int = 60,
        max_cache_entries: int = 512,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if max_cache_entries < 1:
            raise ValueError("max_cache_entries must be at least 1")
        self.token = token.strip()
        self.app_version = app_version
        normalized_my_steam_id = my_steam_id.strip()
        if normalized_my_steam_id.startswith("${") and normalized_my_steam_id.endswith("}"):
            normalized_my_steam_id = ""
        self.my_steam_id = normalized_my_steam_id
        self.cache_ttl_seconds = cache_ttl_seconds
        self.max_cache_entries = max_cache_entries
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            headers={
                "appversion": app_version,
                "platform": "android",
                "gameType": "1,2",
                "gameTypeStr": "1,2",
                "User-Agent": "pwcs2-qq-bot/0.1",
            },
        )
        self._cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._v2_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._match_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def close(self) -> None:
        await self._client.aclose()

    async def detail_stats(self, steam_id: str) -> dict[str, Any]:
        if not self.token:
            raise PerfectWorldApiError("服务器未配置 PERFECTWORLD_TOKEN")

        if cached := self._get_cached(steam_id):
            return cached

        lock_key = f"profile:{steam_id}"
        lock = self._locks.get(lock_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[lock_key] = lock
        async with lock:
            if cached := self._get_cached(steam_id):
                return cached
            data = await self._fetch_detail_stats(steam_id)
            self._cache[steam_id] = (
                time.monotonic() + self.cache_ttl_seconds,
                data,
            )
            self._cache.move_to_end(steam_id)
            while len(self._cache) > self.max_cache_entries:
                self._cache.popitem(last=False)
            return data

    async def match_list(self, steam_id: str, *, page_size: int = 5) -> list[dict[str, Any]]:
        """Return the target player's newest completed Perfect World matches."""
        return await self._match_list(steam_id, page_size=page_size, data_source=3)

    async def official_match_list(
        self, steam_id: str, *, page_size: int = 5
    ) -> list[dict[str, Any]]:
        """Return the target player's newest completed official matches."""
        matches = await self._match_list(
            steam_id,
            page_size=page_size,
            data_source=1,
        )
        return [{**match, "dataSource": 1} for match in matches]

    async def _match_list(
        self,
        steam_id: str,
        *,
        page_size: int,
        data_source: int,
    ) -> list[dict[str, Any]]:
        if not self.token:
            raise PerfectWorldApiError("服务器未配置 PERFECTWORLD_TOKEN")
        if not 1 <= page_size <= 50:
            raise ValueError("page_size must be between 1 and 50")

        my_steam_id = self.my_steam_id or steam_id
        if not my_steam_id.isdigit():
            raise PerfectWorldApiError("PERFECTWORLD_MY_STEAM_ID 必须是数字 SteamID64")
        data = await self._post(
            "/api/csgo/home/match/list",
            {
                "csgoSeasonId": "recent",
                "dataSource": data_source,
                "mySteamId": int(my_steam_id),
                "page": 1,
                "pageSize": page_size,
                "pvpType": -1,
                "toSteamId": int(steam_id),
            },
        )
        if not isinstance(data, dict):
            raise PerfectWorldApiError("完美世界比赛列表返回了无效数据")
        matches = data.get("matchList")
        if matches is None:
            return []
        if not isinstance(matches, list):
            raise PerfectWorldApiError("完美世界比赛列表返回了无效数据")
        return [item for item in matches if isinstance(item, dict)]

    async def detail_stats_v2(self, steam_id: str) -> dict[str, Any]:
        """Return the current app's extended stats, including radar metrics."""
        if not self.token:
            raise PerfectWorldApiError("服务器未配置 PERFECTWORLD_TOKEN")

        if cached := self._get_v2_cached(steam_id):
            return cached

        lock_key = f"profile-v2:{steam_id}"
        lock = self._locks.get(lock_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[lock_key] = lock
        async with lock:
            if cached := self._get_v2_cached(steam_id):
                return cached
            data = await self._fetch_detail_stats_v2(steam_id)
            self._v2_cache[steam_id] = (
                time.monotonic() + self.cache_ttl_seconds,
                data,
            )
            self._v2_cache.move_to_end(steam_id)
            while len(self._v2_cache) > self.max_cache_entries:
                self._v2_cache.popitem(last=False)
            return data

    async def match_detail(self, match_id: str, *, data_source: int = 3) -> dict[str, Any]:
        """Return one completed match's base data and all participating players."""
        if not self.token:
            raise PerfectWorldApiError("服务器未配置 PERFECTWORLD_TOKEN")
        normalized_match_id = match_id.strip()
        if not normalized_match_id:
            raise ValueError("match_id must not be blank")

        cache_key = f"{data_source}:{normalized_match_id}"
        if cached := self._get_match_cached(cache_key):
            return cached

        lock_key = f"match:{cache_key}"
        lock = self._locks.get(lock_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[lock_key] = lock
        async with lock:
            if cached := self._get_match_cached(cache_key):
                return cached
            data = await self._fetch_match_detail(normalized_match_id, data_source=data_source)
            self._match_cache[cache_key] = (
                time.monotonic() + self.cache_ttl_seconds,
                data,
            )
            self._match_cache.move_to_end(cache_key)
            while len(self._match_cache) > self.max_cache_entries:
                self._match_cache.popitem(last=False)
            return data

    def _get_cached(self, steam_id: str) -> dict[str, Any] | None:
        cached = self._cache.get(steam_id)
        if cached is None:
            return None
        if cached[0] <= time.monotonic():
            self._cache.pop(steam_id, None)
            return None
        self._cache.move_to_end(steam_id)
        return cached[1]

    def _get_match_cached(self, match_id: str) -> dict[str, Any] | None:
        cached = self._match_cache.get(match_id)
        if cached is None:
            return None
        if cached[0] <= time.monotonic():
            self._match_cache.pop(match_id, None)
            return None
        self._match_cache.move_to_end(match_id)
        return cached[1]

    def _get_v2_cached(self, steam_id: str) -> dict[str, Any] | None:
        cached = self._v2_cache.get(steam_id)
        if cached is None:
            return None
        if cached[0] <= time.monotonic():
            self._v2_cache.pop(steam_id, None)
            return None
        self._v2_cache.move_to_end(steam_id)
        return cached[1]

    async def _fetch_detail_stats(self, steam_id: str) -> dict[str, Any]:
        my_steam_id = self.my_steam_id or steam_id
        if not my_steam_id.isdigit():
            raise PerfectWorldApiError("PERFECTWORLD_MY_STEAM_ID 必须是数字 SteamID64")
        data = await self._post(
            "/api/csgo/home/pvp/detailStats",
            {
                "accessToken": "",
                "mySteamId": int(my_steam_id),
                "toSteamId": int(steam_id),
            },
        )
        if not isinstance(data, dict) or not data:
            raise PerfectWorldApiError("没有查到该 Steam 账号的完美平台战绩")
        return data

    async def _fetch_detail_stats_v2(self, steam_id: str) -> dict[str, Any]:
        my_steam_id = self.my_steam_id or steam_id
        if not my_steam_id.isdigit():
            raise PerfectWorldApiError("PERFECTWORLD_MY_STEAM_ID 必须是数字 SteamID64")
        data = await self._post(
            "/api/csgo/home/pvp/detailStats/v2",
            {
                "accessToken": "",
                "mySteamId": int(my_steam_id),
                "toSteamId": int(steam_id),
            },
        )
        if not isinstance(data, dict) or not data:
            raise PerfectWorldApiError("没有查到该 Steam 账号的完美平台扩展战绩")
        return data

    async def _fetch_match_detail(
        self,
        match_id: str,
        *,
        data_source: int,
    ) -> dict[str, Any]:
        try:
            response = await self._client.post(
                "/api/v1/csgo/match",
                headers={
                    "accessToken": self.token,
                    "device": "",
                    "platform": "h5_android",
                },
                json={
                    "matchId": match_id,
                    "platform": "admin",
                    "dataSource": data_source,
                },
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise PerfectWorldApiError("完美世界对局详情请求超时，请稍后重试") from exc
        except httpx.HTTPStatusError as exc:
            raise PerfectWorldApiError(
                f"完美世界对局详情返回 HTTP {exc.response.status_code}"
            ) from exc
        except httpx.RequestError as exc:
            raise PerfectWorldApiError("无法连接完美世界对局详情接口") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise PerfectWorldApiError("完美世界对局详情返回了无效数据") from exc
        if not isinstance(payload, dict):
            raise PerfectWorldApiError("完美世界对局详情返回了无效数据")

        status_code = payload.get("code", payload.get("statusCode"))
        if status_code not in (0, "0"):
            message = str(
                payload.get("message") or payload.get("errorMessage") or "未知错误"
            )
            raise PerfectWorldApiError(f"查询对局详情失败：{message}（状态码 {status_code}）")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise PerfectWorldApiError("完美世界对局详情返回了无效数据")
        base = data.get("base")
        players = data.get("players")
        if not isinstance(base, dict) or not isinstance(players, list):
            raise PerfectWorldApiError("完美世界对局详情缺少比赛或玩家数据")
        return data

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        try:
            response = await self._client.post(
                path,
                headers={"token": self.token},
                json=body,
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise PerfectWorldApiError("完美世界接口请求超时，请稍后重试") from exc
        except httpx.HTTPStatusError as exc:
            raise PerfectWorldApiError(
                f"完美世界接口返回 HTTP {exc.response.status_code}"
            ) from exc
        except httpx.RequestError as exc:
            raise PerfectWorldApiError("无法连接完美世界接口，请稍后重试") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise PerfectWorldApiError("完美世界接口返回了无效数据") from exc
        if not isinstance(payload, dict):
            raise PerfectWorldApiError("完美世界接口返回了无效数据")

        status_code = payload.get("statusCode")
        if status_code not in (0, "0"):
            message = str(payload.get("errorMessage") or "未知错误")
            raise PerfectWorldApiError(f"查询失败：{message}（状态码 {status_code}）")
        return payload.get("data")
