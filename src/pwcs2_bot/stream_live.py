"""Strict public-page status adapters for Douyu, Huya and Douyin."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import unescape
from typing import Any, Literal
from urllib.parse import unquote, urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

PlatformName = Literal["douyu", "huya", "douyin"]


class StreamLiveError(RuntimeError):
    """Raised when a platform response cannot be trusted as live or offline."""


class StreamerTarget(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    platform: PlatformName
    name: str = Field(min_length=1, max_length=40)
    room_id: str = Field(min_length=1, max_length=160)
    url: str = ""
    expected_sec_uid: str = Field(default="", max_length=200)
    enabled: bool = True

    @field_validator("platform", mode="before")
    @classmethod
    def normalize_platform(cls, value: Any) -> str:
        return str(value).strip().casefold()

    @field_validator("name", "room_id", "url", "expected_sec_uid", mode="before")
    @classmethod
    def strip_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("name")
    @classmethod
    def reject_control_characters_in_name(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("name must not contain control characters")
        return value

    @model_validator(mode="after")
    def validate_target(self) -> StreamerTarget:
        if not self.room_id.isdecimal():
            raise ValueError("roomId must contain decimal digits only")
        if not self.url:
            return self
        parsed = urlparse(self.url)
        allowed_hosts = {
            "douyu": {"www.douyu.com", "douyu.com"},
            "huya": {"www.huya.com", "huya.com"},
            "douyin": {"live.douyin.com"},
        }
        if (
            parsed.scheme != "https"
            or parsed.hostname not in allowed_hosts[self.platform]
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("url must be an HTTPS room URL on the selected platform")
        path_room_id = parsed.path.strip("/")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", path_room_id):
            raise ValueError("url must point directly to one room")
        if self.platform == "douyin" and path_room_id != self.room_id:
            raise ValueError("Douyin URL room ID must match roomId")
        return self


@dataclass(frozen=True, slots=True)
class LiveSnapshot:
    platform: str
    room_id: str
    streamer_name: str
    is_live: bool
    session_key: str
    title: str
    category: str
    cover_url: str
    live_url: str
    started_at: str
    is_replay: bool = False

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> LiveSnapshot:
        try:
            return cls(
                platform=str(payload["platform"]),
                room_id=str(payload["room_id"]),
                streamer_name=str(payload["streamer_name"]),
                is_live=bool(payload["is_live"]),
                session_key=str(payload.get("session_key") or ""),
                title=str(payload.get("title") or ""),
                category=str(payload.get("category") or ""),
                cover_url=str(payload.get("cover_url") or ""),
                live_url=str(payload.get("live_url") or ""),
                started_at=str(payload.get("started_at") or ""),
                is_replay=bool(payload.get("is_replay", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StreamLiveError("invalid persisted live snapshot") from exc


def _clean(value: Any, *, limit: int = 160) -> str:
    text = re.sub(r"[\r\n\t]+", " ", unescape(str(value or ""))).strip()
    return re.sub(r"\s{2,}", " ", text)[:limit]


def _json_object(value: Any, message: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StreamLiveError(message)
    return value


class LivePlatformClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 10,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=True,
            transport=transport,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/126 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch(self, target: StreamerTarget) -> LiveSnapshot:
        if target.platform == "douyu":
            return await self._fetch_douyu(target)
        if target.platform == "huya":
            return await self._fetch_huya(target)
        if target.platform == "douyin":
            return await self._fetch_douyin(target)
        raise StreamLiveError(f"unsupported platform: {target.platform}")

    async def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._client.get(url, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPError as exc:
            raise StreamLiveError(f"live platform request failed: {exc}") from exc

    async def _fetch_douyu(self, target: StreamerTarget) -> LiveSnapshot:
        response = await self._get(
            f"https://open.douyucdn.cn/api/RoomApi/room/{target.room_id}"
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise StreamLiveError("Douyu returned invalid JSON") from exc
        root = _json_object(payload, "Douyu returned a non-object response")
        data = _json_object(root.get("data"), "Douyu response is missing room data")
        if root.get("error") != 0 or str(data.get("room_id") or "") != target.room_id:
            raise StreamLiveError("Douyu returned a mismatched room")
        status = str(data.get("room_status") or "")
        if status not in {"1", "2"}:
            raise StreamLiveError(f"Douyu returned an unknown room status: {status!r}")
        is_live = status == "1"
        started_at = _clean(data.get("start_time")) if is_live else ""
        return LiveSnapshot(
            platform="douyu",
            room_id=target.room_id,
            streamer_name=_clean(data.get("owner_name")) or target.name,
            is_live=is_live,
            session_key=started_at,
            title=_clean(data.get("room_name")),
            category=_clean(data.get("cate_name"), limit=60),
            cover_url=_clean(data.get("room_thumb"), limit=500),
            live_url=target.url or f"https://www.douyu.com/{target.room_id}",
            started_at=started_at,
        )

    async def _fetch_huya(self, target: StreamerTarget) -> LiveSnapshot:
        profile: dict[str, Any]
        live: dict[str, Any]
        try:
            response = await self._get(
                "https://mp.huya.com/cache.php",
                params={"m": "Live", "do": "profileRoom", "roomid": target.room_id},
                headers={"Referer": "https://www.huya.com/"},
            )
            payload = response.json(parse_int=str)
            profile, live, data = self._validate_huya_payload(payload, target)
            if str(data.get("realLiveStatus") or "").upper() == "ON":
                page_payload = await self._fetch_huya_page_payload(target)
                _page_profile, _page_live, page_data = self._validate_huya_payload(
                    page_payload,
                    target,
                )
                page_status = str(page_data.get("realLiveStatus") or "").upper()
                if page_status != "ON":
                    raise StreamLiveError(
                        "Huya status endpoint and room page disagree on live status"
                    )
                data = {
                    **data,
                    "isReplay": self._huya_is_replay(page_data, _page_live),
                }
        except (StreamLiveError, TypeError, ValueError) as primary_error:
            try:
                payload = await self._fetch_huya_page_payload(target)
                profile, live, data = self._validate_huya_payload(payload, target)
            except StreamLiveError as fallback_error:
                raise StreamLiveError(
                    f"Huya status and page fallback failed: {fallback_error}"
                ) from primary_error
        real_status = str(data.get("realLiveStatus") or "").upper()
        is_replay = self._huya_is_replay(data, live)
        is_live = real_status == "ON" and not is_replay
        started_at = _clean(live.get("startTime")) if is_live else ""
        session_key = (_clean(live.get("liveId")) or started_at) if is_live else ""
        return LiveSnapshot(
            platform="huya",
            room_id=target.room_id,
            streamer_name=_clean(profile.get("nick")) or target.name,
            is_live=is_live,
            session_key=session_key,
            title=_clean(live.get("introduction")),
            category=_clean(live.get("gameFullName"), limit=60),
            cover_url=_clean(live.get("screenshot"), limit=500),
            live_url=target.url or f"https://www.huya.com/{target.room_id}",
            started_at=started_at,
            is_replay=is_replay,
        )

    @staticmethod
    def _huya_is_replay(data: dict[str, Any], live: dict[str, Any]) -> bool:
        for container in (data, live):
            value = container.get("isReplay")
            if value is True or str(value).strip().casefold() in {"1", "true"}:
                return True
        return False

    @staticmethod
    def _validate_huya_payload(
        payload: Any,
        target: StreamerTarget,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        root = _json_object(payload, "Huya returned a non-object response")
        data = _json_object(root.get("data"), "Huya response is missing room data")
        profile = _json_object(
            data.get("profileInfo"), "Huya response is missing profile data"
        )
        if int(root.get("status") or 0) != 200:
            raise StreamLiveError("Huya did not recognize the configured room")
        if str(profile.get("profileRoom") or "") != target.room_id:
            raise StreamLiveError("Huya returned a mismatched room")
        expected_alias = _clean(target.url.rsplit("/", 1)[-1]).casefold() if target.url else ""
        private_host = _clean(profile.get("privateHost")).casefold()
        if expected_alias and not expected_alias.isdecimal() and private_host != expected_alias:
            raise StreamLiveError("Huya returned a mismatched room alias")
        real_status = str(data.get("realLiveStatus") or "").upper()
        live_status = str(data.get("liveStatus") or "").upper()
        if real_status not in {"ON", "OFF"} or live_status not in {"ON", "OFF"}:
            raise StreamLiveError("Huya returned an unknown live status")
        if real_status != live_status:
            raise StreamLiveError("Huya returned contradictory live statuses")
        live_data = data.get("liveData")
        if live_data in (None, []):
            live: dict[str, Any] = {}
        else:
            live = _json_object(live_data, "Huya returned invalid live data")
        return profile, live, data

    async def _fetch_huya_page_payload(self, target: StreamerTarget) -> dict[str, Any]:
        response = await self._get(
            target.url or f"https://www.huya.com/{target.room_id}",
            headers={"Referer": "https://www.huya.com/"},
        )
        match = re.search(
            r"var\s+TT_ROOM_DATA\s*=\s*(\{.*?\})\s*;\s*var\s+TT_PROFILE_INFO",
            response.text,
            flags=re.S,
        )
        if match is None:
            raise StreamLiveError("Huya page is missing TT_ROOM_DATA")
        try:
            room = json.loads(match.group(1), parse_int=str)
        except ValueError as exc:
            raise StreamLiveError("Huya page room data is invalid JSON") from exc
        room = _json_object(room, "Huya page returned invalid room data")
        state = str(room.get("state") or "").upper()
        is_on = room.get("isOn")
        is_replay = room.get("isReplay")
        if (
            state not in {"ON", "OFF"}
            or not isinstance(is_on, bool)
            or not isinstance(is_replay, bool)
        ):
            raise StreamLiveError("Huya page returned an unknown live status")
        if (state == "ON") != is_on:
            raise StreamLiveError("Huya page returned contradictory live statuses")
        return {
            "status": 200,
            "data": {
                "liveStatus": state,
                "realLiveStatus": state,
                "isReplay": is_replay,
                "profileInfo": {
                    "profileRoom": room.get("profileRoom"),
                    "privateHost": room.get("privateHost"),
                    "nick": room.get("nick") or room.get("hostName") or target.name,
                },
                "liveData": {
                    "startTime": room.get("startTime"),
                    "liveId": room.get("liveId"),
                    "introduction": room.get("introduction") or room.get("roomName"),
                    "gameFullName": room.get("gameFullName") or room.get("gameName"),
                    "screenshot": room.get("screenshot"),
                    "isReplay": is_replay,
                },
            },
        }

    async def _fetch_douyin(self, target: StreamerTarget) -> LiveSnapshot:
        room_id = target.room_id
        url = target.url or f"https://live.douyin.com/{room_id}"
        response = await self._get(url, headers={"Referer": "https://live.douyin.com/"})
        room = self._extract_douyin_room(response.text, expected_web_rid=room_id)
        status = room.get("status")
        try:
            status_number = int(status)
        except (TypeError, ValueError) as exc:
            raise StreamLiveError("Douyin response is missing a trusted room status") from exc
        if status_number not in {2, 4}:
            raise StreamLiveError(f"Douyin returned an unknown room status: {status!r}")
        is_live = status_number == 2
        owner = room.get("owner") if isinstance(room.get("owner"), dict) else {}
        actual_sec_uid = _clean(owner.get("sec_uid"), limit=200)
        if target.expected_sec_uid and actual_sec_uid != target.expected_sec_uid:
            raise StreamLiveError("Douyin returned a mismatched streamer identity")
        title = _clean(room.get("title"))
        started_at = _clean(room.get("start_time") or room.get("create_time")) if is_live else ""
        room_id_str = _clean(room.get("id_str") or room.get("room_id")) or room_id
        cover = room.get("cover") if isinstance(room.get("cover"), dict) else {}
        urls = cover.get("url_list") if isinstance(cover, dict) else None
        cover_url = _clean(urls[0], limit=500) if isinstance(urls, list) and urls else ""
        return LiveSnapshot(
            platform="douyin",
            room_id=room_id,
            streamer_name=_clean(owner.get("nickname")) or target.name,
            is_live=is_live,
            session_key=(room_id_str + ":" + started_at) if is_live else "",
            title=title,
            category="抖音直播",
            cover_url=cover_url,
            live_url=url,
            started_at=started_at,
        )

    @classmethod
    def _extract_douyin_room(
        cls,
        html_text: str,
        *,
        expected_web_rid: str,
    ) -> dict[str, Any]:
        if not html_text or "readygo()" in html_text.casefold():
            raise StreamLiveError("Douyin returned a risk-control page")
        candidates: list[str] = []
        for pattern in (
            r'<script[^>]+id=["\']RENDER_DATA["\'][^>]*>(.*?)</script>',
        ):
            candidates.extend(re.findall(pattern, html_text, flags=re.I | re.S))
        for candidate in candidates:
            decoded = unquote(unescape(candidate)).strip()
            try:
                rendered = json.loads(decoded)
            except ValueError:
                continue
            room = cls._find_douyin_room(
                rendered,
                expected_web_rid=expected_web_rid,
            )
            if room is not None:
                return room

        # Newer live pages carry React Server Component state in Flight chunks
        # instead of RENDER_DATA. Decode only the JSON argument; never evaluate
        # the surrounding script.
        flight_arguments: list[str] = []
        prefix = "self.__pace_f.push("
        for script_body in re.findall(
            r"<script[^>]*>(.*?)</script>", html_text, flags=re.I | re.S
        ):
            script = unescape(script_body).strip()
            if script.endswith(";"):
                script = script[:-1].rstrip()
            if script.startswith(prefix) and script.endswith(")"):
                flight_arguments.append(script[len(prefix) : -1])
        for argument in flight_arguments:
            try:
                flight_chunk = json.loads(argument)
            except ValueError:
                continue
            if (
                not isinstance(flight_chunk, list)
                or len(flight_chunk) < 2
                or not isinstance(flight_chunk[1], str)
            ):
                continue
            for line in flight_chunk[1].splitlines():
                _, separator, payload = line.partition(":")
                if not separator or not payload.strip():
                    continue
                try:
                    rendered = json.loads(payload)
                except ValueError:
                    continue
                room = cls._find_douyin_room(
                    rendered,
                    expected_web_rid=expected_web_rid,
                )
                if room is not None:
                    return room
        raise StreamLiveError("Douyin page did not contain trusted room data")

    @classmethod
    def _find_douyin_room(
        cls,
        value: Any,
        *,
        expected_web_rid: str,
    ) -> dict[str, Any] | None:
        if isinstance(value, dict):
            room_info = value.get("roomInfo")
            if isinstance(room_info, dict):
                room = room_info.get("room")
                if isinstance(room, dict) and cls._douyin_room_matches_target(
                    room,
                    room_info,
                    expected_web_rid,
                ):
                    anchor = room_info.get("anchor")
                    owner = room.get("owner")
                    if isinstance(anchor, dict):
                        merged_owner = {
                            **anchor,
                            **(owner if isinstance(owner, dict) else {}),
                        }
                        return {**room, "owner": merged_owner}
                    return room
            for child in value.values():
                found = cls._find_douyin_room(
                    child,
                    expected_web_rid=expected_web_rid,
                )
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = cls._find_douyin_room(
                    child,
                    expected_web_rid=expected_web_rid,
                )
                if found is not None:
                    return found
        return None

    @staticmethod
    def _douyin_room_matches_target(
        room: dict[str, Any],
        room_info: dict[str, Any],
        expected_web_rid: str,
    ) -> bool:
        if room.get("status") not in {2, 4, "2", "4"}:
            return False
        identifiers: set[str] = set()
        for container in (room_info, room):
            for key in (
                "web_rid",
                "webRid",
                "roomId",
                "room_id",
                "room_id_str",
                "id_str",
            ):
                value = _clean(container.get(key))
                if value:
                    identifiers.add(value)
        owner = room.get("owner")
        if isinstance(owner, dict):
            for key in ("web_rid", "webRid"):
                value = _clean(owner.get(key))
                if value:
                    identifiers.add(value)
        return expected_web_rid in identifiers


def format_live_notification(snapshot: LiveSnapshot) -> str:
    platform_name = {"douyu": "斗鱼", "huya": "虎牙", "douyin": "抖音"}.get(
        snapshot.platform, snapshot.platform
    )
    lines = [f"🔴 {platform_name}开播｜{_clean(snapshot.streamer_name)}"]
    if snapshot.title:
        lines.append(f"标题：{_clean(snapshot.title)}")
    if snapshot.category:
        lines.append(f"分区：{_clean(snapshot.category, limit=60)}")
    lines.append(snapshot.live_url)
    return "\n".join(lines)
