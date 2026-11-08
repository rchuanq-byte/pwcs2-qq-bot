"""Read-only TeamSpeak 3 ServerQuery status client."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Protocol


class TeamSpeakQueryError(RuntimeError):
    """Raised when the internal TeamSpeak query endpoint cannot be read."""


@dataclass(frozen=True, slots=True)
class TeamSpeakMember:
    nickname: str
    away: bool = False


@dataclass(frozen=True, slots=True)
class TeamSpeakChannel:
    channel_id: int
    name: str
    members: tuple[TeamSpeakMember, ...] = ()


@dataclass(frozen=True, slots=True)
class TeamSpeakStatus:
    server_name: str
    max_clients: int
    channels: tuple[TeamSpeakChannel, ...]

    @property
    def online_clients(self) -> int:
        return sum(len(channel.members) for channel in self.channels)


class TeamSpeakStatusProvider(Protocol):
    async def status(self) -> TeamSpeakStatus: ...


def _escape(value: object) -> str:
    replacements = {
        "\\": "\\\\",
        " ": "\\s",
        "|": "\\p",
        "/": "\\/",
        "\a": "\\a",
        "\b": "\\b",
        "\f": "\\f",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
        "\v": "\\v",
    }
    return "".join(replacements.get(character, character) for character in str(value))


def _unescape(value: str) -> str:
    replacements = {
        "s": " ",
        "p": "|",
        "/": "/",
        "\\": "\\",
        "a": "\a",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
    }
    output: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            index += 1
            output.append(replacements.get(value[index], value[index]))
        else:
            output.append(value[index])
        index += 1
    return "".join(output)


def _parse_records(lines: list[str]) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for line in lines:
        for raw_record in line.split("|"):
            record: dict[str, str] = {}
            for field in raw_record.split(" "):
                if not field:
                    continue
                key, separator, value = field.partition("=")
                record[key] = _unescape(value) if separator else ""
            if record:
                records.append(record)
    return records


class _Connection:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        timeout_seconds: float,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.timeout_seconds = timeout_seconds

    async def discard_greeting(self) -> None:
        for _ in range(3):
            line = await asyncio.wait_for(
                self.reader.readline(), timeout=self.timeout_seconds
            )
            if not line or b"Welcome to the TeamSpeak" in line:
                return

    async def command(self, command: str) -> list[str]:
        self.writer.write(command.encode("utf-8") + b"\n")
        await asyncio.wait_for(self.writer.drain(), timeout=self.timeout_seconds)
        lines: list[str] = []
        while True:
            raw = await asyncio.wait_for(
                self.reader.readline(), timeout=self.timeout_seconds
            )
            if not raw:
                raise TeamSpeakQueryError("查询连接意外关闭")
            line = raw.decode("utf-8", errors="replace").strip("\r\n")
            if not line:
                continue
            if line.startswith("error "):
                error = _parse_records([line])[0]
                error_id = error.get("id", "unknown")
                if error_id != "0":
                    message = error.get("msg", "unknown")
                    raise TeamSpeakQueryError(
                        f"TeamSpeak 查询失败（{error_id}: {message}）"
                    )
                return lines
            lines.append(line)


class TeamSpeakStatusClient:
    def __init__(
        self,
        *,
        host: str,
        port: int = 10011,
        username: str = "serveradmin",
        password: str,
        timeout_seconds: float = 5.0,
        cache_ttl_seconds: float = 10.0,
    ) -> None:
        if not host:
            raise ValueError("TeamSpeak query host must not be empty")
        if not 1 <= port <= 65535:
            raise ValueError("TeamSpeak query port must be between 1 and 65535")
        if not username or not password:
            raise ValueError("TeamSpeak query credentials must not be empty")
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout_seconds = timeout_seconds
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cache: tuple[float, TeamSpeakStatus] | None = None
        self._lock = asyncio.Lock()

    async def status(self) -> TeamSpeakStatus:
        now = time.monotonic()
        if self._cache and now - self._cache[0] < self.cache_ttl_seconds:
            return self._cache[1]
        async with self._lock:
            now = time.monotonic()
            if self._cache and now - self._cache[0] < self.cache_ttl_seconds:
                return self._cache[1]
            status = await self._fetch()
            self._cache = (now, status)
            return status

    async def _fetch(self) -> TeamSpeakStatus:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.timeout_seconds,
            )
        except (OSError, TimeoutError) as error:
            raise TeamSpeakQueryError("无法连接 TeamSpeak 内部查询接口") from error

        connection = _Connection(
            reader,
            writer,
            timeout_seconds=self.timeout_seconds,
        )
        try:
            await connection.discard_greeting()
            await connection.command(
                f"login {_escape(self.username)} {_escape(self.password)}"
            )
            await connection.command("use sid=1")
            server_records = _parse_records(await connection.command("serverinfo"))
            channel_records = _parse_records(await connection.command("channellist"))
            client_records = _parse_records(await connection.command("clientlist -away"))
        except (OSError, TimeoutError) as error:
            raise TeamSpeakQueryError("TeamSpeak 查询超时") from error
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

        if not server_records:
            raise TeamSpeakQueryError("TeamSpeak 未返回服务器信息")
        server = server_records[0]
        members_by_channel: dict[int, list[TeamSpeakMember]] = {}
        for client in client_records:
            if client.get("client_type") != "0":
                continue
            try:
                channel_id = int(client.get("cid", "0"))
            except ValueError:
                continue
            nickname = client.get("client_nickname", "未知用户")
            members_by_channel.setdefault(channel_id, []).append(
                TeamSpeakMember(
                    nickname=nickname,
                    away=client.get("client_away") == "1",
                )
            )

        channels: list[TeamSpeakChannel] = []
        for channel in channel_records:
            try:
                channel_id = int(channel["cid"])
            except (KeyError, ValueError):
                continue
            channels.append(
                TeamSpeakChannel(
                    channel_id=channel_id,
                    name=channel.get("channel_name", "未命名频道"),
                    members=tuple(members_by_channel.get(channel_id, ())),
                )
            )

        try:
            max_clients = int(server.get("virtualserver_maxclients", "0"))
        except ValueError:
            max_clients = 0
        return TeamSpeakStatus(
            server_name=server.get("virtualserver_name", "TeamSpeak 3"),
            max_clients=max_clients,
            channels=tuple(channels),
        )


def _clean_text(value: str, *, limit: int) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


def format_teamspeak_status(status: TeamSpeakStatus, *, address: str) -> str:
    lines = [
        f"TS3 实时状态｜在线 {status.online_clients}/{status.max_clients}",
        f"地址：{address}",
    ]
    occupied = [channel for channel in status.channels if channel.members]
    if not occupied:
        lines.append("当前没有成员在线")
        return "\n".join(lines)

    for channel in occupied:
        channel_name = _clean_text(channel.name, limit=40)
        lines.append(f"\n{channel_name}（{len(channel.members)}人）")
        member_names = [
            _clean_text(member.nickname, limit=32) + ("〔离开〕" if member.away else "")
            for member in channel.members
        ]
        lines.append("、".join(member_names))
    return "\n".join(lines)
