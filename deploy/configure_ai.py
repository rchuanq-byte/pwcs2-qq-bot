#!/usr/bin/env python3
"""Validate a USTC LLM key and store it in .env without printing it."""

from __future__ import annotations

import base64
import getpass
import http.client
import json
import os
import re
import ssl
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

API_HOST = "api.llm.ustc.edu.cn"
ENV_KEYS = (
    "USTC_LLM_API_KEY",
    "USTC_LLM_MODEL",
    "USTC_LLM_VISION_MODEL",
)
DEFAULT_MAIN_MODEL = "deepseek-flash"
DEFAULT_VISION_MODEL = "deepseek-flash"
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class ConfigurationError(RuntimeError):
    """A safe-to-display configuration failure."""


def parse_models_response(payload: Any) -> tuple[str, ...]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ConfigurationError("模型列表接口返回了无效数据")
    models: list[str] = []
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if MODEL_RE.fullmatch(model_id):
            models.append(model_id)
    if not models:
        raise ConfigurationError("当前 API Key 没有可用模型")
    return tuple(dict.fromkeys(models))


def read_configured_proxy(env_path: Path) -> str:
    value = os.environ.get("USTC_LLM_PROXY_URL")
    if value is None and env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("USTC_LLM_PROXY_URL="):
                value = line.split("=", 1)[1].strip().strip("\"'")
                break
    return value or ""


def fetch_models(api_key: str, proxy_url: str = "") -> tuple[str, ...]:
    proxy_headers: dict[str, str] = {}
    host, port = API_HOST, 443
    if proxy_url:
        try:
            proxy = urlsplit(proxy_url)
            if (proxy.scheme != "http" or not proxy.hostname
                    or proxy.path not in {"", "/"} or proxy.query or proxy.fragment
                    or any(c.isspace() or ord(c) < 32 for c in proxy_url)):
                raise ValueError
            host, port = proxy.hostname, proxy.port if proxy.port is not None else 80
            if port == 0:
                raise ValueError
            if host == "steam-mihomo":
                # The host cannot resolve a Docker service name. Resolve only
                # our fixed local container, without putting proxy secrets in argv.
                result = subprocess.run(
                    ["docker", "inspect", "--format",
                     "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                     "pwcs2-qq-bot-steam-mihomo-1"],
                    capture_output=True, text=True, timeout=8, check=True,
                )
                host = result.stdout.strip()
                if not re.fullmatch(r"[0-9.]+", host):
                    raise ValueError
            if proxy.username is not None:
                auth = unquote(proxy.username)+":"+unquote(proxy.password or "")
                proxy_headers["Proxy-Authorization"] = "Basic "+base64.b64encode(
                    auth.encode()).decode("ascii")
        except (ValueError, OSError, subprocess.SubprocessError):
            raise ConfigurationError("校园代理配置无效或私有代理未启动") from None
    connection = http.client.HTTPSConnection(
        host,
        port=port,
        timeout=20,
        context=ssl.create_default_context(),
    )
    if proxy_url:
        # HTTPSConnection wraps TLS for the tunnel destination, preserving the
        # API hostname/certificate verification rather than trusting the proxy.
        connection.set_tunnel(API_HOST, port=443, headers=proxy_headers)
    try:
        connection.request(
            "GET",
            "/v1/models",
            headers={
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "pwcs2-qq-bot/0.1",
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
        raise ConfigurationError(
            "无法连接 USTC AI API，请确认服务器 iWAN 校园网出口仍在线"
        ) from exc
    finally:
        connection.close()
    if status == 401:
        raise ConfigurationError("API Key 无效或已过期")
    if status == 403:
        raise ConfigurationError("API 拒绝访问，请确认账号权限和校园网出口")
    if not 200 <= status < 300:
        raise ConfigurationError(f"模型列表接口返回 HTTP {status}")
    if len(raw) > 1_048_576:
        raise ConfigurationError("模型列表接口返回数据过大")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("模型列表接口返回了无效 JSON") from exc
    return parse_models_response(payload)


def update_env_text(text: str, values: dict[str, str]) -> str:
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


def main() -> int:
    if not sys.stdin.isatty():
        raise ConfigurationError("请在交互式 SSH 终端运行该脚本")
    root = Path(__file__).resolve().parent.parent
    env_path = root / ".env"

    print("请先确保服务器 iWAN 校园网出口正常。")
    print("API Key 输入不显示，也不会打印到终端。")
    api_key = getpass.getpass("USTC AI API Key（输入不显示）: ").strip()
    if (
        not api_key.startswith("sk-")
        or not 10 <= len(api_key) <= 2048
        or not api_key.isprintable()
        or any(character.isspace() for character in api_key)
    ):
        raise ConfigurationError("API Key 格式不正确")

    models = fetch_models(api_key, read_configured_proxy(env_path))
    suggested = DEFAULT_MAIN_MODEL if DEFAULT_MAIN_MODEL in models else models[0]
    print("可用模型：" + "、".join(models[:20]))
    model = input(f"文字主模型 [{suggested}]: ").strip() or suggested
    if not MODEL_RE.fullmatch(model):
        raise ConfigurationError("文字主模型名称格式不正确")
    if model not in models:
        raise ConfigurationError("所选文字主模型不在当前 API Key 的可用模型列表中")

    suggested_vision = (
        DEFAULT_VISION_MODEL if DEFAULT_VISION_MODEL in models else models[0]
    )
    vision_model = (
        input(f"图片模型（与主模型相同则直接看图）[{suggested_vision}]: ").strip()
        or suggested_vision
    )
    if not MODEL_RE.fullmatch(vision_model):
        raise ConfigurationError("图片辅助模型名称格式不正确")
    if vision_model not in models:
        raise ConfigurationError("所选图片辅助模型不在当前 API Key 的可用模型列表中")

    current = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    updated = update_env_text(
        current,
        {
            "USTC_LLM_API_KEY": api_key,
            "USTC_LLM_MODEL": model,
            "USTC_LLM_VISION_MODEL": vision_model,
        },
    )
    write_env_atomic(env_path, updated)
    print(
        "OK API Key 已安全写入 .env；"
        f"文字主模型：{model}；图片辅助模型：{vision_model}"
    )
    print("下一步：docker compose up -d --force-recreate bot")
    print("然后在 QQ 群 @机器人 提问，或发送 #ai <问题>")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ConfigurationError, OSError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        raise SystemExit(1) from None
