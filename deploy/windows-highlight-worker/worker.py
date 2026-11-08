"""Windows worker for preparing and rendering authenticated CS2 demo jobs.

The worker never receives a QQ or Perfect World credential.  It reaches the
loopback-only server API through an SSH local forward and authenticates each
request with an HMAC secret stored in an environment variable.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
AUTH_SCHEME = "Highlight-HMAC"
PROTOCOL_VERSION = "v1"
MAX_RESPONSE_BYTES = 256 * 1024
MAX_CONTEXT_BYTES = 64 * 1024
DEFAULT_MAX_VIDEO_BYTES = 24 * 1024 * 1024
MIN_VIDEO_BYTES = 1024 * 1024
MAX_VIDEO_BYTES = 96 * 1024 * 1024
MAX_CSDM_TOTAL_TICKS = 60 * 64
CSDM_EVENT_PRE_ROLL_TICKS = 4 * 64
CSDM_EVENT_POST_ROLL_TICKS = 8 * 64

_WORKER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_JOB_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,63}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_DEMO_GAME_VERSION_RE = re.compile(r"(?:^|[/\\])csgo_v(?P<version>\d+)(?:[/\\]|$)")
_STEAM_CLIENT_VERSION_RE = re.compile(r"^ClientVersion=(?P<version>\d+)\s*$", re.MULTILINE)
_STEAM_PATCH_VERSION_RE = re.compile(r"^PatchVersion=(?P<version>[\d.]+)\s*$", re.MULTILINE)
_CSDM_SEQUENCE_STARTED_RE = re.compile(r"Demo playback started, sequences (?P<count>\d+)")


class WorkerError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    api_base_url: str
    worker_id: str
    secret_env: str
    workspace_dir: Path
    renderer_mode: str
    csdm_executable: str
    ffmpeg_executable: str
    steam_inf_path: Path | None
    renderer_command: tuple[str, ...]
    renderer_timeout_seconds: int
    lease_seconds: int
    lease_renew_interval_seconds: int
    poll_seconds: int
    max_demo_bytes: int
    max_video_bytes: int
    keep_successful_jobs: bool

    @classmethod
    def load(cls, path: Path) -> WorkerConfig:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkerError(f"cannot read worker config: {exc}") from exc
        if not isinstance(payload, dict):
            raise WorkerError("worker config must be a JSON object")
        removed_watch_keys = tuple(
            key
            for key in (
                "watch_directories",
                "watch_group_id",
                "watch_steam_ids",
                "watch_source",
                "watch_stable_seconds",
                "watch_include_existing",
            )
            if key in payload
        )
        if removed_watch_keys:
            raise WorkerError(
                "local Demo directory watching was removed; delete these config keys: "
                + ", ".join(removed_watch_keys)
            )
        base_url = str(payload.get("api_base_url") or "").rstrip("/") + "/"
        _validate_base_url(base_url)
        worker_id = str(payload.get("worker_id") or "")
        if _WORKER_ID_RE.fullmatch(worker_id) is None:
            raise WorkerError("worker_id contains unsupported characters")
        secret_env = str(payload.get("secret_env") or "NIUNIU_HIGHLIGHT_WORKER_SECRET")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", secret_env):
            raise WorkerError("secret_env must be a safe environment variable name")
        workspace = Path(str(payload.get("workspace_dir") or "./jobs")).expanduser().resolve()
        renderer_mode = str(payload.get("renderer_mode") or "csdm").strip().casefold()
        if renderer_mode not in {"csdm", "custom"}:
            raise WorkerError("renderer_mode must be either csdm or custom")
        csdm_executable = str(payload.get("csdm_executable") or "csdm").strip()
        if not csdm_executable or "\x00" in csdm_executable:
            raise WorkerError("csdm_executable is invalid")
        ffmpeg_executable = str(payload.get("ffmpeg_executable") or "").strip()
        if "\x00" in ffmpeg_executable:
            raise WorkerError("ffmpeg_executable is invalid")
        raw_steam_inf = str(payload.get("steam_inf_path") or "").strip()
        if "\x00" in raw_steam_inf:
            raise WorkerError("steam_inf_path is invalid")
        steam_inf_path = Path(raw_steam_inf).expanduser().resolve() if raw_steam_inf else None
        if steam_inf_path is not None and not steam_inf_path.is_file():
            raise WorkerError("steam_inf_path must point to the active CS2 steam.inf file")
        raw_command = payload.get("renderer_command") or []
        if not isinstance(raw_command, list) or not all(
            isinstance(item, str) and item and "\x00" not in item for item in raw_command
        ):
            raise WorkerError("renderer_command must be a JSON array of arguments")
        if renderer_mode == "custom" and not raw_command:
            raise WorkerError("custom renderer_mode requires renderer_command")
        lease_seconds = _bounded_int(payload.get("lease_seconds", 1800), 60, 3600, "lease")
        lease_renew_interval_seconds = _bounded_int(
            payload.get(
                "lease_renew_interval_seconds",
                min(300, max(15, lease_seconds // 3)),
            ),
            15,
            1200,
            "lease renewal interval",
        )
        if lease_renew_interval_seconds >= lease_seconds:
            raise WorkerError("lease renewal interval must be shorter than the lease")
        return cls(
            api_base_url=base_url,
            worker_id=worker_id,
            secret_env=secret_env,
            workspace_dir=workspace,
            renderer_mode=renderer_mode,
            csdm_executable=csdm_executable,
            ffmpeg_executable=ffmpeg_executable,
            steam_inf_path=steam_inf_path,
            renderer_command=tuple(raw_command),
            renderer_timeout_seconds=_bounded_int(
                payload.get("renderer_timeout_seconds", 1800), 30, 7200, "renderer timeout"
            ),
            lease_seconds=lease_seconds,
            lease_renew_interval_seconds=lease_renew_interval_seconds,
            poll_seconds=_bounded_int(payload.get("poll_seconds", 20), 5, 3600, "poll"),
            max_demo_bytes=_bounded_int(
                payload.get("max_demo_bytes", 2 * 1024**3),
                1024,
                4 * 1024**3,
                "demo size",
            ),
            max_video_bytes=_bounded_int(
                payload.get("max_video_bytes", DEFAULT_MAX_VIDEO_BYTES),
                MIN_VIDEO_BYTES,
                MAX_VIDEO_BYTES,
                "video size",
            ),
            keep_successful_jobs=bool(payload.get("keep_successful_jobs", False)),
        )


def _bounded_int(value: Any, minimum: int, maximum: int, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise WorkerError(f"invalid {label}") from exc
    if not minimum <= number <= maximum:
        raise WorkerError(f"{label} must be between {minimum} and {maximum}")
    return number


def _consume_worker_secret(environment_name: str) -> bytes:
    """Read the API secret once, then remove it from the process environment."""

    return os.environ.pop(environment_name, "").encode("utf-8")


def _renderer_environment(secret_environment_name: str) -> dict[str, str]:
    """Build a child environment without the queue credential."""

    environment = os.environ.copy()
    environment.pop(secret_environment_name, None)
    return environment


def _prepare_windows_command(
    arguments: Sequence[str],
    environment: dict[str, str],
    *,
    platform_name: str = os.name,
) -> list[str]:
    """Launch the packaged CSDM CLI without routing job data through cmd.exe.

    CS Demo Manager's Windows installer exposes ``csdm.cmd`` rather than a
    native ``csdm.exe``.  Passing arbitrary demo/config paths through
    ``cmd.exe /c`` would add a second quoting language and make shell metachar
    handling fragile.  The batch file is only a tiny, stable launcher, so call
    its Electron binary and CLI JavaScript entry point directly instead.
    """

    prepared = list(arguments)
    if platform_name != "nt" or Path(prepared[0]).suffix.casefold() != ".cmd":
        return prepared
    launcher = Path(prepared[0]).expanduser().resolve()
    if launcher.name.casefold() != "csdm.cmd":
        raise WorkerError("only the packaged csdm.cmd launcher is supported")
    electron = launcher.with_name("cs-demo-manager.exe")
    asar = launcher.parent / "resources" / "app.asar"
    cli = asar / "cli.js"
    if not electron.is_file() or not asar.is_file():
        raise WorkerError("csdm.cmd installation is incomplete")
    environment["ELECTRON_RUN_AS_NODE"] = "1"
    return [str(electron), str(cli), *prepared[1:]]


def _validate_base_url(value: str) -> None:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").casefold()
    loopback = host in {"127.0.0.1", "localhost", "::1"}
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith("/")
        or (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback))
    ):
        raise WorkerError("api_base_url must use HTTPS or loopback HTTP without credentials")


def _canonical(
    worker_id: str,
    timestamp: int,
    nonce: str,
    method: str,
    path: str,
    content_sha256: str,
) -> bytes:
    return (
        f"{PROTOCOL_VERSION}\n{worker_id}\n{timestamp}\n{nonce}\n"
        f"{method.upper()}\n{path}\n{content_sha256}\n"
    ).encode()


def _signed_headers(
    secret: bytes,
    worker_id: str,
    method: str,
    path: str,
    content_sha256: str,
) -> dict[str, str]:
    timestamp = int(time.time())
    nonce = secrets.token_hex(16)
    signature = hmac.new(
        secret,
        _canonical(worker_id, timestamp, nonce, method, path, content_sha256),
        hashlib.sha256,
    ).hexdigest()
    return {
        "Authorization": f"{AUTH_SCHEME} {worker_id}",
        "X-Highlight-Timestamp": str(timestamp),
        "X-Highlight-Nonce": nonce,
        "X-Highlight-Content-SHA256": content_sha256,
        "X-Highlight-Signature": signature,
    }


class HighlightAPI:
    def __init__(self, config: WorkerConfig, secret: bytes) -> None:
        if len(secret) < 32:
            raise WorkerError("worker secret must contain at least 32 bytes")
        self.config = config
        self.secret = secret

    def _url(self, path: str) -> str:
        if path.startswith("/"):
            origin = urlsplit(self.config.api_base_url)
            return f"{origin.scheme}://{origin.netloc}{path}"
        return urljoin(self.config.api_base_url, path)

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        lease_token: str = "",
        allow_empty: bool = False,
    ) -> dict[str, Any] | None:
        body = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else b""
        )
        url = self._url(path)
        parsed = urlsplit(url)
        headers = _signed_headers(
            self.secret,
            self.config.worker_id,
            method,
            parsed.path,
            hashlib.sha256(body).hexdigest(),
        )
        if body:
            headers["Content-Type"] = "application/json"
        if lease_token:
            headers["X-Highlight-Lease"] = lease_token
        request = urllib.request.Request(
            url,
            data=body if body else None,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                if response.status == 204 and allow_empty:
                    return None
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            detail = exc.read(2048).decode("utf-8", "replace")
            raise WorkerError(f"highlight API returned HTTP {exc.code}: {detail[:500]}") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise WorkerError(f"cannot reach highlight API: {exc}") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise WorkerError("highlight API response is too large")
        try:
            result = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkerError("highlight API returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise WorkerError("highlight API returned a non-object response")
        return result

    def claim(self) -> dict[str, Any] | None:
        renderer_patch = _installed_cs2_patch(self.config.steam_inf_path)
        return self._request_json(
            "POST",
            "jobs/claim",
            {"lease_seconds": self.config.lease_seconds, "renderer_patch": renderer_patch},
            allow_empty=True,
        )

    def download_demo(self, job: dict[str, Any], destination: Path) -> None:
        demo = job["demo"]
        path = str(demo["download_path"])
        expected_size = int(demo["size"])
        expected_sha = str(demo["sha256"])
        url = self._url(path)
        parsed = urlsplit(url)
        headers = _signed_headers(
            self.secret,
            self.config.worker_id,
            "GET",
            parsed.path,
            EMPTY_SHA256,
        )
        headers["X-Highlight-Lease"] = str(job["lease_token"])
        request = urllib.request.Request(url, headers=headers, method="GET")
        temporary = destination.with_suffix(".dem.part")
        digest = hashlib.sha256()
        written = 0
        try:
            with (
                urllib.request.urlopen(request, timeout=120) as response,
                temporary.open("xb") as out,
            ):
                declared = response.headers.get("Content-Length")
                if declared is not None and int(declared) != expected_size:
                    raise WorkerError("demo Content-Length does not match the claimed job")
                while chunk := response.read(1024 * 1024):
                    written += len(chunk)
                    if written > self.config.max_demo_bytes or written > expected_size:
                        raise WorkerError("downloaded demo exceeds the expected size")
                    digest.update(chunk)
                    out.write(chunk)
        except urllib.error.HTTPError as exc:
            temporary.unlink(missing_ok=True)
            raise WorkerError(f"demo download returned HTTP {exc.code}") from exc
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise WorkerError(f"demo download failed: {exc}") from exc
        if written != expected_size or not hmac.compare_digest(digest.hexdigest(), expected_sha):
            temporary.unlink(missing_ok=True)
            raise WorkerError("downloaded demo failed size or SHA-256 validation")
        temporary.replace(destination)

    def upload_manifest(self, job: dict[str, Any], manifest: dict[str, Any]) -> None:
        self._request_json(
            "PUT",
            f"jobs/{job['job_id']}/manifest",
            manifest,
            lease_token=str(job["lease_token"]),
        )

    def renew_lease(self, job: dict[str, Any]) -> int:
        result = self._request_json(
            "PUT",
            f"jobs/{job['job_id']}/lease",
            {"lease_seconds": self.config.lease_seconds},
            lease_token=str(job["lease_token"]),
        )
        try:
            lease_until = int((result or {}).get("lease_until") or 0)
        except (TypeError, ValueError) as exc:
            raise WorkerError("highlight API returned an invalid renewed lease") from exc
        if lease_until <= int(time.time()):
            raise WorkerError("highlight API returned an expired renewed lease")
        job["lease_until"] = lease_until
        return lease_until

    def upload_video(self, job: dict[str, Any], path: Path) -> None:
        digest, size = _validated_video(path, self.config.max_video_bytes)
        url = self._url(f"jobs/{job['job_id']}/video")
        parsed = urlsplit(url)
        headers = _signed_headers(
            self.secret,
            self.config.worker_id,
            "PUT",
            parsed.path,
            digest,
        )
        headers.update(
            {
                "X-Highlight-Lease": str(job["lease_token"]),
                "Content-Type": "video/mp4",
                "Content-Length": str(size),
            }
        )
        connection_class = (
            http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        )
        connection = connection_class(parsed.hostname, parsed.port, timeout=180)
        try:
            connection.putrequest("PUT", parsed.path)
            for name, value in headers.items():
                connection.putheader(name, value)
            connection.endheaders()
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    connection.send(chunk)
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if response.status < 200 or response.status >= 300:
                raise WorkerError(
                    f"video upload returned HTTP {response.status}: "
                    f"{raw[:500].decode('utf-8', 'replace')}"
                )
            if len(raw) > MAX_RESPONSE_BYTES:
                raise WorkerError("video upload response is too large")
        except OSError as exc:
            raise WorkerError(f"video upload failed: {exc}") from exc
        finally:
            connection.close()

    def fail(self, job: dict[str, Any], error: str, *, terminal: bool = False) -> None:
        self._request_json(
            "POST",
            f"jobs/{job['job_id']}/fail",
            {"error": " ".join(error.split())[:500], "terminal": terminal},
            lease_token=str(job["lease_token"]),
        )


def _validate_job(payload: dict[str, Any], config: WorkerConfig) -> dict[str, Any]:
    job_id = str(payload.get("job_id") or "")
    group_id = str(payload.get("group_id") or "")
    steam_id = str(payload.get("steam_id") or "")
    match_id = " ".join(str(payload.get("match_id") or "").split())
    lease_token = str(payload.get("lease_token") or "")
    demo = payload.get("demo")
    segments = payload.get("segments")
    context = payload.get("context", {})
    if _JOB_ID_RE.fullmatch(job_id) is None:
        raise WorkerError("claimed job has an invalid job_id")
    if not group_id.isdigit() or not 5 <= len(group_id) <= 20:
        raise WorkerError("claimed job has an invalid group_id")
    if not steam_id.isdigit() or len(steam_id) != 17:
        raise WorkerError("claimed job has an invalid SteamID64")
    if not match_id or len(match_id) > 160:
        raise WorkerError("claimed job has an invalid match_id")
    if not 32 <= len(lease_token) <= 128 or any(char.isspace() for char in lease_token):
        raise WorkerError("claimed job has an invalid lease token")
    if not isinstance(demo, dict):
        raise WorkerError("claimed job is missing demo metadata")
    demo_size = _bounded_int(demo.get("size"), 1, config.max_demo_bytes, "claimed demo size")
    demo_sha = str(demo.get("sha256") or "").casefold()
    download_path = str(demo.get("download_path") or "")
    if _SHA256_RE.fullmatch(demo_sha) is None or not download_path.startswith("/"):
        raise WorkerError("claimed job has invalid demo metadata")
    if not isinstance(segments, list) or not 1 <= len(segments) <= 16:
        raise WorkerError("claimed job has an invalid segment list")
    if not isinstance(context, dict):
        raise WorkerError("claimed job has an invalid highlight context")
    try:
        context_json = json.dumps(
            context,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        normalized_context = json.loads(context_json)
    except (TypeError, ValueError, RecursionError) as exc:
        raise WorkerError("claimed job has an invalid highlight context") from exc
    if len(context_json.encode("utf-8")) > MAX_CONTEXT_BYTES:
        raise WorkerError("claimed job highlight context is too large")
    source = str(normalized_context.get("source") or "").strip().casefold()
    if source and source not in {"perfectworld", "valve"}:
        raise WorkerError("claimed job has an unsupported demo source")
    if config.renderer_mode == "csdm" and not source:
        raise WorkerError("claimed CSDM job context is missing source")
    normalized_segments: list[dict[str, Any]] = []
    for item in segments:
        if not isinstance(item, dict):
            raise WorkerError("claimed job has an invalid segment")
        start = _bounded_int(item.get("start_tick"), 0, 10_000_000, "segment start tick")
        end = _bounded_int(item.get("end_tick"), start + 1, start + 500_000, "segment end tick")
        label = " ".join(str(item.get("label") or "").split())[:80]
        try:
            score = float(item.get("score") or 0.0)
        except (TypeError, ValueError) as exc:
            raise WorkerError("claimed job has an invalid segment score") from exc
        if not 0 <= score <= 1_000_000:
            raise WorkerError("claimed job has an invalid segment score")
        normalized_segments.append(
            {
                "start_tick": start,
                "end_tick": end,
                "label": label,
                "score": score,
            }
        )
    return {
        "job_id": job_id,
        "group_id": group_id,
        "match_id": match_id,
        "steam_id": steam_id,
        "lease_token": lease_token,
        "lease_until": int(payload.get("lease_until") or 0),
        "demo": {
            "download_path": download_path,
            "size": demo_size,
            "sha256": demo_sha,
        },
        "segments": normalized_segments,
        "context": normalized_context,
        "source": source,
    }


def _write_render_files(job: dict[str, Any], directory: Path) -> tuple[Path, Path, Path]:
    manifest_path = directory / "render-manifest.json"
    commands_path = directory / "highlight-replay.cfg"
    output_path = directory / "highlight.mp4"
    manifest = {
        "protocol": 1,
        "job_id": job["job_id"],
        "match_id": job["match_id"],
        "steam_id": job["steam_id"],
        "demo_file": "input.dem",
        "commands_file": commands_path.name,
        "output_file": output_path.name,
        "segments": job["segments"],
        "highlight_context": job.get("context", {}),
        "render": {
            "container": "mp4",
            "video_codec": "h264",
            "audio_codec": "aac",
            "max_width": 1920,
            "max_height": 1080,
            "max_fps": 60,
        },
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "// Generated by the Niuniu highlight worker. Do not edit while rendering.",
        'playdemo "input.dem"',
        "demo_pause",
        f"spec_player_by_accountid {job['steam_id']}",
    ]
    for index, segment in enumerate(job["segments"], start=1):
        label = str(segment["label"]).replace('"', "'")
        lines.extend(
            [
                f"// Segment {index}: {label}",
                f"demo_gototick {segment['start_tick']}",
                "demo_pause",
            ]
        )
    commands_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest_path, commands_path, output_path


def _write_csdm_config(
    job: dict[str, Any],
    demo_path: Path,
    directory: Path,
    *,
    ffmpeg_executable: str = "",
) -> tuple[Path, Path]:
    """Write a CS Demo Manager 3.20 compatible video configuration."""

    output_directory = directory / "csdm-output"
    output_directory.mkdir(parents=True, exist_ok=True)
    steam_id = str(job["steam_id"])
    sequences = []
    fitted_segments = _fit_csdm_segments(job)
    total_ticks = sum(
        int(segment["end_tick"]) - int(segment["start_tick"])
        for segment in fitted_segments
    )
    if total_ticks > MAX_CSDM_TOTAL_TICKS:
        raise WorkerError("CSDM highlight sequences exceed the 60 second render budget")
    for number, segment in enumerate(fitted_segments, start=1):
        start_tick = int(segment["start_tick"])
        sequences.append(
            {
                "number": number,
                "startTick": start_tick,
                "endTick": int(segment["end_tick"]),
                "showOnlyDeathNotices": True,
                "deathNoticesDuration": 5,
                "playersOptions": [],
                "playerCameras": [
                    {
                        "tick": start_tick,
                        "playerSteamId": steam_id,
                        "playerName": "",
                    }
                ],
                "cameras": [],
                "showXRay": False,
                "showAssists": True,
                "playerVoicesEnabled": False,
                "recordAudio": True,
            }
        )
    payload = {
        "demoPath": str(demo_path.resolve()),
        "outputFolderPath": str(output_directory.resolve()),
        "recordingSystem": "CS",
        "recordingOutput": "video",
        "encoderSoftware": "FFmpeg",
        "width": 1280,
        "height": 720,
        "framerate": 30,
        "trueView": False,
        "closeGameAfterRecording": True,
        "concatenateSequences": True,
        "ffmpegSettings": {
            "audioBitrate": 128,
            "constantRateFactor": 26,
            "customLocationEnabled": bool(ffmpeg_executable),
            "customExecutableLocation": ffmpeg_executable,
            "videoContainer": "mp4",
            "videoCodec": "libx264",
            "audioCodec": "aac",
            "inputParameters": "",
            "outputParameters": (
                "-pix_fmt yuv420p -crf 26 -maxrate 3M -bufsize 6M -movflags +faststart"
            ),
        },
        "sequences": sequences,
    }
    path = directory / "csdm-video.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path, output_directory


def _fit_csdm_segments(job: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Split an unusually long multi-kill round into concise event shots."""

    segments = [dict(segment) for segment in job.get("segments", ())]
    total_ticks = sum(
        int(segment["end_tick"]) - int(segment["start_tick"])
        for segment in segments
    )
    if total_ticks <= MAX_CSDM_TOTAL_TICKS:
        return segments

    context = job.get("context")
    if not isinstance(context, Mapping):
        context = {}
    highlight = context.get("highlight")
    if isinstance(highlight, Mapping):
        context = highlight
    events = context.get("events")
    if not isinstance(events, list):
        events = []

    anchors: list[tuple[int, int]] = []
    for index, segment in enumerate(segments):
        start_tick = int(segment["start_tick"])
        end_tick = int(segment["end_tick"])
        for event in events:
            if not isinstance(event, Mapping):
                continue
            try:
                tick = int(event.get("tick"))
            except (TypeError, ValueError):
                continue
            if start_tick <= tick <= end_tick:
                anchors.append((index, tick))
    anchors = list(dict.fromkeys(anchors))
    if not anchors or len(anchors) > 16:
        raise WorkerError("CSDM highlight sequences exceed the 60 second render budget")

    per_event_budget = min(
        CSDM_EVENT_PRE_ROLL_TICKS + CSDM_EVENT_POST_ROLL_TICKS,
        MAX_CSDM_TOTAL_TICKS // len(anchors),
    )
    before = min(CSDM_EVENT_PRE_ROLL_TICKS, max(1, per_event_budget // 3))
    after = max(1, per_event_budget - before)
    compact: list[dict[str, Any]] = []
    for index, tick in anchors:
        original = segments[index]
        start_tick = max(int(original["start_tick"]), tick - before)
        end_tick = min(int(original["end_tick"]), tick + after)
        if end_tick <= start_tick:
            end_tick = start_tick + 1
        candidate = {
            "start_tick": start_tick,
            "end_tick": end_tick,
            "label": original.get("label", ""),
            "score": original.get("score", 0.0),
            "source_index": index,
        }
        if (
            compact
            and compact[-1]["source_index"] == index
            and start_tick <= int(compact[-1]["end_tick"])
        ):
            compact[-1]["end_tick"] = max(int(compact[-1]["end_tick"]), end_tick)
        else:
            compact.append(candidate)

    for segment in compact:
        segment.pop("source_index", None)
    return compact


def _csdm_analyze_arguments(config: WorkerConfig, demo_path: Path, source: str) -> list[str]:
    if source not in {"perfectworld", "valve"}:
        raise WorkerError("CSDM analyze requires perfectworld or valve source")
    return [
        config.csdm_executable,
        "analyze",
        str(demo_path),
        "--source",
        source,
        "--force",
    ]


def _csdm_video_arguments(config: WorkerConfig, config_path: Path) -> list[str]:
    return [config.csdm_executable, "video", "--config-file", str(config_path)]


def _csdm_plugin_log_path(steam_inf_path: Path | None) -> Path:
    """Locate the log written by CSDM's in-game server plugin."""

    if steam_inf_path is None:
        raise WorkerError("steam_inf_path is required for reliable CSDM recording")
    resolved = steam_inf_path.expanduser().resolve()
    if resolved.name.casefold() != "steam.inf" or resolved.parent.name.casefold() != "csgo":
        raise WorkerError("steam_inf_path must point to the installed game/csgo/steam.inf")
    return resolved.parent.parent / "bin" / "win64" / "csdm.log"


def _log_checkpoint(path: Path) -> tuple[int, int]:
    """Return an inode/length pair so old successful hook messages are ignored."""

    try:
        stat = path.stat()
    except OSError:
        return (0, 0)
    return (int(stat.st_ino), int(stat.st_size))


def _read_log_since(path: Path, checkpoint: tuple[int, int]) -> str:
    """Read only data produced after a renderer launch, with a bounded tail."""

    try:
        stat = path.stat()
        offset = checkpoint[1] if int(stat.st_ino) == checkpoint[0] else 0
        if stat.st_size < offset:
            offset = 0
        maximum = 2 * 1024 * 1024
        if stat.st_size - offset > maximum:
            offset = stat.st_size - maximum
        with path.open("rb") as handle:
            handle.seek(offset)
            payload = handle.read(maximum + 1)
    except OSError:
        return ""
    return payload[-maximum:].decode("utf-8", "replace")


def _focus_cs2_window(
    hwnd: int,
    *,
    user32: Any,
    kernel32: Any,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    timeout_seconds: float = 8.0,
) -> bool:
    """Reliably foreground a CS2 window despite Windows' focus-stealing guard.

    A hidden Python/PowerShell child is normally not allowed to call
    ``SetForegroundWindow`` directly.  Temporarily joining its input queue to
    the foreground and CS2 threads gives Windows a legitimate activation path;
    the Alt key transition is the documented user-input hint used by desktop
    automation when the foreground lock is still active.
    """

    vk_menu = 0x12
    keyeventf_keyup = 0x0002
    sw_restore = 9
    sw_show = 5
    swp_nomove = 0x0002
    swp_nosize = 0x0001
    swp_showwindow = 0x0040
    hwnd_topmost = -1
    hwnd_notopmost = -2
    deadline = monotonic() + max(1.0, float(timeout_seconds))
    while monotonic() < deadline:
        if int(user32.GetForegroundWindow() or 0) == hwnd:
            return True
        user32.ShowWindowAsync(hwnd, sw_restore if user32.IsIconic(hwnd) else sw_show)
        current_thread = int(kernel32.GetCurrentThreadId() or 0)
        target_thread = int(user32.GetWindowThreadProcessId(hwnd, None) or 0)
        foreground = int(user32.GetForegroundWindow() or 0)
        foreground_thread = int(
            user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        )
        attached: list[int] = []
        for thread_id in dict.fromkeys((foreground_thread, target_thread)):
            if thread_id and current_thread and thread_id != current_thread:
                if user32.AttachThreadInput(current_thread, thread_id, True):
                    attached.append(thread_id)
        alt_pressed = False
        try:
            user32.keybd_event(vk_menu, 0, 0, 0)
            alt_pressed = True
            user32.BringWindowToTop(hwnd)
            user32.SetWindowPos(
                hwnd,
                hwnd_topmost,
                0,
                0,
                0,
                0,
                swp_nomove | swp_nosize | swp_showwindow,
            )
            user32.SetWindowPos(
                hwnd,
                hwnd_notopmost,
                0,
                0,
                0,
                0,
                swp_nomove | swp_nosize | swp_showwindow,
            )
            user32.SetForegroundWindow(hwnd)
            user32.SetActiveWindow(hwnd)
            user32.SetFocus(hwnd)
        finally:
            if alt_pressed:
                user32.keybd_event(vk_menu, 0, keyeventf_keyup, 0)
            for thread_id in reversed(attached):
                user32.AttachThreadInput(current_thread, thread_id, False)
        if int(user32.GetForegroundWindow() or 0) == hwnd:
            return True
        sleep(0.25)
    return int(user32.GetForegroundWindow() or 0) == hwnd


def _send_cs2_console_command(
    command: str,
    *,
    platform_name: str = os.name,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Focus CS2 and inject one trusted console command using Win32 SendInput."""

    if platform_name != "nt":
        raise WorkerError("automatic CSDM demo reload is supported only on Windows")
    if not command or any(character in command for character in "\r\n"):
        raise WorkerError("CS2 console command is invalid")

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handles: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindowAsync.restype = wintypes.BOOL
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.keybd_event.argtypes = [
        wintypes.BYTE,
        wintypes.BYTE,
        wintypes.DWORD,
        ctypes.c_size_t,
    ]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.SetActiveWindow.argtypes = [wintypes.HWND]
    user32.SetActiveWindow.restype = wintypes.HWND
    user32.SetFocus.argtypes = [wintypes.HWND]
    user32.SetFocus.restype = wintypes.HWND
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    @callback_type
    def collect_window(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, len(title))
        normalized = title.value.casefold()
        if "counter-strike" not in normalized and "反恐精英" not in normalized:
            return True
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        process = kernel32.OpenProcess(0x1000, False, process_id.value)
        if not process:
            return True
        try:
            image = ctypes.create_unicode_buffer(32768)
            image_length = wintypes.DWORD(len(image))
            if not kernel32.QueryFullProcessImageNameW(
                process,
                0,
                image,
                ctypes.byref(image_length),
            ):
                return True
            if Path(image.value).name.casefold() == "cs2.exe":
                handles.append(int(hwnd))
        finally:
            kernel32.CloseHandle(process)
        return True

    deadline = monotonic() + 30.0
    while not handles:
        user32.EnumWindows(collect_window, 0)
        if handles:
            break
        if monotonic() >= deadline:
            raise WorkerError("CSDM connected but the CS2 window was not found")
        sleep(0.5)

    hwnd = handles[0]
    if not _focus_cs2_window(
        hwnd,
        user32=user32,
        kernel32=kernel32,
        monotonic=monotonic,
        sleep=sleep,
    ):
        raise WorkerError("cannot focus the CS2 window for the CSDM demo reload")

    ULONG_PTR = wintypes.WPARAM

    class KeyboardInput(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class MouseInput(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HardwareInput(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class InputUnion(ctypes.Union):
        _fields_ = [
            ("mi", MouseInput),
            ("ki", KeyboardInput),
            ("hi", HardwareInput),
        ]

    class Input(ctypes.Structure):
        _anonymous_ = ("value",)
        _fields_ = [("type", wintypes.DWORD), ("value", InputUnion)]

    def send_key(virtual_key: int) -> None:
        events = (Input * 2)(
            Input(type=1, ki=KeyboardInput(virtual_key, 0, 0, 0, 0)),
            Input(type=1, ki=KeyboardInput(virtual_key, 0, 0x0002, 0, 0)),
        )
        if user32.SendInput(2, events, ctypes.sizeof(Input)) != 2:
            raise WorkerError("Windows rejected the CS2 console key input")

    def send_text(value: str) -> None:
        events: list[Input] = []
        for character in value:
            codepoint = ord(character)
            if codepoint > 0xFFFF:
                raise WorkerError("CS2 console command contains an unsupported character")
            events.extend(
                (
                    Input(type=1, ki=KeyboardInput(0, codepoint, 0x0004, 0, 0)),
                    Input(type=1, ki=KeyboardInput(0, codepoint, 0x0006, 0, 0)),
                )
            )
        batch = (Input * len(events))(*events)
        if user32.SendInput(len(events), batch, ctypes.sizeof(Input)) != len(events):
            raise WorkerError("Windows rejected the CS2 console text input")

    send_key(0xC0)  # VK_OEM_3, the default CS2 developer-console key.
    sleep(0.3)
    send_text(command)
    send_key(0x0D)  # VK_RETURN


def _ensure_csdm_demo_hook(
    process: Any,
    demo_path: Path,
    log_path: Path,
    checkpoint: tuple[int, int],
    *,
    command_sender: Callable[[str], None] = _send_cs2_console_command,
    log_reader: Callable[[Path, tuple[int, int]], str] = _read_log_since,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    # Current CS2 builds may load client.dll noticeably after CSDM opens its
    # WebSocket. Give the plugin time to attach before issuing the old reload
    # fallback, which otherwise risks replaying while the console is visible.
    connection_grace_seconds: float = 20.0,
    timeout_seconds: float = 120.0,
) -> None:
    """Wait for CSDM's hook, with one legacy playback-race recovery attempt."""

    resolved = demo_path.resolve()
    demo_text = str(resolved).replace("\\", "/")
    if not resolved.is_file() or any(character in demo_text for character in '"\r\n'):
        raise WorkerError("cannot safely reload the CSDM demo path")
    # Close the developer-console overlay before playback begins.  Leaving it
    # open causes CSDM's in-game capture hook to record the console for the
    # complete sequence even though the demo itself is playing underneath it.
    command = f'disconnect; hideconsole; playdemo "{demo_text}"'
    deadline = monotonic() + max(1.0, timeout_seconds)
    connected_at: float | None = None
    command_sent = False
    while True:
        return_code = process.poll()
        if return_code is not None:
            raise WorkerError(
                f"CSDM exited with code {return_code} before its demo hook became ready"
            )
        text = log_reader(log_path, checkpoint)
        started_counts = [
            int(match.group("count")) for match in _CSDM_SEQUENCE_STARTED_RE.finditer(text)
        ]
        hooked = "Hooked FrameStageNotify" in text
        if hooked and (not started_counts or any(count > 0 for count in started_counts)):
            return
        if "JSON sequences file not found" in text:
            raise WorkerError("CSDM did not create the recording actions file")
        now = monotonic()
        if "Connected to WebSocket server." in text and connected_at is None:
            connected_at = now
        if (
            not command_sent
            and connected_at is not None
            and now - connected_at >= max(0.0, connection_grace_seconds)
        ):
            command_sender(command)
            command_sent = True
        if now >= deadline:
            detail = "after one Demo reload" if command_sent else "before CSDM connected"
            raise WorkerError(f"CSDM demo hook was not ready {detail}")
        sleep(0.5)


def _stop_process(
    process: Any,
    *,
    platform_name: str = os.name,
    taskkill_runner: Callable[..., Any] = subprocess.run,
) -> None:
    """Best-effort cleanup for a renderer and all of its child processes.

    CSDM starts CS2 and FFmpeg as descendants.  Killing only the direct CSDM
    process can therefore leave both resource-heavy programs running.  On
    Windows, ``taskkill /T`` terminates the complete tree.  The PID is accepted
    only when it is a positive, concrete ``int`` and the command is always
    executed without a shell.

    Cleanup must never mask the exception that caused it, including process
    cancellation, so every operation here is deliberately best-effort.
    """

    try:
        pid = process.pid
    except BaseException:
        pid = None
    if platform_name == "nt" and isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        try:
            taskkill_runner(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=15,
            )
            process.wait(timeout=10)
            return
        except BaseException:
            pass

    try:
        process.terminate()
        process.wait(timeout=10)
        return
    except BaseException:
        pass
    try:
        process.kill()
        process.wait(timeout=5)
    except BaseException:
        pass


def _run_command(
    arguments: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    renew_lease: Callable[[], Any] | None = None,
    renew_interval_seconds: int = 300,
    secret_environment_name: str = "NIUNIU_HIGHLIGHT_WORKER_SECRET",
    popen_factory: Callable[..., Any] = subprocess.Popen,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    process_tree_platform: str = os.name,
    taskkill_runner: Callable[..., Any] = subprocess.run,
    startup_action: Callable[[Any], None] | None = None,
) -> None:
    """Run an argv command and keep a remote lease alive while it is active."""

    if not arguments or any(not isinstance(item, str) or not item for item in arguments):
        raise WorkerError("renderer command contains an invalid argument")
    environment = _renderer_environment(secret_environment_name)
    prepared_arguments = _prepare_windows_command(
        arguments,
        environment,
        platform_name=process_tree_platform,
    )
    try:
        process = popen_factory(
            prepared_arguments,
            cwd=cwd,
            shell=False,
            stdin=subprocess.DEVNULL,
            env=environment,
        )
    except OSError as exc:
        raise WorkerError(f"cannot start renderer command: {exc}") from exc
    completed = False
    try:
        if startup_action is not None:
            startup_action(process)
        started = monotonic()
        deadline = started + max(1, int(timeout_seconds))
        next_renewal = started + max(1, int(renew_interval_seconds))
        while True:
            return_code = process.poll()
            if return_code is not None:
                if return_code != 0:
                    raise WorkerError(f"renderer command exited with code {return_code}")
                completed = True
                return
            now = monotonic()
            if now >= deadline:
                raise WorkerError("renderer exceeded its configured timeout")
            if renew_lease is not None and now >= next_renewal:
                try:
                    renew_lease()
                except Exception as exc:
                    raise WorkerError(f"cannot renew highlight lease: {exc}") from exc
                next_renewal = now + max(1, int(renew_interval_seconds))
            sleep(min(1.0, max(0.05, deadline - now)))
    finally:
        if not completed:
            _stop_process(
                process,
                platform_name=process_tree_platform,
                taskkill_runner=taskkill_runner,
            )


def _mp4_snapshot(directory: Path) -> dict[Path, tuple[int, int]]:
    snapshot: dict[Path, tuple[int, int]] = {}
    if not directory.is_dir():
        return snapshot
    for candidate in directory.rglob("*"):
        if candidate.suffix.casefold() != ".mp4" or not candidate.is_file():
            continue
        if candidate.is_symlink():
            raise WorkerError("CSDM output contains a symbolic link")
        resolved = candidate.resolve()
        stat = resolved.stat()
        snapshot[resolved] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


def _promote_unique_new_mp4(
    output_directory: Path,
    before: Mapping[Path, tuple[int, int]],
    destination: Path,
    *,
    max_video_bytes: int,
) -> Path:
    after = _mp4_snapshot(output_directory)
    candidates = [path for path, fingerprint in after.items() if before.get(path) != fingerprint]
    if len(candidates) != 1:
        raise WorkerError(f"CSDM must create exactly one new MP4, found {len(candidates)}")
    source = candidates[0]
    _validated_video(source, max_video_bytes)
    staging = destination.with_suffix(destination.suffix + ".part")
    staging.unlink(missing_ok=True)
    destination.unlink(missing_ok=True)
    os.replace(source, staging)
    os.replace(staging, destination)
    return destination


def _renderer_arguments(
    template: tuple[str, ...],
    *,
    demo: Path,
    manifest: Path,
    commands: Path,
    output: Path,
) -> list[str]:
    if not template:
        raise WorkerError("renderer_command is empty; configure an OBS/CS2 renderer wrapper")
    replacements = {
        "{demo}": str(demo),
        "{manifest}": str(manifest),
        "{commands}": str(commands),
        "{output}": str(output),
    }
    rendered: list[str] = []
    for argument in template:
        value = argument
        for placeholder, replacement in replacements.items():
            value = value.replace(placeholder, replacement)
        if "{" in value or "}" in value:
            raise WorkerError(f"renderer argument has an unsupported placeholder: {argument}")
        rendered.append(value)
    return rendered


def _hash_file(path: Path, maximum: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                total += len(chunk)
                if total > maximum:
                    raise WorkerError("file exceeds configured size limit")
                digest.update(chunk)
    except OSError as exc:
        raise WorkerError(f"cannot read file: {exc}") from exc
    if total <= 0:
        raise WorkerError("file is empty")
    return digest.hexdigest(), total


def _validated_video(path: Path, maximum: int) -> tuple[str, int]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise WorkerError(f"cannot inspect rendered video: {exc}") from exc
    if size > maximum:
        limit_mib = maximum / (1024 * 1024)
        raise WorkerError(
            f"rendered video exceeds the configured OneBot inline limit ({limit_mib:g} MiB)"
        )
    digest, size = _hash_file(path, maximum)
    if not _has_mp4_signature(path):
        raise WorkerError("renderer output is not a valid MP4")
    return digest, size


def _has_mp4_signature(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            header = handle.read(32)
    except OSError:
        return False
    return len(header) >= 12 and header[4:8] == b"ftyp"


def _render_csdm(
    job: dict[str, Any],
    config: WorkerConfig,
    directory: Path,
    demo_path: Path,
    output_path: Path,
    *,
    api: HighlightAPI | None,
) -> None:
    source = str(job.get("source") or "").casefold()
    renew = (lambda: api.renew_lease(job)) if api is not None else None
    _run_command(
        _csdm_analyze_arguments(config, demo_path, source),
        cwd=directory,
        timeout_seconds=min(config.renderer_timeout_seconds, 900),
        renew_lease=renew,
        renew_interval_seconds=config.lease_renew_interval_seconds,
        secret_environment_name=config.secret_env,
    )
    csdm_config, output_directory = _write_csdm_config(
        job,
        demo_path,
        directory,
        ffmpeg_executable=config.ffmpeg_executable,
    )
    before = _mp4_snapshot(output_directory)
    startup_action: Callable[[Any], None] | None = None
    if os.name == "nt":
        log_path = _csdm_plugin_log_path(config.steam_inf_path)
        checkpoint = _log_checkpoint(log_path)

        def ensure_demo_hook(process: Any) -> None:
            _ensure_csdm_demo_hook(
                process,
                demo_path,
                log_path,
                checkpoint,
            )

        startup_action = ensure_demo_hook
    _run_command(
        _csdm_video_arguments(config, csdm_config),
        cwd=directory,
        timeout_seconds=config.renderer_timeout_seconds,
        renew_lease=renew,
        renew_interval_seconds=config.lease_renew_interval_seconds,
        secret_environment_name=config.secret_env,
        startup_action=startup_action,
    )
    _promote_unique_new_mp4(
        output_directory,
        before,
        output_path,
        max_video_bytes=config.max_video_bytes,
    )


def _render_custom(
    job: dict[str, Any],
    config: WorkerConfig,
    directory: Path,
    demo_path: Path,
    manifest_path: Path,
    commands_path: Path,
    output_path: Path,
    *,
    api: HighlightAPI | None,
) -> None:
    arguments = _renderer_arguments(
        config.renderer_command,
        demo=demo_path,
        manifest=manifest_path,
        commands=commands_path,
        output=output_path,
    )
    renew = (lambda: api.renew_lease(job)) if api is not None else None
    _run_command(
        arguments,
        cwd=directory,
        timeout_seconds=config.renderer_timeout_seconds,
        renew_lease=renew,
        renew_interval_seconds=config.lease_renew_interval_seconds,
        secret_environment_name=config.secret_env,
    )


def _table_records(value: Any) -> list[Mapping[str, Any]]:
    if hasattr(value, "to_dicts"):
        value = value.to_dicts()
    elif hasattr(value, "to_dict"):
        try:
            value = value.to_dict(orient="records")
        except TypeError:
            value = value.to_dict("records")
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise WorkerError("demoparser2 returned an unsupported event table")
    return [item for item in value if isinstance(item, Mapping)]


def _first_value(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = row.get(name)
        if value is not None and value != "":
            return value
    return None


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "y"}
    return bool(value)


def _steam_text(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value or "").strip()


def _nearest_steam_inf(demo_path: Path) -> Path | None:
    """Find steam.inf when a local Demo lives inside the active CS2 tree."""

    for directory in (demo_path.parent, *demo_path.parents):
        candidate = directory / "steam.inf"
        if candidate.is_file():
            return candidate
    return None


def _installed_cs2_patch(steam_inf_path: Path | None) -> str:
    """Read on every manual worker cycle, including when there are no jobs."""
    if steam_inf_path is None:
        return ""
    try:
        content = steam_inf_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise WorkerError(f"cannot read installed CS2 version: {exc}") from exc
    match = _STEAM_PATCH_VERSION_RE.search(content)
    patch = match.group("version").replace(".", "") if match is not None else ""
    if not re.fullmatch(r"[1-9][0-9]{4,8}", patch):
        raise WorkerError("cannot read PatchVersion from the configured steam.inf")
    return patch


def _ensure_demo_matches_installed_game(
    demo_path: Path,
    *,
    parser_factory: Callable[[str], Any] | None = None,
    steam_inf_path: Path | None = None,
) -> tuple[str, str] | None:
    """Reject a stale local Demo before CSDM launches an incompatible CS2 build.

    Valve embeds the server build in the Demo header's ``game_directory`` and
    writes the installed client build to ``steam.inf``.  The check is only
    applied when both values can be located, so externally downloaded queued
    demos keep working even when their file is outside the Steam library.
    """

    if parser_factory is None:
        try:
            from demoparser2 import DemoParser
        except ImportError as exc:
            raise WorkerError(
                "demoparser2 is not installed; run setup-worker.ps1 to verify dependencies"
            ) from exc
        parser_factory = DemoParser
    steam_inf = steam_inf_path or _nearest_steam_inf(demo_path)
    if steam_inf is None or not steam_inf.is_file():
        return None
    try:
        header = parser_factory(str(demo_path)).parse_header()
        game_directory = str(header.get("game_directory") or "")
        demo_match = _DEMO_GAME_VERSION_RE.search(game_directory)
        steam_inf_text = steam_inf.read_text(encoding="utf-8", errors="replace")
        installed_match = _STEAM_CLIENT_VERSION_RE.search(steam_inf_text)
        demo_patch = re.sub(r"\D", "", str(header.get("patch_version") or ""))
        installed_patch_match = _STEAM_PATCH_VERSION_RE.search(steam_inf_text)
        installed_patch = (
            re.sub(r"\D", "", installed_patch_match.group("version"))
            if installed_patch_match is not None
            else ""
        )
    except (OSError, AttributeError, TypeError, ValueError) as exc:
        raise WorkerError(f"cannot verify Demo compatibility: {exc}") from exc
    comparisons: list[tuple[str, str]] = []
    if demo_match is not None and installed_match is not None:
        comparisons.append((demo_match.group("version"), installed_match.group("version")))
    if demo_patch and installed_patch:
        comparisons.append((demo_patch, installed_patch))
    for demo_version, installed_version in comparisons:
        if demo_version != installed_version:
            raise WorkerError(
                "Demo and CS2 versions do not match "
                f"(Demo {demo_version}, installed game {installed_version}); "
                "download or record a fresh Demo with the current CS2 client"
            )
    return comparisons[0] if comparisons else None


def _is_terminal_render_error(error: str) -> bool:
    """Only older Demos are obsolete; a newer Demo can work after a CS2 update."""

    normalized = " ".join(str(error).split()).casefold()
    match = re.match(
        r"demo and cs2 versions do not match \(demo ([0-9]+), installed game ([0-9]+)\)",
        normalized,
    )
    return match is not None and int(match[1]) < int(match[2])


def _local_highlight_candidates(
    demo_path: Path,
    steam_id: str,
    *,
    parser_factory: Callable[[str], Any] | None = None,
) -> list[dict[str, Any]]:
    """Parse a local demo and return conservative round-level candidates."""

    return _local_highlight_candidates_for_players(
        demo_path,
        (steam_id,),
        parser_factory=parser_factory,
    ).get(steam_id, [])


def _local_highlight_candidates_for_players(
    demo_path: Path,
    steam_ids: Sequence[str],
    *,
    parser_factory: Callable[[str], Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Parse deaths once and build candidates for every configured member."""

    if parser_factory is None:
        try:
            from demoparser2 import DemoParser
        except ImportError as exc:
            raise WorkerError(
                "demoparser2 is not installed; run setup-worker.ps1 to verify dependencies"
            ) from exc
        parser_factory = DemoParser
    try:
        parser = parser_factory(str(demo_path))
        rows = _table_records(
            parser.parse_event(
                "player_death",
                other=["total_rounds_played", "is_warmup_period"],
            )
        )
    except WorkerError:
        raise
    except Exception as exc:
        raise WorkerError(f"cannot parse local demo: {exc}") from exc
    tracked = set(steam_ids)
    rounds: dict[str, dict[int, list[tuple[int, bool]]]] = {
        steam_id: defaultdict(list) for steam_id in tracked
    }
    for row in rows:
        if _truthy(row.get("is_warmup_period")):
            continue
        attacker = _steam_text(
            _first_value(
                row,
                "attacker_steamid",
                "attacker_steam_id",
                "attacker_xuid",
            )
        )
        victim = _steam_text(_first_value(row, "user_steamid", "victim_steamid", "victim_steam_id"))
        if attacker not in tracked or not victim or victim == attacker:
            continue
        tick = max(0, _integer(row.get("tick")))
        round_number = max(1, _integer(row.get("total_rounds_played")) + 1)
        headshot = _truthy(_first_value(row, "headshot", "is_headshot"))
        rounds[attacker][round_number].append((tick, headshot))
    result: dict[str, list[dict[str, Any]]] = {steam_id: [] for steam_id in tracked}
    for steam_id, player_rounds in rounds.items():
        for round_number, kills in sorted(player_rounds.items()):
            kills.sort()
            kill_count = len(kills)
            headshots = sum(headshot for _tick, headshot in kills)
            if kill_count < 3 and headshots < 2:
                continue
            if kill_count >= 5:
                label = "ACE"
            elif kill_count == 4:
                label = "4K"
            elif kill_count == 3:
                label = "3K"
            else:
                label = f"{headshots} headshots"
            first_kill_tick = kills[0][0]
            last_kill_tick = kills[-1][0]
            desired_before = 8 * 64
            desired_after = 10 * 64
            available_padding = max(0, MAX_CSDM_TOTAL_TICKS - (last_kill_tick - first_kill_tick))
            before = min(desired_before, available_padding // 2)
            after = min(desired_after, available_padding - before)
            remaining = available_padding - before - after
            before += min(desired_before - before, remaining)
            remaining = available_padding - before - after
            after += min(desired_after - after, remaining)
            start_tick = max(0, first_kill_tick - before)
            end_tick = max(last_kill_tick + after, start_tick + 1)
            if end_tick - start_tick > MAX_CSDM_TOTAL_TICKS:
                end_tick = start_tick + MAX_CSDM_TOTAL_TICKS
            result[steam_id].append(
                {
                    "start_tick": start_tick,
                    "end_tick": end_tick,
                    "label": f"Round {round_number} {label}",
                    "score": float(min(100, kill_count * 18 + headshots * 6)),
                }
            )
    return result


def _local_job(
    demo_path: Path,
    steam_id: str,
    source: str,
    segments: list[dict[str, Any]],
) -> dict[str, Any]:
    identity = f"{demo_path.resolve()}\n{demo_path.stat().st_size}\n{steam_id}".encode()
    return {
        "job_id": f"local-{hashlib.sha256(identity).hexdigest()[:24]}",
        "match_id": demo_path.stem[:160] or "local-demo",
        "steam_id": steam_id,
        "segments": segments,
        "context": {"source": source, "local_demo": True},
        "source": source,
    }


def _validate_local_input(demo_path: Path, steam_id: str) -> Path:
    if not steam_id.isdigit() or len(steam_id) != 17:
        raise WorkerError("--steam-id must be a 17 digit SteamID64")
    if demo_path.is_symlink():
        raise WorkerError("--local-demo must not be a symbolic link")
    try:
        resolved = demo_path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise WorkerError(f"cannot open local demo: {exc}") from exc
    if not resolved.is_file() or resolved.suffix.casefold() != ".dem":
        raise WorkerError("--local-demo must point to a regular .dem file")
    return resolved


def _render_local(
    config: WorkerConfig,
    demo_path: Path,
    steam_id: str,
    source: str,
    segments: list[dict[str, Any]],
) -> Path:
    if not segments:
        raise WorkerError("the local demo has no 3K, multi-headshot, or stronger candidate")
    _ensure_demo_matches_installed_game(demo_path, steam_inf_path=config.steam_inf_path)
    # A local acceptance render produces one concise clip.  Rendering every
    # candidate from a full match can exceed the same 60-second/24-MiB budget
    # enforced for queued bot jobs, so choose the strongest candidate here.
    strongest = max(
        segments,
        key=lambda item: (float(item.get("score") or 0.0), -int(item["start_tick"])),
    )
    job = _local_job(demo_path, steam_id, source, [strongest])
    directory = config.workspace_dir / job["job_id"]
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    render_demo = demo_path
    if config.renderer_mode == "custom":
        render_demo = directory / "input.dem"
        shutil.copyfile(demo_path, render_demo)
    manifest_path, commands_path, output_path = _write_render_files(job, directory)
    if config.renderer_mode == "csdm":
        _render_csdm(
            job,
            config,
            directory,
            render_demo,
            output_path,
            api=None,
        )
    else:
        _render_custom(
            job,
            config,
            directory,
            render_demo,
            manifest_path,
            commands_path,
            output_path,
            api=None,
        )
    _validated_video(output_path, config.max_video_bytes)
    return output_path


def process_once(api: HighlightAPI, config: WorkerConfig) -> bool:
    raw_job = api.claim()
    if raw_job is None:
        return False
    job: dict[str, Any] | None = None
    directory: Path | None = None
    try:
        job = _validate_job(raw_job, config)
        directory = config.workspace_dir / job["job_id"]
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)
        demo_path = directory / "input.dem"
        api.download_demo(job, demo_path)
        _ensure_demo_matches_installed_game(demo_path, steam_inf_path=config.steam_inf_path)
        manifest_path, commands_path, output_path = _write_render_files(job, directory)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        api.upload_manifest(job, manifest)
        if config.renderer_mode == "csdm":
            _render_csdm(
                job,
                config,
                directory,
                demo_path,
                output_path,
                api=api,
            )
        else:
            _render_custom(
                job,
                config,
                directory,
                demo_path,
                manifest_path,
                commands_path,
                output_path,
                api=api,
            )
        _validated_video(output_path, config.max_video_bytes)
        api.upload_video(job, output_path)
        if not config.keep_successful_jobs:
            shutil.rmtree(directory, ignore_errors=True)
        print(f"completed highlight job {job['job_id']}", flush=True)
        return True
    except Exception as exc:
        message = str(exc) if isinstance(exc, WorkerError) else f"unexpected worker failure: {exc}"
        obsolete_demo = _is_terminal_render_error(message)
        reported = False
        if job is not None:
            try:
                api.fail(job, message, terminal=obsolete_demo)
                reported = True
            except Exception as report_error:
                print(f"could not report failed job: {report_error}", file=sys.stderr, flush=True)
        # The server keeps its verified source Demo for retries.  Retaining a
        # second multi-hundred-megabyte copy after a permanently failed render
        # would eventually fill the Windows disk, so every failed cycle starts
        # clean on the next lease.
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        if obsolete_demo and reported:
            print(f"skipped obsolete highlight job {job['job_id']}: {message}", flush=True)
            return True
        raise WorkerError(message) from exc


class SingleInstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> SingleInstanceLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            raise WorkerError("another highlight worker process is already running") from exc
        return self

    def __exit__(self, *_args: Any) -> None:
        if self.handle is not None:
            self.handle.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Niuniu CS2 demo highlight worker")
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--local-demo", type=Path)
    parser.add_argument("--steam-id")
    parser.add_argument("--source", choices=("perfectworld", "valve"), default="valve")
    parser.add_argument(
        "--no-render",
        action="store_true",
        help="parse --local-demo and print candidate tick ranges without launching CS2",
    )
    args = parser.parse_args()
    try:
        if args.local_demo is not None:
            if args.once:
                raise WorkerError("--once cannot be combined with --local-demo")
            demo_path = _validate_local_input(args.local_demo, str(args.steam_id or ""))
            candidates = _local_highlight_candidates(demo_path, str(args.steam_id))
            summary = {
                "demo": str(demo_path),
                "steam_id": str(args.steam_id),
                "source": args.source,
                "candidates": candidates,
            }
            if args.no_render:
                print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
                return 0
            config = WorkerConfig.load(args.config.resolve())
            config.workspace_dir.mkdir(parents=True, exist_ok=True)
            output = _render_local(
                config,
                demo_path,
                str(args.steam_id),
                args.source,
                candidates,
            )
            summary["output"] = str(output)
            print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
            return 0
        if args.no_render or args.steam_id is not None:
            raise WorkerError("--no-render and --steam-id require --local-demo")
        config = WorkerConfig.load(args.config.resolve())
        secret = _consume_worker_secret(config.secret_env)
        api = HighlightAPI(config, secret)
        config.workspace_dir.mkdir(parents=True, exist_ok=True)
        with SingleInstanceLock(config.workspace_dir / "worker.lock"):
            while True:
                try:
                    processed = process_once(api, config)
                except WorkerError as exc:
                    print(f"worker cycle failed: {exc}", file=sys.stderr, flush=True)
                    processed = False
                    if args.once:
                        return 1
                if args.once:
                    return 0
                if not processed:
                    time.sleep(config.poll_seconds)
    except (WorkerError, OSError) as exc:
        print(f"worker startup failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
