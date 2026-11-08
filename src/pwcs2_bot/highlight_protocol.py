"""Authenticated protocol primitives shared by the highlight API and workers.

The API is intentionally designed for a loopback listener reached through an
SSH tunnel.  HMAC request signing is still required so accidentally publishing
the port does not create an anonymous demo or video upload endpoint.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any

AUTH_SCHEME = "Highlight-HMAC"
PROTOCOL_VERSION = "v1"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
MAX_JSON_BODY_BYTES = 64 * 1024

_WORKER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_NONCE_RE = re.compile(r"[0-9a-f]{32,64}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_JOB_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,63}\Z")


class HighlightProtocolError(ValueError):
    """A safe protocol or authentication validation failure."""


def normalize_cs2_patch(value: object) -> str:
    """Accept only compact or dotted CS2 patch numbers, never arbitrary text."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str | int) or isinstance(value, bool):
        raise HighlightProtocolError("invalid CS2 patch version")
    text = str(value).strip()
    compact = text.replace(".", "")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", text) or not re.fullmatch(
        r"[1-9][0-9]{4,8}", compact
    ):
        raise HighlightProtocolError("invalid CS2 patch version")
    return compact


@dataclass(frozen=True, slots=True)
class AuthenticatedWorker:
    worker_id: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class HighlightSegment:
    start_tick: int
    end_tick: int
    label: str = ""
    score: float = 0.0

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> HighlightSegment:
        try:
            start_tick = int(payload.get("start_tick"))
            end_tick = int(payload.get("end_tick"))
            score = float(payload.get("score") or 0.0)
        except (TypeError, ValueError) as exc:
            raise HighlightProtocolError("invalid highlight segment numbers") from exc
        if start_tick < 0 or end_tick <= start_tick or end_tick - start_tick > 500_000:
            raise HighlightProtocolError("invalid highlight segment tick range")
        if not 0 <= score <= 1_000_000:
            raise HighlightProtocolError("invalid highlight segment score")
        label = " ".join(str(payload.get("label") or "").split())
        if len(label) > 80 or any(ord(char) < 32 for char in label):
            raise HighlightProtocolError("invalid highlight segment label")
        return cls(start_tick=start_tick, end_tick=end_tick, label=label, score=score)

    def as_dict(self) -> dict[str, Any]:
        return {
            "start_tick": self.start_tick,
            "end_tick": self.end_tick,
            "label": self.label,
            "score": self.score,
        }


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def new_nonce() -> str:
    return secrets.token_hex(16)


def _canonical_path(path: str) -> str:
    if not path.startswith("/") or "?" in path or "#" in path or "\r" in path or "\n" in path:
        raise HighlightProtocolError("request path must be an absolute path without a query")
    return path


def canonical_request(
    *,
    worker_id: str,
    timestamp: int,
    nonce: str,
    method: str,
    path: str,
    content_sha256: str,
) -> bytes:
    if _WORKER_ID_RE.fullmatch(worker_id) is None:
        raise HighlightProtocolError("invalid worker id")
    if _NONCE_RE.fullmatch(nonce) is None:
        raise HighlightProtocolError("invalid request nonce")
    if _SHA256_RE.fullmatch(content_sha256) is None:
        raise HighlightProtocolError("invalid content digest")
    normalized_method = method.strip().upper()
    if not normalized_method.isalpha():
        raise HighlightProtocolError("invalid HTTP method")
    normalized_path = _canonical_path(path)
    return (
        f"{PROTOCOL_VERSION}\n{worker_id}\n{timestamp}\n{nonce}\n"
        f"{normalized_method}\n{normalized_path}\n{content_sha256}\n"
    ).encode()


