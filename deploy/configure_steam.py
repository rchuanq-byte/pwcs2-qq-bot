#!/usr/bin/env python3
"""Validate a Steam Web API key and store it in .env without printing it."""

from __future__ import annotations

import getpass
import http.client
import json
import os
import re
import ssl
import stat
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlencode

API_HOST = "api.steampowered.com"
ENV_KEY = "STEAM_WEB_API_KEY"
KEY_RE = re.compile(r"^[A-Fa-f0-9]{32}$")


class ConfigurationError(RuntimeError):
    """A safe-to-display configuration failure."""


def read_env_value(text: str, key: str) -> str:
    prefix = f"{key}="
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return ""


def validate_key(api_key: str, steam_id: str) -> None:
    query = urlencode({"steamids": steam_id})
    connection = http.client.HTTPSConnection(
        API_HOST,
        port=443,
        timeout=20,
        context=ssl.create_default_context(),
    )
    try:
        connection.request(
            "GET",
            f"/ISteamUser/GetPlayerSummaries/v2/?{query}",
            headers={
                "User-Agent": "pwcs2-qq-bot/0.1",
                "x-webapi-key": api_key,
            },
        )
        response = connection.getresponse()
        raw = response.read(1_048_577)
        status = response.status
    except (
        TimeoutError,
        ConnectionError,
        OSError,
        ssl.SSLError,
        http.client.HTTPException,
    ) as exc:
        raise ConfigurationError("无法连接 Steam Web API，请检查服务器网络出口") from exc
    finally:
        connection.close()
    if status in {401, 403}:
        raise ConfigurationError("Steam Web API Key 无效或无权访问")
    if not 200 <= status < 300:
        raise ConfigurationError(f"Steam Web API 返回 HTTP {status}")
    if len(raw) > 1_048_576:
        raise ConfigurationError("Steam Web API 返回数据过大")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("Steam Web API 返回了无效 JSON") from exc
    players = payload.get("response", {}).get("players") if isinstance(payload, dict) else None
    if not isinstance(players, list):
        raise ConfigurationError("Steam Web API 返回格式异常")


def update_env_text(text: str, api_key: str) -> str:
    result: list[str] = []
    written = False
    for line in text.splitlines():
        if re.match(rf"^{ENV_KEY}=", line):
            if not written:
                result.append(f"{ENV_KEY}={api_key}")
                written = True
            continue
        result.append(line)
    if not written:
        if result and result[-1] != "":
            result.append("")
        result.append(f"{ENV_KEY}={api_key}")
    return "\n".join(result) + "\n"


def write_env_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    mode = existing_mode & 0o600 or 0o600
    descriptor, temporary_name = tempfile.mkstemp(prefix=".env.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def main() -> int:
    if not sys.stdin.isatty():
        raise ConfigurationError("请在交互式 SSH 终端运行该脚本")
    root = Path(__file__).resolve().parent.parent
    env_path = root / ".env"
    current = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    steam_id = read_env_value(current, "PERFECTWORLD_MY_STEAM_ID")
    if not steam_id.isdigit():
        steam_id = "76561197960435530"

    print("先在 https://steamcommunity.com/dev/apikey 创建用户 Web API Key。")
    print("API Key 输入不显示，也不会打印到终端。")
    api_key = getpass.getpass("Steam Web API Key（输入不显示）: ").strip()
    if not KEY_RE.fullmatch(api_key):
        raise ConfigurationError("Steam Web API Key 应为 32 位十六进制字符串")

    validate_key(api_key, steam_id)
    write_env_atomic(env_path, update_env_text(current, api_key))
    print("OK Steam Web API Key 已安全写入 .env")
    print("下一步：docker compose up -d --force-recreate bot")
    print("然后检查：docker compose logs --tail=100 bot")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ConfigurationError, OSError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        raise SystemExit(1) from None
