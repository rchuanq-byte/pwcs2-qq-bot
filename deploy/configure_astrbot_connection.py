"""Configure only the OneBot reverse connection; never restart QQ or send messages."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import time
import urllib.request
from pathlib import Path


class WebUI:
    def __init__(self, root):
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.credential = ""
        config = json.loads(
            (root / "data/napcat/config/webui.json").read_text(encoding="utf-8-sig")
        )
        digest = hashlib.sha256((config["token"] + ".napcat").encode()).hexdigest()
        self.credential = self.post("auth/login", {"hash": digest})["Credential"]

    def post(self, route, data):
        headers = {"Content-Type": "application/json"}
        if self.credential:
            headers["Authorization"] = "Bearer " + self.credential
        req = urllib.request.Request(
            "http://127.0.0.1:6099/api/" + route, data=json.dumps(data).encode(), headers=headers
        )
        with self.opener.open(req, timeout=10) as response:
            result = json.load(response)
        if result.get("code") != 0:
            raise RuntimeError("NapCat configuration API did not accept the operation")
        return result.get("data")


def private_write(path, data, owner=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".onebot-update-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        if owner:
            os.chown(name, *owner)
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def account_config_path(root: Path, account: str | None = None) -> Path:
    if account is None:
        raw = (root / ".env").read_text(encoding="utf-8-sig")
        values = re.findall(r"(?m)^\s*NAPCAT_ACCOUNT\s*=([^\r\n]*)", raw)
        if len(values) != 1:
            raise ValueError("Exactly one NAPCAT_ACCOUNT is required")
        account = values[0].strip().strip("\"'")
    if not re.fullmatch(r"[1-9][0-9]{4,11}", account):
        raise ValueError("Invalid QQ account number")
    return root / "data/napcat/config" / f"onebot11_{account}.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "enable", "disable"])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--account", help="QQ account; defaults to NAPCAT_ACCOUNT in .env")
    args = parser.parse_args()
    root = args.root.resolve()
    ui = WebUI(root)
    status = ui.post("QQLogin/CheckLoginStatus", {})
    ready = status.get("isLogin") is True and status.get("coreReady") is True
    print("QQ", "ready" if ready else "login_required")
    if args.action == "status":
        return 0 if ready else 4
    path = account_config_path(root, args.account)
    if ready:
        identity = ui.post("QQLogin/GetQQLoginInfo", {})
        account = path.stem.removeprefix("onebot11_")
        if str(identity.get("uin") or "") != account:
            raise RuntimeError("Logged-in QQ does not match the selected account; config unchanged")
    original = json.loads(path.read_text(encoding="utf-8-sig"))
    if args.backup:
        args.backup.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_write(args.backup / f"onebot-before-{time.time_ns()}.json", original)
    config = json.loads(json.dumps(original))
    network = config["network"]
    clients = network.setdefault("websocketClients", [])
    clients[:] = [c for c in clients if c.get("name") != "niuniu-astrbot"]
    if args.action == "enable":
        servers = [s for s in network.get("websocketServers", []) if s.get("name") == "pwcs2"]
        if not servers or not servers[0].get("token"):
            raise RuntimeError("Existing authenticated OneBot server is required")
        clients.append(
            {
                "name": "niuniu-astrbot",
                "enable": True,
                "url": "ws://bot:6199/ws",
                "token": servers[0]["token"],
                "messagePostFormat": "array",
                "reportSelfMessage": False,
                "reconnectInterval": 5000,
                "heartInterval": 30000,
                "debug": False,
            }
        )
    for server in network.get("websocketServers", []):
        server["debug"] = False
    if ready:
        ui.post("OB11Config/SetConfig", {"config": json.dumps(config, ensure_ascii=False)})
        print("OK OneBot network hot-reloaded; QQ process was not restarted")
        return 0
    info = path.stat()
    private_write(path, config, (info.st_uid, info.st_gid))
    print("PENDING_LOGIN saved reverse configuration; rerun after QR login to hot-reload")
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
