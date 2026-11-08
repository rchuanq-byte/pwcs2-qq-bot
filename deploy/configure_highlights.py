#!/usr/bin/env python3
"""Safely enable the private Demo-to-highlight pipeline."""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from pathlib import Path

HIGHLIGHT_PROFILE = "highlights"
WORKER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{16,4096}$")
WORKER_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
_HIGHLIGHT_DIRECTORIES = ("highlights", "demos", "demo-import")
_OBSOLETE_ENV_KEYS = {"HIGHLIGHT_INTENT_ENABLED"}


class ConfigurationError(RuntimeError):
    """A safe-to-display configuration failure."""


def validate_access_token(value: str) -> str:
    token = value.strip()
    if not TOKEN_PATTERN.fullmatch(token):
        raise ConfigurationError("完美 PWA access_token 格式不正确")
    return token


def validate_worker_id(value: str) -> str:
    worker_id = value.strip()
    if not WORKER_ID_PATTERN.fullmatch(worker_id):
        raise ConfigurationError("Worker ID 只能包含字母、数字、点、下划线和短横线")
    return worker_id


def validate_worker_secret(value: str) -> str:
    secret = value.strip()
    if not WORKER_SECRET_PATTERN.fullmatch(secret):
        raise ConfigurationError("Worker 密钥必须是 32–256 位 Base64URL 字符")
    return secret


def validate_public_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        raise ConfigurationError("PWA 公网 IP 格式不正确") from None
    if not isinstance(address, ipaddress.IPv4Address) or not address.is_global:
        raise ConfigurationError("PWA 公网 IP 必须是可路由的公网 IPv4")
    return str(address)


def validate_account_steam_id(value: str) -> str:
    steam_id = value.strip()
    if len(steam_id) != 17 or not steam_id.isdecimal():
        raise ConfigurationError("PWA 凭据账号 SteamID64 格式不正确")
    return steam_id


def merge_compose_profiles(current: str) -> str:
    profiles = [item for item in re.split(r"[\s,]+", current.strip()) if item]
    if HIGHLIGHT_PROFILE not in profiles:
        profiles.append(HIGHLIGHT_PROFILE)
    return ",".join(dict.fromkeys(profiles))


def _current_value(text: str, key: str) -> str:
    prefix = f"{key}="
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return ""


def existing_worker_secrets(text: str) -> dict[str, str]:
    raw = _current_value(text, "PWCS2_HIGHLIGHT_WORKER_SECRETS_JSON")
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        worker_id: secret
        for worker_id, secret in payload.items()
        if isinstance(worker_id, str)
        and WORKER_ID_PATTERN.fullmatch(worker_id)
        and isinstance(secret, str)
        and WORKER_SECRET_PATTERN.fullmatch(secret)
    }


