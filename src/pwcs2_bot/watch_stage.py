"""Read one live Perfect World watch-stage scoreboard on demand."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException


class WatchStageError(RuntimeError):
    """A safe, user-facing live watch-stage failure."""


@dataclass(frozen=True, slots=True)
class WatchStagePlayer:
    steam_id: str
    side: str
    nickname: str
    avatar_url: str
    kills: int
    assists: int
    deaths: int
    score: int
    adr: float
    headshot: str
    alive: bool
    pvp_score: int | None
    avg_rating: float | None
    avg_we: float | None
    is_group_member: bool = False


@dataclass(frozen=True, slots=True)
class WatchStageSnapshot:
    status: str
    requested_steam_id: str
    requested_name: str
    platform: str
    match_id: str
    map_name: str
    ct_score: int | None
    terrorist_score: int | None
    duration: str
    warmup: bool
    updated_at: str
    players: tuple[WatchStagePlayer, ...]
    message: str = ""


_MAP_NAMES = {
    "de_ancient": "远古遗迹",
    "de_anubis": "阿努比斯",
    "de_cache": "死城之谜",
    "de_cbble": "古堡激战",
    "de_dust2": "炙热沙城Ⅱ",
    "de_inferno": "炼狱小镇",
    "de_mirage": "荒漠迷城",
    "de_nuke": "核子危机",
    "de_overpass": "死亡游乐园",
    "de_train": "列车停放站",
    "de_vertigo": "殒命大厦",
}
_HTTP_HEADERS = {
    "Origin": "https://news.wmpvp.com",
    "Referer": "https://news.wmpvp.com/",
    "X-Requested-With": "XMLHttpRequest",
    "platform": "h5_web",
    "appversion": "4.1.0",
    "appTheme": "0",
    "Accept": "application/json, text/plain, */*",
}
_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "EsportsApp Version=4.1.0"
)


class WatchStageClient:
    def __init__(
        self,
        *,
        enabled: bool = True,
        base_url: str = "https://appactivity.wmpvp.com",
        timeout_seconds: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
        websocket_connect: Callable[..., Any] = connect,
    ) -> None:
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self._websocket_connect = websocket_connect
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            follow_redirects=False,
            headers=_HTTP_HEADERS,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def snapshot(
        self,
        steam_id: str,
        *,
        platform: str | None = None,
    ) -> WatchStageSnapshot:
        if not self.enabled:
            raise WatchStageError("观战功能尚未启用")
        if not steam_id.isdigit() or len(steam_id) != 17:
            raise ValueError("SteamID64 格式不正确")
        if platform not in {None, "1", "2"}:
            raise ValueError("观战平台必须为完美或官匹")

        no_active: WatchStageSnapshot | None = None
        failures: list[WatchStageError] = []
        platforms = (platform,) if platform is not None else ("2", "1")
        for selected_platform in platforms:
            try:
                snapshot = await self._platform_snapshot(steam_id, selected_platform)
            except WatchStageError as exc:
                failures.append(exc)
                continue
            if snapshot.status == "running":
                if any(player.steam_id == steam_id for player in snapshot.players):
                    return snapshot
                no_active = no_active or _no_active_snapshot(
                    steam_id,
                    selected_platform,
                )
                continue
            no_active = no_active or snapshot
        if no_active is not None:
            return (
                _no_active_snapshot(steam_id, "")
                if platform is None
                else no_active
            )
        if failures:
            raise failures[-1]
        raise WatchStageError("完美观将台暂时不可用，请稍后重试")

    async def _platform_snapshot(
        self, steam_id: str, platform: str
    ) -> WatchStageSnapshot:
        websocket_url = await self._websocket_url(steam_id, platform)
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with self._websocket_connect(
                    websocket_url,
                    origin="https://news.wmpvp.com",
                    user_agent_header=_USER_AGENT,
                    open_timeout=self.timeout_seconds,
                    close_timeout=3,
                    ping_interval=30,
                ) as websocket:
                    await websocket.send("ping")
                    subscribed = False
                    for _attempt in range(8):
                        raw = await websocket.recv()
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", errors="replace")
                        if raw == "pong":
                            await websocket.send(
                                json.dumps(
                                    {
                                        "messageType": 10001,
                                        "messageData": {"steam_id": steam_id},
                                    }
                                )
                            )
                            subscribed = True
                            continue
                        try:
                            payload = json.loads(raw)
                        except (TypeError, json.JSONDecodeError):
                            continue
                        message_type = _integer(payload.get("messageType"))
                        if message_type == 10003:
                            return _no_active_snapshot(steam_id, platform)
                        match_data = payload.get("messageData")
                        if message_type == 10002 and isinstance(match_data, dict):
                            players = match_data.get("playerList")
                            if not isinstance(players, list) or not any(
                                isinstance(player, dict)
                                and str(player.get("steamId") or "") == steam_id
                                for player in players
                            ):
                                continue
                            profiles = await self._team_statistics(match_data, platform)
                            return _build_snapshot(
                                steam_id,
                                platform,
                                match_data,
                                profiles,
                            )
                        if not subscribed:
                            await websocket.send(
                                json.dumps(
                                    {
                                        "messageType": 10001,
                                        "messageData": {"steam_id": steam_id},
                                    }
                                )
                            )
                            subscribed = True
        except TimeoutError as exc:
            raise WatchStageError("连接完美观将台超时，请稍后重试") from exc
        except (OSError, WebSocketException, ValueError) as exc:
            raise WatchStageError("连接完美观将台失败，请稍后重试") from exc
        raise WatchStageError("完美观将台没有返回实时比赛数据")

    async def _websocket_url(self, steam_id: str, platform: str) -> str:
        try:
            response = await self._client.get(
                "/steamcn/match/watchStage/getWebsocketInfo",
                params={"steamId": steam_id, "platform": platform},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise WatchStageError("获取完美观将台地址失败") from exc
        result = payload.get("result") if isinstance(payload, dict) else None
        websocket_url = result.get("websocketUrl") if isinstance(result, dict) else None
        if not isinstance(websocket_url, str) or not _trusted_websocket_url(websocket_url):
            raise WatchStageError("完美观将台返回了无效连接地址")
        return websocket_url

    async def _team_statistics(
        self, match_data: dict[str, Any], platform: str
    ) -> dict[str, dict[str, Any]]:
        players = match_data.get("playerList")
        if not isinstance(players, list):
            return {}
        ct_ids = [
            str(player.get("steamId"))
            for player in players
            if isinstance(player, dict)
            and player.get("side") == "CT"
            and player.get("steamId")
        ]
        terrorist_ids = [
            str(player.get("steamId"))
            for player in players
            if isinstance(player, dict)
            and player.get("side") == "TERRORIST"
            and player.get("steamId")
        ]
        if not ct_ids and not terrorist_ids:
            return {}
        path = (
            "/steamcn/match/watchStage/getMatchTeamStatisticsData"
            if platform == "1"
            else "/steamcn/match/watchStage/getPvPMatchTeamStatisticsData"
        )
        try:
            response = await self._client.post(
                path,
                json={
                    "ctTeamSteamIds": ct_ids,
                    "teTeamSteamIds": terrorist_ids,
                    "map": match_data.get("map"),
                },
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return {}
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            return {}
        keys = (
            ("ctTeamPlayInfoList", "teTeamPlayInfoList")
            if platform == "1"
            else ("ctPlayerStatsDTOList", "tplayerStatsDTOList")
        )
        profiles: dict[str, dict[str, Any]] = {}
        for key in keys:
            rows = result.get(key)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                profile_steam_id = _text(row.get("steamId"), "")
                if profile_steam_id:
                    profiles[profile_steam_id] = row
        return profiles


def format_watch_stage(snapshot: WatchStageSnapshot) -> str:
    target = snapshot.requested_name or snapshot.requested_steam_id
    source = (
        "官匹观战"
        if snapshot.platform == "1"
        else "完美观战"
        if snapshot.platform == "2"
        else "观战"
    )
    if snapshot.status != "running":
        return (
            f"👁️ {source}｜{target}\n"
            "当前没有可展示的实时比赛，可能尚未开打、已经结束，"
            "或该对局不支持观将台。"
        )
    map_label = display_map_name(snapshot.map_name)
    score = f"CT {snapshot.ct_score or 0} : {snapshot.terrorist_score or 0} T"
    state = "热身中" if snapshot.warmup else snapshot.duration or "进行中"
    lines = [f"👁️ {source}｜{map_label}", f"{score}｜{state}"]
    for side, label in (("CT", "🔵 CT"), ("TERRORIST", "🟠 T")):
        lines.append(label)
        players = [player for player in snapshot.players if player.side == side]
        for player in players:
            alive = "●" if player.alive else "○"
            group = "🐮" if player.is_group_member else ""
            lines.append(
                f"{alive}{group}{player.nickname} "
                f"{player.kills}/{player.assists}/{player.deaths} "
                f"ADR {player.adr:.0f}"
            )
    return "\n".join(lines)


def display_map_name(value: str) -> str:
    normalized = value.strip().casefold()
    localized = _MAP_NAMES.get(normalized)
    if localized:
        return f"{localized} · {value}"
    return value or "未知地图"


def _profile_rating(profile: dict[str, Any], platform: str) -> float | None:
    keys = ("rating", "ratingPro") if platform == "1" else ("ratingPro", "rating")
    for key in keys:
        value = _float(profile.get(key))
        if value is not None and value > 0:
            return value
    return None


def _build_snapshot(
    requested_steam_id: str,
    platform: str,
    match_data: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
) -> WatchStageSnapshot:
    raw_players = match_data.get("playerList")
    players: list[WatchStagePlayer] = []
    if isinstance(raw_players, list):
        for raw in raw_players:
            if not isinstance(raw, dict):
                continue
            steam_id = _text(raw.get("steamId"), "")
            if not steam_id:
                continue
            profile = profiles.get(steam_id, {})
            players.append(
                WatchStagePlayer(
                    steam_id=steam_id,
                    side=_text(raw.get("side"), ""),
                    nickname=_text(
                        profile.get("nickname")
                        or profile.get("nickName")
                        or raw.get("nickname"),
                        steam_id,
                    ),
                    avatar_url=_text(profile.get("avatar"), ""),
                    kills=_integer(raw.get("kill")) or 0,
                    assists=_integer(raw.get("assist")) or 0,
                    deaths=_integer(raw.get("death")) or 0,
                    score=_integer(raw.get("score")) or 0,
                    adr=_float(raw.get("adr")) or 0.0,
                    headshot=_text(raw.get("headshot"), "-"),
                    alive=bool(raw.get("alive")),
                    pvp_score=(
                        None
                        if platform == "1"
                        else _integer(profile.get("pvpScore"))
                    ),
                    avg_rating=_profile_rating(profile, platform),
                    avg_we=None if platform == "1" else _float(profile.get("we")),
                )
            )
    players.sort(
        key=lambda player: (
            0 if player.side == "CT" else 1,
            -player.score,
            -player.kills,
            player.nickname.casefold(),
        )
    )
    return WatchStageSnapshot(
        status="running",
        requested_steam_id=requested_steam_id,
        requested_name="",
        platform=platform,
        match_id=_text(match_data.get("matchId"), ""),
        map_name=_text(match_data.get("map"), "未知地图"),
        ct_score=_integer(match_data.get("ctScore")),
        terrorist_score=_integer(match_data.get("terroristScore")),
        duration=_format_duration(match_data.get("duration")),
        warmup=bool(match_data.get("warmUpStatus")),
        updated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        players=tuple(players),
    )


def _no_active_snapshot(steam_id: str, platform: str) -> WatchStageSnapshot:
    return WatchStageSnapshot(
        status="no_active_match",
        requested_steam_id=steam_id,
        requested_name="",
        platform=platform,
        match_id="",
        map_name="",
        ct_score=None,
        terrorist_score=None,
        duration="",
        warmup=False,
        updated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        players=(),
        message="该玩家当前没有可展示的观将台比赛",
    )


def _trusted_websocket_url(value: str) -> bool:
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").casefold()
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "wss"
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
        and (hostname == "wmpvp.com" or hostname.endswith(".wmpvp.com"))
    )


def _format_duration(value: Any) -> str:
    text = _text(value, "")
    if not text:
        return ""
    if ":" in text:
        return text
    seconds = _integer(value)
    if seconds is None or seconds < 0:
        return text
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _text(value: Any, fallback: str) -> str:
    if value is None:
        return fallback
    normalized = " ".join(str(value).split())
    return normalized or fallback


def _integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
