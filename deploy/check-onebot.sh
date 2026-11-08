#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! docker compose ps -q bot >/dev/null; then
  echo "bot 容器不存在，请先运行 docker compose up -d bot" >&2
  exit 1
fi

docker compose exec -T bot python - <<'PY'
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import sys
from typing import Any


HOST = "napcat"
PORT = 3001
ECHO = "pwcs2-onebot-check"


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise RuntimeError("connection closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_text_frame(sock: socket.socket, payload: bytes) -> None:
    header = bytearray([0x81])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    mask = os.urandom(4)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    sock.sendall(bytes(header) + mask + masked)


def recv_message(sock: socket.socket) -> dict[str, Any]:
    while True:
        first, second = recv_exact(sock, 2)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", recv_exact(sock, 8))[0]
        mask = recv_exact(sock, 4) if masked else b""
        payload = recv_exact(sock, length)
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        if opcode == 0x8:
            raise RuntimeError("server closed websocket")
        if opcode not in (0x1, 0x2):
            continue
        text = payload.decode("utf-8", errors="replace")
        message = json.loads(text)
        if isinstance(message, dict):
            return message
        raise RuntimeError("unexpected non-object response")


def main() -> int:
    token = os.environ.get("NAPCAT_ONEBOT_TOKEN", "").strip()
    if not token or token.startswith("CHANGE_ME"):
        print("FAIL NAPCAT_ONEBOT_TOKEN 未配置")
        return 1

    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        "GET / HTTP/1.1\r\n"
        f"Host: {HOST}:{PORT}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Authorization: Bearer {token}\r\n"
        "\r\n"
    )
    action = {
        "action": "get_login_info",
        "params": {},
        "echo": ECHO,
    }

    try:
        with socket.create_connection((HOST, PORT), timeout=5) as sock:
            sock.settimeout(5)
            sock.sendall(request.encode("ascii"))
            response = sock.recv(4096).decode("latin1", errors="replace")
            status_line = response.splitlines()[0] if response else ""
            if not status_line.startswith("HTTP/1.1 101 "):
                print(f"FAIL websocket handshake: {status_line or 'NO_RESPONSE'}")
                return 1
            send_text_frame(sock, json.dumps(action, separators=(",", ":")).encode())
            for _ in range(20):
                message = recv_message(sock)
                if message.get("echo") != ECHO:
                    continue
                if message.get("retcode") == 0 or message.get("status") == "ok":
                    print("OK OneBot get_login_info action succeeded")
                    return 0
                print(
                    "FAIL OneBot action failed: "
                    f"status={message.get('status')} retcode={message.get('retcode')}"
                )
                return 1
            print("FAIL OneBot action response timeout")
            return 1
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1


raise SystemExit(main())
PY