def update_env_text(
    text: str,
    *,
    access_token: str,
    worker_id: str,
    worker_secret: str,
    public_ip: str = "",
    account_steam_id: str = "",
) -> str:
    worker_secrets = json.dumps(
        {worker_id: worker_secret},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    updates = {
        "HIGHLIGHT_ENABLED": "1",
        "PWCS2_PWA_DEMO_ENABLED": "1",
        "PERFECTWORLD_PWA_ACCESS_TOKEN": access_token,
        "PWCS2_PWA_PUBLIC_IP": public_ip,
        "PERFECTWORLD_MY_STEAM_ID": account_steam_id,
        "PWCS2_HIGHLIGHT_WORKER_SECRETS_JSON": worker_secrets,
        "PWCS2_HIGHLIGHT_DATA_DIR": "/data/highlights",
        "PWCS2_HIGHLIGHT_INTENT_DATABASE_PATH": "/data/highlights/intents.db",
        "PWCS2_HIGHLIGHT_MAX_VIDEO_BYTES": str(24 * 1024 * 1024),
        "PWCS2_HIGHLIGHT_NAPCAT_MEDIA_ROOT": "/app/highlights",
        "PWCS2_HIGHLIGHT_SHARED_OUTPUT_GID": "1000",
        "PWCS2_HIGHLIGHT_MAX_BACKLOG_DEMOS": "2",
        "PWCS2_HIGHLIGHT_MAX_BACKLOG_DEMO_BYTES": str(2 * 1024 * 1024 * 1024),
        "COMPOSE_PROFILES": merge_compose_profiles(
            _current_value(text, "COMPOSE_PROFILES")
        ),
    }
    result: list[str] = []
    written: set[str] = set()
    for line in text.splitlines():
        key = line.split("=", 1)[0] if "=" in line else ""
        if key in _OBSOLETE_ENV_KEYS:
            continue
        if key in updates:
            if key not in written:
                result.append(f"{key}={updates[key]}")
                written.add(key)
            continue
        result.append(line)
    if result and result[-1] != "":
        result.append("")
    for key, value in updates.items():
        if key not in written:
            result.append(f"{key}={value}")
    return "\n".join(result) + "\n"


def prepare_data_directories(root: Path) -> tuple[Path, ...]:
    """Create only the three least-privilege host mounts used by highlights."""

    resolved_root = root.resolve(strict=True)
    data = resolved_root / "data"
    bot_data = data / "bot"
    for parent in (data, bot_data):
        if parent.is_symlink():
            raise ConfigurationError("高光数据目录的父目录不能是符号链接")
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise ConfigurationError("无法创建高光数据目录") from None
        if not parent.is_dir():
            raise ConfigurationError("高光数据目录的父路径不是目录")

    resolved_bot_data = bot_data.resolve(strict=True)
    prepared: list[Path] = []
    for name in _HIGHLIGHT_DIRECTORIES:
        path = bot_data / name
        if path.is_symlink():
            raise ConfigurationError("高光数据目录不能是符号链接")
        try:
            path.mkdir(mode=0o770, exist_ok=True)
            resolved = path.resolve(strict=True)
            if resolved.parent != resolved_bot_data or not resolved.is_dir():
                raise ConfigurationError("高光数据目录超出预期位置")
            if stat.S_IMODE(path.stat().st_mode) != 0o770:
                path.chmod(0o770)
        except ConfigurationError:
            raise
        except OSError:
            raise ConfigurationError("无法安全准备高光数据目录") from None
        prepared.append(resolved)
    return tuple(prepared)


def write_text_atomic(path: Path, text: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
        path.chmod(mode)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="配置私有 Demo 高光服务")
    parser.add_argument("--worker-id", default="")
    parser.add_argument("--public-ip", default="")
    parser.add_argument("--account-steam-id", default="")
    parser.add_argument(
        "--secret-stdin",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    env_path = root / ".env"
    current = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    print("该功能只处理已完成比赛的 Demo；接口仅监听服务器 127.0.0.1。")
    print("Worker 密钥输入不显示，也不会写入日志或 Git。")
    existing_access_token = _current_value(
        current,
        "PERFECTWORLD_PWA_ACCESS_TOKEN",
    ) or _current_value(current, "PERFECTWORLD_TOKEN")
    if existing_access_token:
        access_token = validate_access_token(existing_access_token)
        print("已复用现有完美电竞登录令牌作为 PWA access_token。")
    else:
        print("PWA access_token 输入同样不会显示。")
        access_token = validate_access_token(
            getpass.getpass("完美 PWA access_token（输入不显示）: ")
        )
    public_ip = validate_public_ip(
        args.public_ip or _current_value(current, "PWCS2_PWA_PUBLIC_IP")
    )
    account_steam_id = validate_account_steam_id(
        args.account_steam_id
        or _current_value(current, "PERFECTWORLD_MY_STEAM_ID")
    )
    configured_workers = existing_worker_secrets(current)
    default_worker_id = next(iter(configured_workers), "home-windows")
    worker_id = validate_worker_id(
        args.worker_id
        or input(f"Windows Worker ID [{default_worker_id}]: ")
        or default_worker_id
    )
    reused = False
    if args.secret_stdin:
        entered_secret = sys.stdin.readline().rstrip("\r\n")
        if not entered_secret:
            raise ConfigurationError("未从标准输入收到 Worker 密钥")
    elif worker_id in configured_workers:
        entered_secret = configured_workers[worker_id]
        reused = True
        print("已复用该 Windows Worker 的现有密钥。")
    else:
        entered_secret = getpass.getpass(
            "Windows Worker 共享密钥（留空自动生成，输入不显示）: "
        )
    generated = not reused and not entered_secret.strip()
    worker_secret = validate_worker_secret(
        secrets.token_urlsafe(48) if generated else entered_secret
    )
    prepare_data_directories(root)
    mode = (
        (stat.S_IMODE(env_path.stat().st_mode) & 0o600) or 0o600
        if env_path.exists()
        else 0o600
    )
    write_text_atomic(
        env_path,
        update_env_text(
            current,
            access_token=access_token,
            worker_id=worker_id,
            worker_secret=worker_secret,
            public_ip=public_ip,
            account_steam_id=account_steam_id,
        ),
        mode=mode,
    )
    print("OK 高光服务配置已安全写入 .env")
    print(f"Windows Worker ID：{worker_id}")
    if generated:
        print("下面的密钥只显示这一次，请复制到 Windows Worker 的 setup-secret.ps1：")
        print(worker_secret)
    print(
        "下一步：docker compose up -d --build --force-recreate "
        "bot highlight-controller highlight-api"
    )
    print("然后从 Windows 建立 SSH -L 18791:127.0.0.1:18791 隧道并启动 Worker。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigurationError as exc:
        raise SystemExit(f"FAIL {exc}") from None