def signed_headers(
    secret: bytes,
    *,
    worker_id: str,
    method: str,
    path: str,
    content_sha256: str = EMPTY_SHA256,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    if len(secret) < 32:
        raise HighlightProtocolError("highlight worker secret must contain at least 32 bytes")
    timestamp = int(time.time()) if timestamp is None else int(timestamp)
    nonce = new_nonce() if nonce is None else nonce
    canonical = canonical_request(
        worker_id=worker_id,
        timestamp=timestamp,
        nonce=nonce,
        method=method,
        path=path,
        content_sha256=content_sha256,
    )
    signature = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
    return {
        "Authorization": f"{AUTH_SCHEME} {worker_id}",
        "X-Highlight-Timestamp": str(timestamp),
        "X-Highlight-Nonce": nonce,
        "X-Highlight-Content-SHA256": content_sha256,
        "X-Highlight-Signature": signature,
    }


class RequestAuthenticator:
    """Validate signed requests and reject stale or replayed nonces."""

    def __init__(
        self,
        worker_secrets: Mapping[str, str | bytes],
        *,
        max_clock_skew_seconds: int = 300,
        now: Callable[[], float] | None = None,
    ) -> None:
        normalized: dict[str, bytes] = {}
        for worker_id, raw_secret in worker_secrets.items():
            if _WORKER_ID_RE.fullmatch(worker_id) is None:
                raise HighlightProtocolError("invalid configured worker id")
            secret = (
                raw_secret.encode() if isinstance(raw_secret, str) else bytes(raw_secret)
            )
            if len(secret) < 32:
                raise HighlightProtocolError(
                    f"highlight worker secret for {worker_id} must contain at least 32 bytes"
                )
            normalized[worker_id] = secret
        if not normalized:
            raise HighlightProtocolError("at least one highlight worker must be configured")
        self._worker_secrets = normalized
        self.max_clock_skew_seconds = max(1, int(max_clock_skew_seconds))
        self._now = now or time.time
        self._nonces: dict[tuple[str, str], int] = {}
        self._lock = Lock()

    def authenticate(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
    ) -> AuthenticatedWorker:
        authorization = str(headers.get("Authorization") or "")
        scheme, separator, worker_id = authorization.partition(" ")
        if separator != " " or scheme != AUTH_SCHEME or worker_id not in self._worker_secrets:
            raise HighlightProtocolError("invalid highlight authorization")
        try:
            timestamp = int(str(headers.get("X-Highlight-Timestamp") or ""))
        except ValueError as exc:
            raise HighlightProtocolError("invalid request timestamp") from exc
        nonce = str(headers.get("X-Highlight-Nonce") or "").casefold()
        content_sha256 = str(headers.get("X-Highlight-Content-SHA256") or "").casefold()
        supplied_signature = str(headers.get("X-Highlight-Signature") or "").casefold()
        if _SHA256_RE.fullmatch(supplied_signature) is None:
            raise HighlightProtocolError("invalid request signature")
        now = int(self._now())
        if abs(now - timestamp) > self.max_clock_skew_seconds:
            raise HighlightProtocolError("request timestamp is outside the allowed clock skew")
        canonical = canonical_request(
            worker_id=worker_id,
            timestamp=timestamp,
            nonce=nonce,
            method=method,
            path=path,
            content_sha256=content_sha256,
        )
        expected = hmac.new(
            self._worker_secrets[worker_id], canonical, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, supplied_signature):
            raise HighlightProtocolError("invalid request signature")
        self._reserve_nonce(worker_id, nonce, timestamp, now)
        return AuthenticatedWorker(worker_id=worker_id, content_sha256=content_sha256)

    def _reserve_nonce(self, worker_id: str, nonce: str, timestamp: int, now: int) -> None:
        oldest = now - self.max_clock_skew_seconds
        key = (worker_id, nonce)
        with self._lock:
            expired = [item for item, seen_at in self._nonces.items() if seen_at < oldest]
            for item in expired:
                self._nonces.pop(item, None)
            if key in self._nonces:
                raise HighlightProtocolError("request nonce has already been used")
            self._nonces[key] = timestamp


def validate_body_digest(body: bytes, expected_sha256: str) -> None:
    if not hmac.compare_digest(sha256_bytes(body), expected_sha256):
        raise HighlightProtocolError("request body digest mismatch")


def validate_job_id(job_id: str) -> str:
    if _JOB_ID_RE.fullmatch(job_id) is None:
        raise HighlightProtocolError("invalid highlight job id")
    return job_id
