"""Durable, lease-based jobs for an external Windows CS2 demo renderer."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pwcs2_bot.highlight_protocol import (
    HighlightProtocolError,
    HighlightSegment,
    normalize_cs2_patch,
    validate_job_id,
)

MAX_DEMO_BYTES = 2 * 1024 * 1024 * 1024
MAX_VIDEO_BYTES = 96 * 1024 * 1024
MAX_SEGMENTS = 16
MAX_CONTEXT_BYTES = 64 * 1024
_MANAGED_TMP_ARTIFACT_RE = re.compile(
    r"(?P<job_id>[A-Za-z0-9][A-Za-z0-9_-]{7,63})\."
    r"(?P<nonce>[0-9a-f]{16})\.(?:dem|mp4)\.part\Z"
)


@dataclass(frozen=True, slots=True)
class ClaimedHighlightJob:
    job_id: str
    group_id: str
    match_id: str
    steam_id: str
    demo_sha256: str
    demo_size: int
    segments: tuple[HighlightSegment, ...]
    context: dict[str, Any]
    lease_token: str
    lease_until: int
    demo_patch: str = ""

    def as_worker_payload(self, api_prefix: str = "/internal/highlights/v1") -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "group_id": self.group_id,
            "match_id": self.match_id,
            "steam_id": self.steam_id,
            "demo": {
                "download_path": f"{api_prefix}/jobs/{self.job_id}/demo",
                "sha256": self.demo_sha256,
                "size": self.demo_size,
                "patch_version": self.demo_patch,
            },
            "segments": [segment.as_dict() for segment in self.segments],
            "context": self.context,
            "lease_token": self.lease_token,
            "lease_until": self.lease_until,
        }


@dataclass(frozen=True, slots=True)
class CompletedHighlight:
    job_id: str
    group_id: str
    match_id: str
    steam_id: str
    output_path: Path
    output_sha256: str
    completed_at: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class UnknownHighlightDelivery:
    job_id: str
    group_id: str
    match_id: str
    steam_id: str
    state: str
    started_at: str
    last_error: str
    demo_present: bool
    output_present: bool


@dataclass(frozen=True, slots=True)
class FailedHighlightDelivery:
    """A render that was proven unsendable before OneBot was contacted."""

    job_id: str
    group_id: str
    match_id: str
    steam_id: str
    failed_at: str
    last_error: str
    demo_present: bool
    output_present: bool


@dataclass(frozen=True, slots=True)
class HighlightDemoBacklog:
    """Actual managed Demo usage used for admission control.

    Filesystem usage is authoritative here: completed rows remain in SQLite
    after delivery, while orphaned or failed Demo files still consume disk.
    Counting the managed directory therefore avoids both false capacity and an
    unbounded disk leak when the Windows renderer is offline.
    """

    demo_count: int
    demo_bytes: int

    def can_accept(
        self,
        *,
        reserved_bytes: int,
        max_demo_count: int,
        max_demo_bytes: int,
    ) -> bool:
        if reserved_bytes < 0 or max_demo_count < 1 or max_demo_bytes < 1:
            raise ValueError("invalid highlight Demo backlog limit")
        return (
            self.demo_count < max_demo_count
            and self.demo_bytes + reserved_bytes <= max_demo_bytes
        )


@dataclass(frozen=True, slots=True)
class HighlightPruneResult:
    """Bounded artifact-maintenance result retained for operator logging."""

    failed_jobs: tuple[str, ...] = ()
    delivered_jobs: tuple[str, ...] = ()
    orphan_files: tuple[str, ...] = ()
    skipped_jobs: tuple[str, ...] = ()


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sha256_file(path: Path, *, max_bytes: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise HighlightProtocolError("highlight file exceeds the configured size limit")
            digest.update(chunk)
    if total <= 0:
        raise HighlightProtocolError("highlight file is empty")
    return digest.hexdigest(), total


def _is_mp4(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            header = handle.read(32)
    except OSError:
        return False
    return len(header) >= 12 and header[4:8] == b"ftyp"


def _chmod_private_directory(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o700)


def _chmod_private_file(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)


def _share_output(path: Path, gid: int, *, directory: bool) -> None:
    if os.name != "nt":
        os.chown(path, -1, gid)
        path.chmod(0o750 if directory else 0o640)


def _share_root(path: Path, gid: int) -> None:
    if os.name != "nt":
        os.chown(path, -1, gid)
        path.chmod(0o770)


def _normalize_context(value: dict[str, Any] | None) -> tuple[dict[str, Any], str]:
    """Return a bounded, JSON-only copy of renderer metadata.

    Context is intentionally opaque to the job store.  It is copied into the
    renderer manifest, but never interpolated into CS2 console commands.
    """

    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise HighlightProtocolError("highlight context must be a JSON object")
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        normalized = json.loads(serialized)
    except (TypeError, ValueError, RecursionError) as exc:
        raise HighlightProtocolError("highlight context must contain valid JSON") from exc
    if len(serialized.encode("utf-8")) > MAX_CONTEXT_BYTES:
        raise HighlightProtocolError("highlight context is too large")
    if not isinstance(normalized, dict):  # Defensive: json round-trip must preserve object.
        raise HighlightProtocolError("highlight context must be a JSON object")
    return normalized, serialized


class HighlightJobStore:
    """Own demo copies and render outputs independently from the main bot DB."""

    def __init__(
        self,
        root: str | Path,
        *,
        max_demo_bytes: int = MAX_DEMO_BYTES,
        max_video_bytes: int = MAX_VIDEO_BYTES,
        max_attempts: int = 3,
        shared_root_gid: int | None = None,
        shared_output_gid: int | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.demo_root = self.root / "demos"
        self.output_root = self.root / "renders"
        self.tmp_root = self.root / "tmp"
        for directory in (self.root, self.demo_root, self.output_root, self.tmp_root):
            directory.mkdir(parents=True, exist_ok=True)
            _chmod_private_directory(directory)
        self.shared_root_gid = (
            None if shared_root_gid is None else max(0, int(shared_root_gid))
        )
        if self.shared_root_gid is not None:
            _share_root(self.root, self.shared_root_gid)
        self.database_path = self.root / "jobs.db"
        self.max_demo_bytes = max(1, int(max_demo_bytes))
        self.max_video_bytes = max(1, int(max_video_bytes))
        self.max_attempts = max(1, int(max_attempts))
        self.shared_output_gid = (
            None if shared_output_gid is None else max(0, int(shared_output_gid))
        )
        if self.shared_output_gid is not None:
            _share_output(self.output_root, self.shared_output_gid, directory=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        try:
            _chmod_private_file(self.database_path)
        except OSError:
            connection.close()
            raise HighlightProtocolError(
                "highlight database permissions could not be secured"
            ) from None
        connection.row_factory = sqlite3.Row
        return connection

    def demo_backlog(self) -> HighlightDemoBacklog:
        """Measure managed Demos plus all direct temporary-file bytes.

        Any inspection failure is fail-closed so the controller pauses rather
        than downloading another several-hundred-megabyte Demo into a disk it
        cannot account for safely.  Temporary files are included even when
        their names are unknown: a crash during Demo copy or video upload must
        not make occupied bytes invisible to admission control.  The count is
        limited to queued/leased/rendering jobs plus unregistered ``.dem``
        orphans: failed and ambiguous-delivery audit artifacts still consume
        the byte budget, but cannot pin the independent executable-job count
        for their retention period.  Orphans count until pruning so a crash
        between copying a Demo and committing its row cannot bypass the cap.
        """

        active_demo_paths: set[Path] = set()
        known_demo_paths: set[Path] = set()
        orphan_demo_paths: set[Path] = set()
        total = 0
        try:
            with self._connect() as connection:
                demo_rows = connection.execute(
                    """
                    SELECT job_id, status, demo_path FROM highlight_worker_jobs
                    WHERE demo_path != ''
                    """
                ).fetchall()
            for row in demo_rows:
                path = self._validated_artifact_path(
                    row["demo_path"],
                    expected_parent=self.demo_root,
                    expected_suffix=".dem",
                    job_id=str(row["job_id"]),
                )
                if path is not None:
                    known_demo_paths.add(path)
                    if row["status"] in {"queued", "leased", "rendering"}:
                        active_demo_paths.add(path)
            candidates = tuple(self.demo_root.iterdir())
            for path in candidates:
                if path.is_symlink():
                    continue
                resolved = path.resolve(strict=True)
                if resolved.parent != self.demo_root or not resolved.is_file():
                    continue
                size = resolved.stat().st_size
                if size < 0:
                    raise OSError("negative file size")
                total += size
                if resolved.suffix.lower() == ".dem" and resolved not in known_demo_paths:
                    orphan_demo_paths.add(resolved)
            for path in tuple(self.tmp_root.iterdir()):
                if path.is_symlink():
                    continue
                resolved = path.resolve(strict=True)
                if resolved.parent != self.tmp_root or not resolved.is_file():
                    continue
                size = resolved.stat().st_size
                if size < 0:
                    raise OSError("negative file size")
                total += size
        except OSError:
            raise HighlightProtocolError(
                "managed highlight Demo backlog could not be inspected"
            ) from None
        return HighlightDemoBacklog(
            demo_count=len(active_demo_paths | orphan_demo_paths),
            demo_bytes=total,
        )

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS highlight_worker_jobs (
                    job_id TEXT PRIMARY KEY,
                    group_id TEXT NOT NULL,
                    match_id TEXT NOT NULL,
                    steam_id TEXT NOT NULL,
                    demo_path TEXT NOT NULL,
                    demo_sha256 TEXT NOT NULL,
                    demo_size INTEGER NOT NULL,
                    segments_json TEXT NOT NULL,
                    context_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL CHECK (
                        status IN ('queued', 'leased', 'rendering', 'completed',
                                   'delivered', 'failed')
                    ),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    lease_worker TEXT NOT NULL DEFAULT '',
                    lease_token_hash TEXT NOT NULL DEFAULT '',
                    lease_until INTEGER NOT NULL DEFAULT 0,
                    manifest_json TEXT NOT NULL DEFAULT '',
                    output_path TEXT NOT NULL DEFAULT '',
                    output_sha256 TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    delivery_state TEXT NOT NULL DEFAULT '',
                    delivery_started_at TEXT NOT NULL DEFAULT '',
                    delivery_last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(highlight_worker_jobs)"
                ).fetchall()
            }
            if "context_json" not in columns:
                connection.execute(
                    "ALTER TABLE highlight_worker_jobs "
                    "ADD COLUMN context_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "demo_patch" not in columns:
                connection.execute(
                    "ALTER TABLE highlight_worker_jobs "
                    "ADD COLUMN demo_patch TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS highlight_renderer_versions (
                    worker_id TEXT PRIMARY KEY,
                    patch_version TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            for name in (
                "delivery_state",
                "delivery_started_at",
                "delivery_last_error",
            ):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE highlight_worker_jobs "
                        f"ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                    )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS highlight_worker_jobs_claim_idx
                ON highlight_worker_jobs(status, lease_until, created_at)
                """
            )

    def enqueue_demo(
        self,
        *,
        group_id: str,
        match_id: str,
        steam_id: str,
        demo_path: str | Path,
        segments: tuple[HighlightSegment, ...] | list[HighlightSegment],
        context: dict[str, Any] | None = None,
        job_id: str | None = None,
        demo_patch: str = "",
    ) -> str:
        demo_patch = normalize_cs2_patch(demo_patch)
        if not group_id.isdigit() or not 5 <= len(group_id) <= 20:
            raise HighlightProtocolError("invalid highlight group id")
        match_id = " ".join(match_id.split())
        if not match_id or len(match_id) > 160 or any(ord(char) < 32 for char in match_id):
            raise HighlightProtocolError("invalid highlight match id")
        if not steam_id.isdigit() or len(steam_id) != 17:
            raise HighlightProtocolError("invalid highlight SteamID64")
        normalized_segments = tuple(segments)
        if not 1 <= len(normalized_segments) <= MAX_SEGMENTS:
            raise HighlightProtocolError("invalid number of highlight segments")
        if not all(isinstance(segment, HighlightSegment) for segment in normalized_segments):
            raise HighlightProtocolError("invalid highlight segment")
        _normalized_context, context_json = _normalize_context(context)
        original_source = Path(demo_path)
        if original_source.is_symlink():
            raise HighlightProtocolError("highlight input must not be a symbolic link")
        source = original_source.resolve(strict=True)
        if source.suffix.casefold() != ".dem" or not source.is_file():
            raise HighlightProtocolError("highlight input must be a regular .dem file")
        digest, size = _sha256_file(source, max_bytes=self.max_demo_bytes)
        job_id = validate_job_id(job_id or uuid.uuid4().hex)
        destination = self.demo_root / f"{job_id}.dem"
        temporary = self.tmp_root / f"{job_id}.{secrets.token_hex(8)}.dem.part"
        serialized_segments = json.dumps(
            [segment.as_dict() for segment in normalized_segments],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM highlight_worker_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if existing is not None:
            if (
                str(existing["group_id"]) == group_id
                and str(existing["match_id"]) == match_id
                and str(existing["steam_id"]) == steam_id
                and str(existing["demo_sha256"]) == digest
                and int(existing["demo_size"]) == size
                and str(existing["segments_json"]) == serialized_segments
                and str(existing["context_json"]) == context_json
            ):
                return job_id
            raise HighlightProtocolError("highlight job id conflicts with existing content")
        if destination.exists():
            if _sha256_file(destination, max_bytes=self.max_demo_bytes) != (digest, size):
                raise HighlightProtocolError("highlight job already has a conflicting demo")
            _chmod_private_file(destination)
        else:
            shutil.copyfile(source, temporary)
            _chmod_private_file(temporary)
            copied_digest, copied_size = _sha256_file(
                temporary,
                max_bytes=self.max_demo_bytes,
            )
            if copied_digest != digest or copied_size != size:
                temporary.unlink(missing_ok=True)
                raise HighlightProtocolError("highlight demo changed while being copied")
            temporary.replace(destination)
            _chmod_private_file(destination)
        now = _utc_timestamp()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO highlight_worker_jobs
                        (job_id, group_id, match_id, steam_id, demo_path,
                         demo_sha256, demo_size, segments_json, context_json, status,
                         created_at, updated_at, demo_patch)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                    """,
                    (
                        job_id,
                        group_id,
                        match_id,
                        steam_id,
                        str(destination),
                        digest,
                        size,
                        serialized_segments,
                        context_json,
                        now,
                        now,
                        demo_patch,
                    ),
                )
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return job_id

    def renew_lease(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        lease_seconds: int = 900,
    ) -> int:
        """Extend a live worker lease and return its new Unix expiry."""
        row = self._authorized_lease(
            job_id,
            worker_id=worker_id,
            lease_token=lease_token,
        )
        lease_until = int(time.time()) + max(60, min(int(lease_seconds), 3600))
        now = _utc_timestamp()
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE highlight_worker_jobs
                SET lease_until = ?, updated_at = ?
                WHERE job_id = ? AND lease_worker = ? AND lease_token_hash = ?
                  AND status IN ('leased', 'rendering')
                """,
                (
                    lease_until,
                    now,
                    job_id,
                    worker_id,
                    row["lease_token_hash"],
                ),
            )
        if updated.rowcount != 1:
            raise HighlightProtocolError("highlight lease changed while renewing")
        return lease_until

    def renderer_patches(self, *, fallback: str = "") -> tuple[str, ...]:
        """Authenticated worker reports override the bootstrap environment setting.

        Keep the last known build while a PC is offline. Newer Demos are never
        discarded on this evidence, so an offline PC updating cannot lose them.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT patch_version FROM highlight_renderer_versions"
            ).fetchall()
        patches = {str(row[0]) for row in rows}
        if not patches and fallback:
            patches.add(normalize_cs2_patch(fallback))
        return tuple(sorted(patches, key=int))

    def claim(
        self, worker_id: str, *, lease_seconds: int = 900, renderer_patch: str = ""
    ) -> ClaimedHighlightJob | None:
        renderer_patch = normalize_cs2_patch(renderer_patch)
        now_epoch = int(time.time())
        lease_until = now_epoch + max(60, min(int(lease_seconds), 3600))
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        now = _utc_timestamp()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if renderer_patch:
                connection.execute(
                    """
                    INSERT INTO highlight_renderer_versions VALUES (?, ?, ?)
                    ON CONFLICT(worker_id) DO UPDATE SET
                        patch_version = excluded.patch_version,
                        updated_at = excluded.updated_at
                    """,
                    (worker_id, renderer_patch, now),
                )
                oldest_patch = connection.execute(
                    "SELECT MIN(CAST(patch_version AS INTEGER)) "
                    "FROM highlight_renderer_versions"
                ).fetchone()[0]
                # Do not spend leases/downloads on known obsolete Demos. Never
                # touch a live lease or discard a Demo newer than the clients.
                connection.execute(
                    """
                    UPDATE highlight_worker_jobs
                    SET status = 'failed', lease_worker = '', lease_token_hash = '',
                        lease_until = 0, updated_at = ?,
                        last_error = 'skipped: Demo predates all reported CS2 renderers'
                    WHERE demo_patch != '' AND CAST(demo_patch AS INTEGER) < ?
                      AND (status = 'queued' OR (
                          status IN ('leased', 'rendering') AND lease_until < ?))
                    """,
                    (now, oldest_patch, now_epoch),
                )
            connection.execute(
                """
                UPDATE highlight_worker_jobs
                SET status = 'failed', lease_worker = '', lease_token_hash = '',
                    lease_until = 0,
                    last_error = 'highlight worker lease expired after maximum attempts',
                    updated_at = ?
                WHERE attempts >= ? AND status IN ('leased', 'rendering')
                  AND lease_until < ?
                """,
                (now, self.max_attempts, now_epoch),
            )
            connection.execute(
                """
                UPDATE highlight_worker_jobs
                SET status = 'failed',
                    last_error = 'highlight worker maximum attempts exhausted',
                    updated_at = ?
                WHERE attempts >= ? AND status = 'queued'
                """,
                (now, self.max_attempts),
            )
            row = connection.execute(
                """
                SELECT * FROM highlight_worker_jobs
                WHERE attempts < ? AND (
                    status = 'queued'
                    OR (status IN ('leased', 'rendering') AND lease_until < ?)
                )
                  AND (? = '' OR demo_patch = '' OR demo_patch = ?)
                ORDER BY created_at, job_id
                LIMIT 1
                """,
                (self.max_attempts, now_epoch, renderer_patch, renderer_patch),
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """
                UPDATE highlight_worker_jobs
                SET status = 'leased', attempts = attempts + 1,
                    lease_worker = ?, lease_token_hash = ?, lease_until = ?,
                    manifest_json = '', last_error = '', updated_at = ?
                WHERE job_id = ? AND attempts = ?
                """,
                (
                    worker_id,
                    token_hash,
                    lease_until,
                    now,
                    row["job_id"],
                    row["attempts"],
                ),
            )
            if updated.rowcount != 1:
                return None
            segments_payload = json.loads(str(row["segments_json"]))
            context, _serialized_context = _normalize_context(
                json.loads(str(row["context_json"]))
            )
        return ClaimedHighlightJob(
            job_id=str(row["job_id"]),
            group_id=str(row["group_id"]),
            match_id=str(row["match_id"]),
            steam_id=str(row["steam_id"]),
            demo_sha256=str(row["demo_sha256"]),
            demo_size=int(row["demo_size"]),
            segments=tuple(
                HighlightSegment.from_mapping(item)
                for item in segments_payload
                if isinstance(item, dict)
            ),
            context=context,
            lease_token=token,
            lease_until=lease_until,
            demo_patch=str(row["demo_patch"]),
        )

    def demo_for_lease(
        self, job_id: str, *, worker_id: str, lease_token: str
    ) -> tuple[Path, str, int]:
        row = self._authorized_lease(job_id, worker_id=worker_id, lease_token=lease_token)
        path = self._validated_artifact_path(
            row["demo_path"],
            expected_parent=self.demo_root,
            expected_suffix=".dem",
            job_id=job_id,
        )
        if path is None or not path.is_file():
            raise HighlightProtocolError("invalid stored highlight demo path")
        return path, str(row["demo_sha256"]), int(row["demo_size"])

    def record_manifest(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        manifest: dict[str, Any],
    ) -> None:
        row = self._authorized_lease(job_id, worker_id=worker_id, lease_token=lease_token)
        if str(manifest.get("job_id") or "") != job_id:
            raise HighlightProtocolError("render manifest job id mismatch")
        serialized = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
        if len(serialized.encode("utf-8")) > 64 * 1024:
            raise HighlightProtocolError("render manifest is too large")
        now = _utc_timestamp()
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE highlight_worker_jobs
                SET status = 'rendering', manifest_json = ?, updated_at = ?
                WHERE job_id = ? AND lease_worker = ? AND lease_token_hash = ?
                  AND lease_until >= ? AND status IN ('leased', 'rendering')
                """,
                (
                    serialized,
                    now,
                    job_id,
                    worker_id,
                    row["lease_token_hash"],
                    int(time.time()),
                ),
            )
        if updated.rowcount != 1:
            raise HighlightProtocolError("highlight lease changed while saving manifest")

    def complete(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        temporary_video_path: str | Path,
    ) -> CompletedHighlight:
        row = self._authorized_lease(job_id, worker_id=worker_id, lease_token=lease_token)
        temporary_candidate = Path(temporary_video_path)
        if temporary_candidate.is_symlink():
            raise HighlightProtocolError("invalid temporary highlight video path")
        temporary = temporary_candidate.resolve(strict=True)
        if (
            temporary.parent != self.tmp_root
            or temporary.suffix.casefold() not in {".part", ".mp4"}
        ):
            raise HighlightProtocolError("invalid temporary highlight video path")
        digest, size = _sha256_file(temporary, max_bytes=self.max_video_bytes)
        if size <= 0 or not _is_mp4(temporary):
            raise HighlightProtocolError("rendered highlight is not a valid MP4 file")
        output = self.output_root / f"{job_id}.mp4"
        temporary.replace(output)
        if self.shared_output_gid is None:
            _chmod_private_file(output)
        else:
            _share_output(output, self.shared_output_gid, directory=False)
        now = _utc_timestamp()
        try:
            with self._connect() as connection:
                updated = connection.execute(
                    """
                    UPDATE highlight_worker_jobs
                    SET status = 'completed', output_path = ?, output_sha256 = ?,
                        completed_at = ?, updated_at = ?, lease_token_hash = '',
                        lease_until = 0
                    WHERE job_id = ? AND lease_worker = ? AND lease_token_hash = ?
                      AND status = 'rendering'
                    """,
                    (
                        str(output),
                        digest,
                        now,
                        now,
                        job_id,
                        worker_id,
                        row["lease_token_hash"],
                    ),
                )
            if updated.rowcount != 1:
                raise HighlightProtocolError("highlight lease changed while completing upload")
        except Exception:
            output.unlink(missing_ok=True)
            raise
        return CompletedHighlight(
            job_id=job_id,
            group_id=str(row["group_id"]),
            match_id=str(row["match_id"]),
            steam_id=str(row["steam_id"]),
            output_path=output,
            output_sha256=digest,
            completed_at=now,
            context=_normalize_context(json.loads(str(row["context_json"])))[0],
        )

    def fail(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        error: str,
        terminal: bool = False,
    ) -> None:
        row = self._authorized_lease(job_id, worker_id=worker_id, lease_token=lease_token)
        safe_error = " ".join(error.split())[:500] or "worker reported an unspecified error"
        # A Demo compiled for an older CS2 build cannot become playable through
        # retries. Keep it for audit, but do not spend the remaining attempts
        # on an immutable, known-unrenderable input.
        next_status = (
            "failed"
            if terminal or int(row["attempts"]) >= self.max_attempts
            else "queued"
        )
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE highlight_worker_jobs
                SET status = ?, last_error = ?, lease_worker = '',
                    lease_token_hash = '', lease_until = 0, updated_at = ?
                WHERE job_id = ? AND lease_worker = ? AND lease_token_hash = ?
                """,
                (
                    next_status,
                    safe_error,
                    _utc_timestamp(),
                    job_id,
                    worker_id,
                    row["lease_token_hash"],
                ),
            )

    def completed(self, *, limit: int = 10) -> tuple[CompletedHighlight, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT job_id, group_id, match_id, steam_id, output_path,
                       output_sha256, completed_at, context_json
                FROM highlight_worker_jobs
                WHERE status = 'completed' AND delivery_state = ''
                ORDER BY completed_at, job_id
                LIMIT ?
                """,
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        completed: list[CompletedHighlight] = []
        for row in rows:
            failure = ""
            try:
                output = self._validated_artifact_path(
                    row["output_path"],
                    expected_parent=self.output_root,
                    expected_suffix=".mp4",
                    job_id=str(row["job_id"]),
                )
            except HighlightProtocolError:
                output = None
                failure = "stored highlight output path is unsafe"
            if output is None or not output.is_file():
                failure = failure or "stored highlight output is unavailable"
            elif not _is_mp4(output):
                failure = "stored highlight output is not a valid MP4"
            if failure:
                self.fail_pre_dispatch_delivery(str(row["job_id"]), failure)
                continue
            completed.append(
                CompletedHighlight(
                    job_id=str(row["job_id"]),
                    group_id=str(row["group_id"]),
                    match_id=str(row["match_id"]),
                    steam_id=str(row["steam_id"]),
                    output_path=output,
                    output_sha256=str(row["output_sha256"]),
                    completed_at=str(row["completed_at"]),
                    context=_normalize_context(json.loads(str(row["context_json"])))[0],
                )
            )
        return tuple(completed)

    def fail_pre_dispatch_delivery(self, job_id: str, error: str) -> bool:
        """Terminalize a failure proven to precede any OneBot send attempt.

        The durable audit row and hashes are retained.  Each artifact path is
        validated independently before deletion, so a corrupt database path
        can never make cleanup escape the managed directories or prevent the
        job from becoming visibly terminal.
        """

        validate_job_id(job_id)
        safe_error = " ".join(error.split())[:500] or "highlight cannot be delivered"
        now = _utc_timestamp()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT job_id, demo_path, output_path
                FROM highlight_worker_jobs
                WHERE job_id = ? AND status = 'completed' AND delivery_state = ''
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                return False
            updated = connection.execute(
                """
                UPDATE highlight_worker_jobs
                SET status = 'failed', delivery_state = 'failed',
                    delivery_started_at = ?, delivery_last_error = ?,
                    last_error = ?, updated_at = ?
                WHERE job_id = ? AND status = 'completed' AND delivery_state = ''
                """,
                (now, safe_error, f"delivery preparation failed: {safe_error}", now, job_id),
            )
        if updated.rowcount != 1:
            return False
        for column, raw_path, expected_parent, expected_suffix in (
            ("demo_path", row["demo_path"], self.demo_root, ".dem"),
            ("output_path", row["output_path"], self.output_root, ".mp4"),
        ):
            try:
                path = self._validated_artifact_path(
                    raw_path,
                    expected_parent=expected_parent,
                    expected_suffix=expected_suffix,
                    job_id=job_id,
                )
                if path is None:
                    continue
                path.unlink(missing_ok=True)
                with self._connect() as connection:
                    connection.execute(
                        f"""
                        UPDATE highlight_worker_jobs SET {column} = ''
                        WHERE job_id = ? AND status = 'failed'
                          AND delivery_state = 'failed' AND {column} = ?
                        """,
                        (job_id, str(raw_path)),
                    )
            except (HighlightProtocolError, OSError):
                # Preserve the raw path in the terminal audit row.  Routine
                # pruning will retry only after applying the same path checks.
                continue
        return True

    def failed_deliveries(self, *, limit: int = 100) -> tuple[FailedHighlightDelivery, ...]:
        """Return deterministic pre-dispatch failures for operator diagnostics."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT job_id, group_id, match_id, steam_id, demo_path, output_path,
                       delivery_started_at, delivery_last_error
                FROM highlight_worker_jobs
                WHERE status = 'failed' AND delivery_state = 'failed'
                ORDER BY delivery_started_at, job_id
                LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return tuple(
            FailedHighlightDelivery(
                job_id=str(row["job_id"]),
                group_id=str(row["group_id"]),
                match_id=str(row["match_id"]),
                steam_id=str(row["steam_id"]),
                failed_at=str(row["delivery_started_at"]),
                last_error=str(row["delivery_last_error"]),
                demo_present=bool(row["demo_path"])
                and Path(str(row["demo_path"])).is_file(),
                output_present=bool(row["output_path"])
                and Path(str(row["output_path"])).is_file(),
            )
            for row in rows
        )

    def claim_delivery(self, job_id: str) -> bool:
        """Atomically reserve one completed video for a single OneBot attempt.

        There is no idempotency key in the OneBot send action.  Once claimed,
        an interrupted request must never be automatically dispatched again:
        OneBot may already have accepted it even when the caller saw a timeout.
        """

        validate_job_id(job_id)
        now = _utc_timestamp()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE highlight_worker_jobs
                SET delivery_state = 'in_flight', delivery_started_at = ?,
                    delivery_last_error = '', updated_at = ?
                WHERE job_id = ? AND status = 'completed' AND delivery_state = ''
                """,
                (now, now, job_id),
            )
        return updated.rowcount == 1

    def mark_delivery_unknown(self, job_id: str, error: str) -> None:
        """Quarantine an ambiguous send result instead of risking a duplicate."""

        validate_job_id(job_id)
        safe_error = " ".join(error.split())[:500] or "OneBot delivery result is unknown"
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE highlight_worker_jobs
                SET delivery_state = 'unknown', delivery_last_error = ?, updated_at = ?
                WHERE job_id = ? AND status = 'completed'
                  AND delivery_state = 'in_flight'
                """,
                (safe_error, _utc_timestamp(), job_id),
            )
        if updated.rowcount != 1:
            raise HighlightProtocolError("highlight delivery is not in flight")

    def release_delivery_claim(self, job_id: str, error: str) -> None:
        """Release only a failure proven to precede the OneBot action call."""

        validate_job_id(job_id)
        safe_error = " ".join(error.split())[:500] or "OneBot is not ready"
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE highlight_worker_jobs
                SET delivery_state = '', delivery_started_at = '',
                    delivery_last_error = ?, updated_at = ?
                WHERE job_id = ? AND status = 'completed'
                  AND delivery_state = 'in_flight'
                """,
                (safe_error, _utc_timestamp(), job_id),
            )
        if updated.rowcount != 1:
            raise HighlightProtocolError("highlight delivery is not in flight")

    def unknown_deliveries(self, *, limit: int = 100) -> tuple[UnknownHighlightDelivery, ...]:
        """Return ambiguous and archived attempts for operator audit."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT job_id, group_id, match_id, steam_id, demo_path, output_path,
                       delivery_state, delivery_started_at, delivery_last_error
                FROM highlight_worker_jobs
                WHERE status = 'completed'
                  AND delivery_state IN ('unknown', 'unknown_archived')
                ORDER BY delivery_started_at, job_id
                LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return tuple(
            UnknownHighlightDelivery(
                job_id=str(row["job_id"]),
                group_id=str(row["group_id"]),
                match_id=str(row["match_id"]),
                steam_id=str(row["steam_id"]),
                state=str(row["delivery_state"]),
                started_at=str(row["delivery_started_at"]),
                last_error=str(row["delivery_last_error"]),
                demo_present=bool(row["demo_path"])
                and Path(str(row["demo_path"])).is_file(),
                output_present=bool(row["output_path"])
                and Path(str(row["output_path"])).is_file(),
            )
            for row in rows
        )

    def retry_unknown_delivery(
        self,
        job_id: str,
        *,
        confirmed_not_delivered: bool = False,
    ) -> None:
        """Permit one manual retry only after an operator verifies QQ history."""

        validate_job_id(job_id)
        if not confirmed_not_delivered:
            raise HighlightProtocolError(
                "manual retry requires confirmation that QQ did not receive the video"
            )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT job_id, output_path FROM highlight_worker_jobs
                WHERE job_id = ? AND status = 'completed'
                  AND delivery_state = 'unknown'
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                raise HighlightProtocolError("highlight delivery cannot be retried")
            output = self._validated_artifact_path(
                row["output_path"],
                expected_parent=self.output_root,
                expected_suffix=".mp4",
                job_id=job_id,
            )
            if output is None or not output.is_file() or not _is_mp4(output):
                raise HighlightProtocolError("highlight delivery artifact is unavailable")
            updated = connection.execute(
                """
                UPDATE highlight_worker_jobs
                SET delivery_state = '', delivery_started_at = '', updated_at = ?
                WHERE job_id = ? AND status = 'completed'
                  AND delivery_state = 'unknown'
                """,
                (_utc_timestamp(), job_id),
            )
        if updated.rowcount != 1:
            raise HighlightProtocolError("highlight delivery cannot be retried")

    def archive_unknown_delivery_files(
        self,
        *,
        older_than_seconds: int = 14 * 24 * 60 * 60,
        limit: int = 20,
    ) -> tuple[str, ...]:
        """Delete stale ambiguous artifacts without ever re-queueing them.

        The audit row and hashes remain in SQLite.  A minimum three-day hold
        prevents an aggressive configuration from erasing evidence before an
        operator has a reasonable chance to inspect QQ history.
        """

        retention_seconds = max(3 * 24 * 60 * 60, int(older_than_seconds))
        cutoff = (datetime.now(UTC) - timedelta(seconds=retention_seconds)).isoformat(
            timespec="seconds"
        )
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT job_id
                FROM highlight_worker_jobs
                WHERE status = 'completed' AND delivery_state = 'unknown'
                  AND delivery_started_at != '' AND delivery_started_at <= ?
                ORDER BY delivery_started_at, job_id
                LIMIT ?
                """,
                (cutoff, max(1, min(int(limit), 100))),
            ).fetchall()
        archived: list[str] = []
        for row in rows:
            job_id = str(row["job_id"])
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    """
                    SELECT job_id, demo_path, output_path
                    FROM highlight_worker_jobs
                    WHERE job_id = ? AND status = 'completed'
                      AND delivery_state = 'unknown'
                      AND delivery_started_at != '' AND delivery_started_at <= ?
                    """,
                    (job_id, cutoff),
                ).fetchone()
                if current is None:
                    continue
                managed_paths = self._validated_artifact_paths(current)
                for path in managed_paths:
                    path.unlink(missing_ok=True)
                updated = connection.execute(
                    """
                    UPDATE highlight_worker_jobs
                    SET delivery_state = 'unknown_archived', demo_path = '',
                        output_path = '', updated_at = ?
                    WHERE job_id = ? AND status = 'completed'
                      AND delivery_state = 'unknown'
                    """,
                    (_utc_timestamp(), job_id),
                )
            if updated.rowcount == 1:
                archived.append(job_id)
        return tuple(archived)

    def quarantine_stale_delivery_claims(
        self,
        *,
        older_than_seconds: int = 30 * 60,
        limit: int = 100,
    ) -> tuple[str, ...]:
        """Convert abandoned in-flight claims to unknown, never back to pending."""

        stale_seconds = max(5 * 60, int(older_than_seconds))
        cutoff = (datetime.now(UTC) - timedelta(seconds=stale_seconds)).isoformat(
            timespec="seconds"
        )
        now = _utc_timestamp()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT job_id FROM highlight_worker_jobs
                WHERE status = 'completed' AND delivery_state = 'in_flight'
                  AND delivery_started_at != '' AND delivery_started_at <= ?
                ORDER BY delivery_started_at, job_id
                LIMIT ?
                """,
                (cutoff, max(1, min(int(limit), 1000))),
            ).fetchall()
            job_ids = tuple(str(row["job_id"]) for row in rows)
            for job_id in job_ids:
                connection.execute(
                    """
                    UPDATE highlight_worker_jobs
                    SET delivery_state = 'unknown',
                        delivery_started_at = ?,
                        delivery_last_error = 'stale in-flight claim after restart',
                        updated_at = ?
                    WHERE job_id = ? AND status = 'completed'
                      AND delivery_state = 'in_flight'
                      AND delivery_started_at <= ?
                    """,
                    (now, now, job_id, cutoff),
                )
        return job_ids

    def mark_delivered(self, job_id: str) -> None:
        validate_job_id(job_id)
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE highlight_worker_jobs
                SET status = 'delivered', delivery_state = 'delivered', updated_at = ?
                WHERE job_id = ? AND status = 'completed'
                  AND delivery_state = 'in_flight'
                """,
                (_utc_timestamp(), job_id),
            )
        if updated.rowcount != 1:
            raise HighlightProtocolError("highlight is not awaiting delivery")

    def cleanup_delivered_files(self, job_id: str) -> None:
        """Remove large artifacts only after OneBot accepted the video."""
        validate_job_id(job_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT job_id, demo_path, output_path FROM highlight_worker_jobs
                WHERE job_id = ? AND status = 'delivered'
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            raise HighlightProtocolError("highlight is not marked delivered")
        managed_paths = self._validated_artifact_paths(row)
        for path in managed_paths:
            path.unlink(missing_ok=True)

    def prune(
        self,
        *,
        failed_retention_seconds: int = 7 * 24 * 60 * 60,
        delivered_retention_seconds: int = 60 * 60,
        orphan_retention_seconds: int = 24 * 60 * 60,
        limit: int = 100,
    ) -> HighlightPruneResult:
        """Prune old terminal artifacts while preserving durable job records.

        Ambiguous delivery states are deliberately outside this method.  They
        remain quarantined and can only be archived by
        :meth:`archive_unknown_delivery_files`, so routine disk maintenance can
        never turn an unknown send into an automatic retry.

        The seven-day failed-job, one-hour delivered-job, and 24-hour orphan
        windows are safety floors even when a caller supplies a smaller value.
        """

        bounded_limit = max(1, min(int(limit), 1000))
        failed_cutoff = self._retention_cutoff(
            failed_retention_seconds,
            minimum_seconds=7 * 24 * 60 * 60,
        )
        delivered_cutoff = self._retention_cutoff(
            delivered_retention_seconds,
            minimum_seconds=60 * 60,
        )
        orphan_cutoff_epoch = time.time() - max(
            24 * 60 * 60,
            int(orphan_retention_seconds),
        )
        failed, failed_skipped = self._prune_terminal_artifacts(
            status="failed",
            cutoff=failed_cutoff,
            limit=bounded_limit,
        )
        delivered, delivered_skipped = self._prune_terminal_artifacts(
            status="delivered",
            cutoff=delivered_cutoff,
            limit=bounded_limit,
        )
        orphans = self._prune_orphan_artifacts(
            cutoff_epoch=orphan_cutoff_epoch,
            limit=bounded_limit,
        )
        remaining = max(0, bounded_limit - len(orphans))
        temporary_orphans = self._prune_temporary_orphan_artifacts(
            cutoff_epoch=orphan_cutoff_epoch,
            limit=remaining,
        )
        return HighlightPruneResult(
            failed_jobs=failed,
            delivered_jobs=delivered,
            orphan_files=(*orphans, *temporary_orphans),
            skipped_jobs=tuple(dict.fromkeys((*failed_skipped, *delivered_skipped))),
        )

    @staticmethod
    def _retention_cutoff(value: int, *, minimum_seconds: int) -> str:
        seconds = max(minimum_seconds, int(value))
        return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat(
            timespec="seconds"
        )

    def _prune_terminal_artifacts(
        self,
        *,
        status: str,
        cutoff: str,
        limit: int,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if status not in {"failed", "delivered"}:
            raise ValueError("unsupported highlight terminal status")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT job_id FROM highlight_worker_jobs
                WHERE status = ? AND updated_at <= ?
                  AND (demo_path != '' OR output_path != '')
                ORDER BY updated_at, job_id
                LIMIT ?
                """,
                (status, cutoff, limit),
            ).fetchall()
        pruned: list[str] = []
        skipped: list[str] = []
        for row in rows:
            job_id = str(row["job_id"])
            try:
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    current = connection.execute(
                        """
                        SELECT job_id, demo_path, output_path
                        FROM highlight_worker_jobs
                        WHERE job_id = ? AND status = ? AND updated_at <= ?
                          AND (demo_path != '' OR output_path != '')
                        """,
                        (job_id, status, cutoff),
                    ).fetchone()
                    if current is None:
                        continue
                    managed_paths = self._validated_artifact_paths(current)
                    for path in managed_paths:
                        path.unlink(missing_ok=True)
                    updated = connection.execute(
                        """
                        UPDATE highlight_worker_jobs
                        SET demo_path = '', output_path = ''
                        WHERE job_id = ? AND status = ? AND updated_at <= ?
                        """,
                        (job_id, status, cutoff),
                    )
                if updated.rowcount == 1:
                    pruned.append(job_id)
            except (HighlightProtocolError, OSError):
                skipped.append(job_id)
        return tuple(pruned), tuple(skipped)

    def _prune_orphan_artifacts(
        self,
        *,
        cutoff_epoch: float,
        limit: int,
    ) -> tuple[str, ...]:
        deleted: list[str] = []
        for root, suffix in (
            (self.demo_root, ".dem"),
            (self.output_root, ".mp4"),
        ):
            try:
                candidates = sorted(root.iterdir(), key=lambda path: path.name)
            except OSError:
                continue
            for candidate in candidates:
                if len(deleted) >= limit:
                    return tuple(deleted)
                if candidate.suffix.casefold() != suffix or candidate.is_symlink():
                    continue
                try:
                    validate_job_id(candidate.stem)
                    resolved = candidate.resolve(strict=True)
                    if (
                        resolved.parent != root
                        or not resolved.is_file()
                        or resolved.stat().st_mtime > cutoff_epoch
                    ):
                        continue
                    with self._connect() as connection:
                        connection.execute("BEGIN IMMEDIATE")
                        referenced = connection.execute(
                            """
                            SELECT 1 FROM highlight_worker_jobs
                            WHERE demo_path = ? OR output_path = ?
                            LIMIT 1
                            """,
                            (str(resolved), str(resolved)),
                        ).fetchone()
                        if referenced is not None:
                            continue
                        if candidate.is_symlink():
                            continue
                        current = candidate.resolve(strict=True)
                        if (
                            current != resolved
                            or current.parent != root
                            or not current.is_file()
                            or current.stat().st_mtime > cutoff_epoch
                        ):
                            continue
                        current.unlink()
                    deleted.append(candidate.name)
                except (FileNotFoundError, HighlightProtocolError, OSError, RuntimeError):
                    continue
        return tuple(deleted)

    def _prune_temporary_orphan_artifacts(
        self,
        *,
        cutoff_epoch: float,
        limit: int,
    ) -> tuple[str, ...]:
        """Delete only stale, nonce-named upload/copy fragments in ``tmp``."""

        if limit <= 0:
            return ()
        try:
            candidates = sorted(self.tmp_root.iterdir(), key=lambda path: path.name)
        except OSError:
            return ()
        deleted: list[str] = []
        for candidate in candidates:
            if len(deleted) >= limit:
                break
            match = _MANAGED_TMP_ARTIFACT_RE.fullmatch(candidate.name)
            if match is None or candidate.is_symlink():
                continue
            try:
                validate_job_id(match.group("job_id"))
                resolved = candidate.resolve(strict=True)
                if (
                    resolved.parent != self.tmp_root
                    or not resolved.is_file()
                    or resolved.stat().st_mtime > cutoff_epoch
                ):
                    continue
                if candidate.is_symlink():
                    continue
                current = candidate.resolve(strict=True)
                if (
                    current != resolved
                    or current.parent != self.tmp_root
                    or not current.is_file()
                    or current.stat().st_mtime > cutoff_epoch
                ):
                    continue
                current.unlink()
                deleted.append(f"tmp/{candidate.name}")
            except (FileNotFoundError, HighlightProtocolError, OSError, RuntimeError):
                continue
        return tuple(deleted)

    def _validated_artifact_paths(self, row: sqlite3.Row) -> tuple[Path, ...]:
        """Validate every persisted artifact before deleting any of them."""

        job_id = validate_job_id(str(row["job_id"]))
        managed_paths: list[Path] = []
        for raw_path, expected_parent, expected_suffix in (
            (row["demo_path"], self.demo_root, ".dem"),
            (row["output_path"], self.output_root, ".mp4"),
        ):
            path = self._validated_artifact_path(
                raw_path,
                expected_parent=expected_parent,
                expected_suffix=expected_suffix,
                job_id=job_id,
            )
            if path is not None:
                managed_paths.append(path)
        return tuple(managed_paths)

    @staticmethod
    def _validated_artifact_path(
        raw_path: object,
        *,
        expected_parent: Path,
        expected_suffix: str,
        job_id: str,
    ) -> Path | None:
        if not raw_path:
            return None
        validated_job_id = validate_job_id(job_id)
        candidate = Path(str(raw_path))
        try:
            if candidate.is_symlink():
                raise HighlightProtocolError(
                    "stored highlight path must not be a symbolic link"
                )
            path = candidate.resolve()
        except (OSError, RuntimeError) as exc:
            raise HighlightProtocolError("stored highlight path is invalid") from exc
        expected = expected_parent / f"{validated_job_id}{expected_suffix}"
        if path != expected or path.parent != expected_parent:
            raise HighlightProtocolError("stored highlight path is outside managed storage")
        return path

    def _authorized_lease(
        self, job_id: str, *, worker_id: str, lease_token: str
    ) -> sqlite3.Row:
        validate_job_id(job_id)
        supplied_hash = hashlib.sha256(lease_token.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM highlight_worker_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if (
            row is None
            or str(row["lease_worker"]) != worker_id
            or str(row["status"]) not in {"leased", "rendering"}
            or int(row["lease_until"]) < int(time.time())
            or not hmac.compare_digest(str(row["lease_token_hash"]), supplied_hash)
        ):
            raise HighlightProtocolError("invalid or expired highlight lease")
        return row
