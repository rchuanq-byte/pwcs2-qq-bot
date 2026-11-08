#!/usr/bin/env python3
"""Log in to Perfect World and store the returned credentials without printing them."""

from __future__ import annotations

import getpass
import json
import os
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

LOGIN_URL = "https://passport.pwesports.cn/account/login"
PHONE_RE = re.compile(r"^1\d{10}$")
CODE_RE = re.compile(r"^\d{4,8}$")
STEAM_ID_RE = re.compile(r"^7656119\d{10}$")
ENV_KEYS = ("PERFECTWORLD_TOKEN", "PERFECTWORLD_MY_STEAM_ID")


class ConfigurationError(RuntimeError):
    """A safe-to-display configuration failure."""


def parse_login_response(payload: Any) -> tuple[str, str]:
    if not isinstance(payload, dict):
        raise ConfigurationError("完美平台登录接口返回了无效数据")

    if payload.get("code") not in (0, "0"):
        description = " ".join(str(payload.get("description") or "未知错误").split())[:200]
        raise ConfigurationError(f"完美平台登录失败：{description}")

    try:
        account = payload["result"]["loginResult"]["accountInfo"]
        token = str(account["token"]).strip()
        steam_id = str(account["steamId"]).strip()
    except (KeyError, TypeError) as exc:
        raise ConfigurationError("登录成功，但响应中缺少 token 或 SteamID64") from exc

    if (
        not 20 <= len(token) <= 2048
        or not token.isprintable()
        or any(character.isspace() for character in token)
    ):
        raise ConfigurationError("登录成功，但响应中的 token 格式异常")
    if not STEAM_ID_RE.fullmatch(steam_id):
        raise ConfigurationError("登录成功，但响应中的 SteamID64 格式异常")
    return token, steam_id


def update_env_text(text: str, values: dict[str, str]) -> str:
    """Replace keys once, discard duplicates, and preserve unrelated lines."""
    result: list[str] = []
    written: set[str] = set()
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", line)
        key = match.group(1) if match else ""
        if key not in values:
            result.append(line)
            continue
        if key not in written:
            result.append(f"{key}={values[key]}")
            written.add(key)

    missing = [key for key in ENV_KEYS if key not in written]
    if missing and result and result[-1] != "":
        result.append("")
    result.extend(f"{key}={values[key]}" for key in missing)
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


def login(phone: str, security_code: str) -> tuple[str, str]:
    body = json.dumps(
        {"appId": 2, "mobilePhone": phone, "securityCode": security_code},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        LOGIN_URL,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "pwcs2-qq-bot/0.1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read(1_048_577)
    except urllib.error.HTTPError as exc:
        raise ConfigurationError(f"完美平台登录接口返回 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ConfigurationError("无法连接完美平台登录接口") from exc

    if len(raw) > 1_048_576:
        raise ConfigurationError("完美平台登录接口返回数据过大")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("完美平台登录接口返回了无效 JSON") from exc
    return parse_login_response(payload)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    env_path = root / ".env"
    if not sys.stdin.isatty():
        raise ConfigurationError("请在交互式 SSH 终端运行该脚本")

    print("请先在完美电竞 App 登录页请求短信验证码，但不要在 App 内提交验证码。")
    print("手机号和验证码仅发送到 passport.pwesports.cn；token 不会显示在终端。")
    phone = getpass.getpass("完美平台手机号（输入不显示）: ").strip()
    security_code = getpass.getpass("短信验证码（输入不显示）: ").strip()
    if not PHONE_RE.fullmatch(phone):
        raise ConfigurationError("手机号格式不正确")
    if not CODE_RE.fullmatch(security_code):
        raise ConfigurationError("短信验证码格式不正确")

    token, steam_id = login(phone, security_code)
    current = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    updated = update_env_text(
        current,
        {"PERFECTWORLD_TOKEN": token, "PERFECTWORLD_MY_STEAM_ID": steam_id},
    )
    write_env_atomic(env_path, updated)
    print(f"OK 登录成功，已安全写入 .env；关联 SteamID64：{steam_id}")
    print("下一步：docker compose up -d --force-recreate bot")
    print(f"然后验证：./deploy/check-perfectworld.sh {steam_id}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ConfigurationError, OSError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        raise SystemExit(1) from None
