#!/usr/bin/env python3
"""Create a private Steam-only Mihomo configuration without exposing node secrets."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

PROXY_PROFILE = "steam-proxy"
PROXY_IMAGE = "pwcs2-steam-mihomo:1.19.29"
DEFAULT_NODE_FILTER = "(?i)港|hong[ -]?kong|(^|[^a-z])hk([^a-z]|$)"
MAX_SUBSCRIPTION_URL_LENGTH = 4096
MAX_FILTER_LENGTH = 200
MAX_CONFIG_FILE_SIZE = 10 * 1024 * 1024
IMPORTED_PROVIDER_PATH = "./providers/steam-user.yaml"


class ConfigurationError(RuntimeError):
    """A safe-to-display configuration failure."""


def validate_subscription_url(value: str) -> str:
    """Validate a secret HTTPS subscription URL without returning it in errors."""
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > MAX_SUBSCRIPTION_URL_LENGTH
        or any(character.isspace() or ord(character) < 32 for character in normalized)
    ):
        raise ConfigurationError("订阅地址格式不正确")
    try:
        parsed = urlsplit(normalized)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError("订阅地址格式不正确") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not hostname
        or port is None and parsed.netloc.endswith(":")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ConfigurationError("订阅地址必须是有效的 HTTPS URL")
    return normalized


def validate_node_filter(value: str) -> str:
    normalized = value.strip() or DEFAULT_NODE_FILTER
    if (
        len(normalized) > MAX_FILTER_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise ConfigurationError("节点筛选规则格式不正确")
    try:
        re.compile(normalized)
    except re.error as exc:
        raise ConfigurationError("节点筛选规则不是有效的正则表达式") from exc
    return normalized


def yaml_string(value: str) -> str:
    """JSON string literals are valid YAML scalars and avoid hand-written escaping."""
    return json.dumps(value, ensure_ascii=True)


def rotate_authentication_config(text: str, credentials: str) -> str:
    """Replace only the generated mixed-port credential in an existing config."""
    lines = text.splitlines()
    authentication_lines = [
        index for index, line in enumerate(lines) if line == "authentication:"
    ]
    if len(authentication_lines) != 1:
        raise ConfigurationError("现有 Mihomo 配置缺少唯一 authentication 段")
    index = authentication_lines[0]
    if index + 1 >= len(lines) or not re.fullmatch(r"  - .+", lines[index + 1]):
        raise ConfigurationError("现有 Mihomo authentication 格式无法安全轮换")
    lines[index + 1] = f"  - {yaml_string(credentials)}"
    return "\n".join(lines) + "\n"


def _render_config(
    provider_lines: list[str],
    node_filter: str,
    proxy_username: str,
    proxy_password: str,
) -> str:
    credentials = f"{proxy_username}:{proxy_password}"
    provider = "\n".join(f"    {line}" for line in provider_lines)
    return f"""\
mixed-port: 7890
allow-lan: true
bind-address: "*"
authentication:
  - {yaml_string(credentials)}
mode: rule
log-level: warning
ipv6: false
unified-delay: true
tcp-concurrent: true

proxy-providers:
  steam-hk-provider:
{provider}
    filter: {yaml_string(node_filter)}
    exclude-type: direct|dns|wireguard|tailscale|ssh|openvpn
    health-check:
      enable: true
      url: https://api.steampowered.com/
      expected-status: 404
      interval: 60
      timeout: 7000
      lazy: false

proxy-groups:
  - name: STEAM-HK
    type: fallback
    use:
      - steam-hk-provider
    empty-fallback: REJECT
    url: https://api.steampowered.com/
    expected-status: 404
    interval: 60
    lazy: false
    timeout: 7000
    max-failed-times: 2
    disable-udp: true

rules:
  - DOMAIN,api.steampowered.com,STEAM-HK
  - MATCH,REJECT
