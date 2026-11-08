#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT_DIR"

fail() {
  printf 'FAIL %s\n' "$1" >&2
  exit 1
}

docker compose config --quiet || fail "Docker Compose 配置无效"

enabled="$(python3 - <<'PY'
from pathlib import Path

value = ""
path = Path(".env")
if path.is_file():
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, candidate = line.partition("=")
        if separator and key == "HIGHLIGHT_ENABLED":
            value = candidate.strip().casefold()
            break
print("1" if value in {"1", "true", "yes", "on"} else "0")
PY
)"
[[ "$enabled" == "1" ]] || fail "高光功能尚未启用"

for service in highlight-controller highlight-api bot; do
  mapfile -t running_ids < <(docker compose ps --status running -q "$service")
  [[ "${#running_ids[@]}" -eq 1 && -n "${running_ids[0]}" ]] \
    || fail "$service 不是唯一正常运行的容器"
done

if ! timeout --signal=TERM --kill-after=5s 15s \
  docker compose exec -T bot python - <<'PY'
from importlib.metadata import PackageNotFoundError, version

for distribution in ("cs-demo-downloader", "demoparser2"):
    try:
        version(distribution)
    except PackageNotFoundError:
        continue
    raise SystemExit(f"FAIL Bot 镜像不应安装高光组件：{distribution}")
print("OK Bot 镜像未安装 PWA 签名或 Demo 解析组件")
PY
then
  fail "Bot/PWA 供应链隔离自检失败或超时"
fi

if ! timeout --signal=TERM --kill-after=5s 30s \
  docker compose exec -T highlight-controller python - <<'PY'
from importlib.metadata import version
import ipaddress
import os
from pathlib import Path
from urllib.parse import urlsplit

from demoparser2 import DemoParser  # noqa: F401
from pwcs2_bot.pwa_demo_adapter import _load_pwa_functions

if version("cs-demo-downloader") != "1.1.0":
    raise SystemExit("FAIL PWA 下载器版本不正确")
functions = _load_pwa_functions()
access_token = os.environ.get("PERFECTWORLD_PWA_ACCESS_TOKEN", "")
if len(access_token) < 16 or any(character.isspace() for character in access_token):
    raise SystemExit("FAIL PWA access token 未正确配置")
public_ip = os.environ.get("PWCS2_PWA_PUBLIC_IP", "")
try:
    public_address = ipaddress.ip_address(public_ip)
except ValueError:
    raise SystemExit("FAIL PWA 公网 IPv4 未正确配置") from None
if not isinstance(public_address, ipaddress.IPv4Address) or not public_address.is_global:
    raise SystemExit("FAIL PWA 公网 IPv4 必须是可路由地址")
account_steam_id = os.environ.get("PERFECTWORLD_MY_STEAM_ID", "")
if len(account_steam_id) != 17 or not account_steam_id.isdecimal():
    raise SystemExit("FAIL PWA 凭据账号 SteamID64 未正确配置")
url = functions.get_demo_url(
    "PVP@00000000000000000000000000000000",
    access_token,
)
parsed = urlsplit(url)
if parsed.scheme != "https" or parsed.hostname != "pwaweblogin.wmpvp.com":
    raise SystemExit("FAIL PWA 签名器返回了异常地址")
headers = dict(
    functions.build_download_headers(
        account_steam_id,
        public_ip=str(public_address),
        timestamp=1_700_000_000,
    )
)
if not headers or not all(isinstance(key, str) and isinstance(value, str) for key, value in headers.items()):
    raise SystemExit("FAIL PWA 下载请求头生成失败")
intent_database = Path(
    os.environ.get(
        "PWCS2_HIGHLIGHT_INTENT_DATABASE_PATH",
        "/data/highlights/intents.db",
    )
)
if not intent_database.is_file():
    raise SystemExit("FAIL 高光 intent 数据库不存在")
print("OK PWA 签名器与 Demo 解析器可用")
PY
then
  fail "PWA 签名器与 Demo 解析器自检失败或超时"
fi

if ! timeout --signal=TERM --kill-after=5s 20s \
  docker compose exec -T highlight-api python - <<'PY'
import json
import os
import urllib.request
from pathlib import Path

from pwcs2_bot.highlight_protocol import signed_headers

raw = os.environ.get("PWCS2_HIGHLIGHT_WORKER_SECRETS_JSON", "")
try:
    configured = json.loads(raw)
except json.JSONDecodeError:
    raise SystemExit("FAIL Worker 密钥配置不是合法 JSON") from None
if not isinstance(configured, dict) or not configured:
    raise SystemExit("FAIL 尚未配置 Windows Worker")
worker_id, secret = next(iter(configured.items()))
if not isinstance(worker_id, str) or not isinstance(secret, str) or len(secret) < 32:
    raise SystemExit("FAIL Windows Worker 配置无效")

path = "/internal/highlights/v1/health"
request = urllib.request.Request(
    "http://127.0.0.1:18791" + path,
    headers=signed_headers(
        secret.encode("utf-8"),
        worker_id=worker_id,
        method="GET",
        path=path,
    ),
)
with urllib.request.urlopen(request, timeout=5) as response:
    payload = json.load(response)
if response.status != 200 or payload.get("status") != "ok":
    raise SystemExit("FAIL 高光私有接口健康检查失败")

database = Path(os.environ.get("PWCS2_HIGHLIGHT_DATA_DIR", "/data/highlights")) / "jobs.db"
if not database.is_file():
    raise SystemExit("FAIL 高光任务数据库不存在")
print("OK HMAC 私有接口与任务数据库可用")
PY
then
  fail "高光私有接口自检失败或超时"
fi

published="$(docker compose port highlight-api 18791)"
[[ "$published" == 127.0.0.1:* ]] || fail "高光接口没有严格绑定服务器回环地址"

printf 'OK Demo 自动高光服务验收通过\n'
