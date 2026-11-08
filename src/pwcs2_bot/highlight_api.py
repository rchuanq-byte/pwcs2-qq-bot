"""Private aiohttp routes used by the Windows CS2 demo render worker.

The route registrar can be attached to an existing aiohttp gateway.  A
standalone loopback-only runner is also provided for deployments where the
gateway does not expose an extension hook.  Every route, including health, is
HMAC authenticated.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.web_request import RequestKey

from pwcs2_bot.highlight_jobs import HighlightJobStore
from pwcs2_bot.highlight_protocol import (
    MAX_JSON_BODY_BYTES,
    HighlightProtocolError,
    RequestAuthenticator,
    normalize_cs2_patch,
    validate_body_digest,
    validate_job_id,
)

API_PREFIX = "/internal/highlights/v1"
WORKER_KEY = RequestKey("highlight_worker", str)
CONTENT_SHA_KEY = RequestKey("highlight_content_sha256", str)
AUTH_KEY = web.AppKey("highlight_authenticator", RequestAuthenticator)
STORE_KEY = web.AppKey("highlight_job_store", HighlightJobStore)
MAX_UPLOAD_KEY = web.AppKey("highlight_max_upload", int)


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _authenticate(request: web.Request) -> str:
    try:
        worker = request.app[AUTH_KEY].authenticate(
            method=request.method,
            path=request.path,
            headers=request.headers,
        )
    except HighlightProtocolError as exc:
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": str(exc)}, ensure_ascii=False),
            content_type="application/json",
        ) from exc
    request[WORKER_KEY] = worker.worker_id
    request[CONTENT_SHA_KEY] = worker.content_sha256
    return worker.worker_id


async def _verified_json(request: web.Request) -> dict[str, Any]:
    declared = request.content_length
    if declared is None or declared < 2 or declared > MAX_JSON_BODY_BYTES:
        raise web.HTTPRequestEntityTooLarge(
            max_size=MAX_JSON_BODY_BYTES,
            actual_size=declared or 0,
        )
    body = await request.read()
    try:
        validate_body_digest(body, request[CONTENT_SHA_KEY])
        payload = json.loads(body)
    except (HighlightProtocolError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": str(exc)}, ensure_ascii=False),
            content_type="application/json",
        ) from exc
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(
            text='{"error":"JSON body must be an object"}',
            content_type="application/json",
        )
    return payload


def _lease_token(request: web.Request) -> str:
    value = str(request.headers.get("X-Highlight-Lease") or "")
    if not 32 <= len(value) <= 128 or any(char.isspace() for char in value):
        raise web.HTTPForbidden(
            text='{"error":"missing or invalid highlight lease"}',
            content_type="application/json",
        )
    return value


def register_highlight_routes(
    app: web.Application,
    *,
    store: HighlightJobStore,
    worker_secrets: dict[str, str | bytes],
    max_upload_bytes: int | None = None,
) -> None:
    """Register authenticated worker routes on an aiohttp application."""

    app[AUTH_KEY] = RequestAuthenticator(worker_secrets)
    app[STORE_KEY] = store
    app[MAX_UPLOAD_KEY] = min(
        store.max_video_bytes,
        max_upload_bytes if max_upload_bytes is not None else store.max_video_bytes,
    )

    async def health(request: web.Request) -> web.Response:
        _authenticate(request)
        return web.json_response({"status": "ok", "service": "highlight-worker-api"})

    async def claim(request: web.Request) -> web.Response:
        worker_id = _authenticate(request)
        payload = await _verified_json(request)
        try:
            lease_seconds = int(payload.get("lease_seconds") or 900)
            renderer_patch = normalize_cs2_patch(payload.get("renderer_patch"))
        except (TypeError, ValueError) as exc:
            raise web.HTTPBadRequest(
                text='{"error":"invalid lease_seconds or renderer_patch"}',
                content_type="application/json",
            ) from exc
        job = await asyncio.to_thread(
            request.app[STORE_KEY].claim,
            worker_id,
            lease_seconds=lease_seconds,
            renderer_patch=renderer_patch,
        )
        if job is None:
            return web.Response(status=204)
        return web.json_response(job.as_worker_payload(API_PREFIX))

    async def download_demo(request: web.Request) -> web.StreamResponse:
        worker_id = _authenticate(request)
        lease_token = _lease_token(request)
        try:
            job_id = validate_job_id(request.match_info["job_id"])
            path, digest, size = await asyncio.to_thread(
                request.app[STORE_KEY].demo_for_lease,
                job_id,
                worker_id=worker_id,
                lease_token=lease_token,
            )
        except (HighlightProtocolError, OSError) as exc:
            raise web.HTTPForbidden(
                text=json.dumps({"error": str(exc)}, ensure_ascii=False),
                content_type="application/json",
            ) from exc
        response = web.FileResponse(path, headers={"X-Content-SHA256": digest})
        response.content_length = size
        response.content_type = "application/octet-stream"
        return response

    async def upload_manifest(request: web.Request) -> web.Response:
        worker_id = _authenticate(request)
        lease_token = _lease_token(request)
        manifest = await _verified_json(request)
        try:
            job_id = validate_job_id(request.match_info["job_id"])
            await asyncio.to_thread(
                request.app[STORE_KEY].record_manifest,
                job_id,
                worker_id=worker_id,
                lease_token=lease_token,
                manifest=manifest,
            )
        except HighlightProtocolError as exc:
            raise web.HTTPForbidden(
                text=json.dumps({"error": str(exc)}, ensure_ascii=False),
                content_type="application/json",
            ) from exc
        return web.json_response({"status": "accepted"})

    async def renew_lease(request: web.Request) -> web.Response:
        worker_id = _authenticate(request)
        lease_token = _lease_token(request)
        payload = await _verified_json(request)
        try:
            job_id = validate_job_id(request.match_info["job_id"])
            lease_seconds = int(payload.get("lease_seconds") or 900)
            lease_until = await asyncio.to_thread(
                request.app[STORE_KEY].renew_lease,
                job_id,
                worker_id=worker_id,
                lease_token=lease_token,
                lease_seconds=lease_seconds,
            )
        except (TypeError, ValueError, HighlightProtocolError) as exc:
            raise web.HTTPForbidden(
                text=json.dumps({"error": str(exc)}, ensure_ascii=False),
                content_type="application/json",
            ) from exc
        return web.json_response({"status": "renewed", "lease_until": lease_until})

    async def upload_video(request: web.Request) -> web.Response:
        worker_id = _authenticate(request)
        lease_token = _lease_token(request)
        maximum = request.app[MAX_UPLOAD_KEY]
        declared = request.content_length
        if declared is None or declared <= 0 or declared > maximum:
            raise web.HTTPRequestEntityTooLarge(max_size=maximum, actual_size=declared or 0)
        if request.content_type.casefold() != "video/mp4":
            raise web.HTTPUnsupportedMediaType(
                text='{"error":"render upload must use video/mp4"}',
                content_type="application/json",
            )
        job_id = validate_job_id(request.match_info["job_id"])
        temporary = request.app[STORE_KEY].tmp_root / (
            f"{job_id}.{secrets.token_hex(8)}.mp4.part"
        )
        digest = hashlib.sha256()
        written = 0
        try:
            with temporary.open("xb") as handle:
                async for chunk in request.content.iter_chunked(1024 * 1024):
                    written += len(chunk)
                    if written > maximum:
                        raise web.HTTPRequestEntityTooLarge(
                            max_size=maximum, actual_size=written
                        )
                    digest.update(chunk)
                    handle.write(chunk)
            if written != declared:
                raise HighlightProtocolError("render upload length mismatch")
            expected = request[CONTENT_SHA_KEY]
            if not hmac.compare_digest(digest.hexdigest(), expected):
                raise HighlightProtocolError("render upload digest mismatch")
            completed = await asyncio.to_thread(
                request.app[STORE_KEY].complete,
                job_id,
                worker_id=worker_id,
                lease_token=lease_token,
                temporary_video_path=temporary,
            )
        except HighlightProtocolError as exc:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": str(exc)}, ensure_ascii=False),
                content_type="application/json",
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)
        return web.json_response(
            {
                "status": "completed",
                "job_id": completed.job_id,
                "sha256": completed.output_sha256,
            }
        )

    async def fail(request: web.Request) -> web.Response:
        worker_id = _authenticate(request)
        lease_token = _lease_token(request)
        payload = await _verified_json(request)
        try:
            job_id = validate_job_id(request.match_info["job_id"])
            await asyncio.to_thread(
                request.app[STORE_KEY].fail,
                job_id,
                worker_id=worker_id,
                lease_token=lease_token,
                error=str(payload.get("error") or ""),
                terminal=payload.get("terminal") is True,
            )
        except HighlightProtocolError as exc:
            raise web.HTTPForbidden(
                text=json.dumps({"error": str(exc)}, ensure_ascii=False),
                content_type="application/json",
            ) from exc
        return web.json_response({"status": "recorded"})

    app.router.add_get(f"{API_PREFIX}/health", health)
    app.router.add_post(f"{API_PREFIX}/jobs/claim", claim)
    app.router.add_get(f"{API_PREFIX}/jobs/{{job_id}}/demo", download_demo)
    app.router.add_put(f"{API_PREFIX}/jobs/{{job_id}}/lease", renew_lease)
    app.router.add_put(f"{API_PREFIX}/jobs/{{job_id}}/manifest", upload_manifest)
    app.router.add_put(f"{API_PREFIX}/jobs/{{job_id}}/video", upload_video)
    app.router.add_post(f"{API_PREFIX}/jobs/{{job_id}}/fail", fail)


def create_highlight_app(
    *,
    store: HighlightJobStore,
    worker_secrets: dict[str, str | bytes],
    max_upload_bytes: int | None = None,
) -> web.Application:
    maximum = max_upload_bytes or store.max_video_bytes
    app = web.Application(client_max_size=maximum + 1024)
    register_highlight_routes(
        app,
        store=store,
        worker_secrets=worker_secrets,
        max_upload_bytes=maximum,
    )
    return app


def _configured_worker_secrets() -> dict[str, str]:
    raw = os.environ.get("PWCS2_HIGHLIGHT_WORKER_SECRETS_JSON", "")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit("PWCS2_HIGHLIGHT_WORKER_SECRETS_JSON is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise SystemExit("PWCS2_HIGHLIGHT_WORKER_SECRETS_JSON must be an object")
    return {str(key): str(value) for key, value in payload.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description="Private CS2 highlight worker API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18791)
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("PWCS2_HIGHLIGHT_DATA_DIR", "/data/highlights"),
    )
    parser.add_argument(
        "--max-video-bytes",
        type=int,
        default=int(
            os.environ.get("PWCS2_HIGHLIGHT_MAX_VIDEO_BYTES", str(24 * 1024 * 1024))
        ),
    )
    args = parser.parse_args()
    loopback_hosts = {"127.0.0.1", "::1", "localhost"}
    container_bind_allowed = os.environ.get(
        "PWCS2_HIGHLIGHT_ALLOW_CONTAINER_BIND", ""
    ).strip() == "1"
    if args.host not in loopback_hosts and not (
        args.host == "0.0.0.0" and container_bind_allowed
    ):
        raise SystemExit(
            "highlight API must bind to loopback, or use the explicit container-bind guard"
        )
    if not 1024 * 1024 <= args.max_video_bytes <= 96 * 1024 * 1024:
        raise SystemExit("highlight max video size must be between 1 MiB and 96 MiB")
    try:
        shared_output_gid = int(
            os.environ.get("PWCS2_HIGHLIGHT_SHARED_OUTPUT_GID", "-1")
        )
    except ValueError as exc:
        raise SystemExit("highlight shared output gid must be an integer") from exc
    if not -1 <= shared_output_gid <= 65535:
        raise SystemExit("highlight shared output gid is outside the supported range")
    store = HighlightJobStore(
        Path(args.data_dir),
        max_video_bytes=args.max_video_bytes,
        shared_root_gid=shared_output_gid if shared_output_gid >= 0 else None,
        shared_output_gid=shared_output_gid if shared_output_gid >= 0 else None,
    )
    app = create_highlight_app(store=store, worker_secrets=_configured_worker_secrets())
    web.run_app(app, host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