"""


def render_config(
    subscription_url: str,
    node_filter: str,
    proxy_username: str,
    proxy_password: str,
) -> str:
    """Render a locked Steam-only config backed by an HTTPS subscription."""
    return _render_config(
        [
            "type: http",
            f"url: {yaml_string(subscription_url)}",
            "path: ./providers/steam-hk.yaml",
            "interval: 21600",
            "size-limit: 10485760",
        ],
        node_filter,
        proxy_username,
        proxy_password,
    )


def render_file_config(
    node_filter: str,
    proxy_username: str,
    proxy_password: str,
) -> str:
    """Render a locked Steam-only config backed by an imported provider file."""
    return _render_config(
        [
            "type: file",
            f"path: {IMPORTED_PROVIDER_PATH}",
        ],
        node_filter,
        proxy_username,
        proxy_password,
    )


def read_proxy_config_file(path: Path) -> str:
    """Read a full Clash/Mihomo YAML after conservative format and size checks."""
    try:
        resolved = path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ConfigurationError("找不到指定的配置文件") from exc
    if not resolved.is_file():
        raise ConfigurationError("指定路径不是普通配置文件")
    if resolved.stat().st_size > MAX_CONFIG_FILE_SIZE:
        raise ConfigurationError("配置文件超过 10 MiB，拒绝导入")

    content = resolved.read_bytes()
    if len(content) > MAX_CONFIG_FILE_SIZE:
        raise ConfigurationError("配置文件超过 10 MiB，拒绝导入")
    if b"\x00" in content:
        raise ConfigurationError("配置文件不是有效的文本 YAML")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConfigurationError("配置文件必须使用 UTF-8 编码") from exc

    if not re.search(
        r"(?m)^(?:proxies|\"proxies\"|'proxies')[ \t]*:",
        text,
    ):
        raise ConfigurationError(
            "配置文件没有内联 proxies 节点；请导出包含节点的完整 Clash/Mihomo YAML"
        )
    return text.replace("\r\n", "\n").replace("\r", "\n")


def merge_compose_profiles(current: str) -> str:
    profiles = [
        item.strip()
        for item in re.split(r"[\s,]+", current)
        if item.strip()
    ]
    if PROXY_PROFILE not in profiles:
        profiles.append(PROXY_PROFILE)
    return ",".join(dict.fromkeys(profiles))


def update_env_text(
    text: str,
    *,
    proxy_url: str,
    image: str = PROXY_IMAGE,
) -> str:
    updates = {
        "STEAM_PRESENCE_PROXY_URL": proxy_url,
        "STEAM_MIHOMO_IMAGE": image,
    }
    existing_profiles = ""
    for line in text.splitlines():
        if line.startswith("COMPOSE_PROFILES="):
            existing_profiles = line.split("=", 1)[1].strip()
            break
    updates["COMPOSE_PROFILES"] = merge_compose_profiles(existing_profiles)

    result: list[str] = []
    written: set[str] = set()
    for line in text.splitlines():
        key = line.split("=", 1)[0] if "=" in line else ""
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="安全配置仅供 Steam API 使用的 Mihomo 代理",
    )
    parser.add_argument(
        "--config-file",
        type=Path,
        help="导入包含顶层 proxies: 节点的 Clash/Mihomo YAML",
    )
    parser.add_argument(
        "--node-filter",
        help="节点名称筛选正则；省略时在终端询问",
    )
    parser.add_argument(
        "--rotate-credentials",
        action="store_true",
        help="只轮换本机 mixed-port 认证，不读取或改写订阅节点",
    )
    return parser.parse_args(argv)


def rotate_existing_credentials(root: Path) -> None:
    env_path = root / ".env"
    config_path = root / "data" / "mihomo" / "config.yaml"
    if not env_path.is_file() or not config_path.is_file():
        raise ConfigurationError("Steam 专用 Mihomo 尚未配置")

    proxy_username = f"steam_{secrets.token_hex(8)}"
    proxy_password = secrets.token_urlsafe(24)
    credentials = f"{proxy_username}:{proxy_password}"
    proxy_url = f"http://{credentials}@steam-mihomo:7890"
    config_text = rotate_authentication_config(
        config_path.read_text(encoding="utf-8"),
        credentials,
    )
    current_env = env_path.read_text(encoding="utf-8")
    env_mode = stat.S_IMODE(env_path.stat().st_mode) & 0o600 or 0o600
    write_text_atomic(config_path, config_text)
    write_text_atomic(
        env_path,
        update_env_text(current_env, proxy_url=proxy_url),
        mode=env_mode,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    if args.rotate_credentials:
        if args.config_file is not None or args.node_filter is not None:
            raise ConfigurationError("轮换认证时不能同时重新导入节点")
        rotate_existing_credentials(root)
        print("OK Steam 专用代理认证已轮换；订阅和节点未改动")
        print("下一步：docker compose up -d --force-recreate steam-mihomo bot")
        return 0
    if not sys.stdin.isatty() and (
        args.config_file is None or args.node_filter is None
    ):
        raise ConfigurationError(
            "非交互运行必须同时提供 --config-file 和 --node-filter"
        )

    env_path = root / ".env"
    config_path = root / "data" / "mihomo" / "config.yaml"
    runtime_path = root / "data" / "mihomo" / "runtime"
    imported_provider_path = runtime_path / "providers" / "steam-user.yaml"

    print("该配置只代理 Steam 官方 API，不会修改服务器全局网络或 iWAN。")
    if args.config_file is None:
        print("订阅地址输入不显示，也不会打印到终端；请勿把订阅发到群聊或提交到 Git。")
        subscription_url = validate_subscription_url(
            getpass.getpass("Mihomo/Clash 订阅 URL（输入不显示）: ")
        )
        imported_config = None
    else:
        print("将只读取配置文件中的 proxies 节点；原文件的规则、DNS、TUN 和端口不会启用。")
        imported_config = read_proxy_config_file(args.config_file)
        subscription_url = None
    if args.node_filter is None:
        node_filter = validate_node_filter(
            input(f"香港节点筛选规则 [{DEFAULT_NODE_FILTER}]: ")
        )
    else:
        node_filter = validate_node_filter(args.node_filter)

    proxy_username = f"steam_{secrets.token_hex(8)}"
    proxy_password = secrets.token_urlsafe(24)
    proxy_url = f"http://{proxy_username}:{proxy_password}@steam-mihomo:7890"
    if subscription_url is not None:
        config_text = render_config(
            subscription_url,
            node_filter,
            proxy_username,
            proxy_password,
        )
    else:
        config_text = render_file_config(
            node_filter,
            proxy_username,
            proxy_password,
        )
    current_env = env_path.read_text(encoding="utf-8") if env_path.exists() else ""

    runtime_path.mkdir(parents=True, exist_ok=True)
    runtime_path.chmod(0o700)
    if imported_config is not None:
        imported_provider_path.parent.mkdir(parents=True, exist_ok=True)
        imported_provider_path.parent.chmod(0o700)
        write_text_atomic(imported_provider_path, imported_config)
    write_text_atomic(config_path, config_text)
    env_mode = (
        stat.S_IMODE(env_path.stat().st_mode) & 0o600 or 0o600
        if env_path.exists()
        else 0o600
    )
    write_text_atomic(
        env_path,
        update_env_text(current_env, proxy_url=proxy_url),
        mode=env_mode,
    )

    if imported_config is not None:
        print(
            "OK 节点已私密导入 data/mihomo/runtime/providers/steam-user.yaml；"
            "Steam 专用规则已写入"
        )
    else:
        print("OK Steam 专用 Mihomo 配置已安全写入 data/mihomo/config.yaml 和 .env")
    print("下一步：python3 deploy/install_mihomo.py")
    print("然后：docker compose build steam-mihomo")
    print("再启动：docker compose up -d steam-mihomo")
    print("再执行：docker compose up -d --force-recreate bot")
    print("最后验收：./deploy/check-steam-proxy.sh")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ConfigurationError, OSError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        raise SystemExit(1) from None
