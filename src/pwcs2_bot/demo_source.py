"""Safe acquisition and local import primitives for Counter-Strike demos.

The core deliberately knows nothing about PWA access tokens, signed URLs, or
request headers.  An authenticated adapter may be injected through
``PwaDemoProvider`` after its upstream contract has been verified.  The adapter
must download and unpack one demo inside the supplied staging directory; this
module then validates and atomically moves it into managed storage.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_CS2_DEMO_MAGIC = b"PBDEMS2\x00"
_LEGACY_DEMO_MAGIC = b"HL2DEMO\x00"
_VALID_DEMO_MAGICS = (_CS2_DEMO_MAGIC, _LEGACY_DEMO_MAGIC)
_SAFE_PROVIDER_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_SAFE_PLATFORM_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_STAGING_DIRECTORY_NAME = re.compile(r"^[0-9a-f]{32}$")


class DemoSourceError(RuntimeError):
    """A credential-safe demo acquisition failure."""


@dataclass(frozen=True, slots=True)
class DemoRequest:
    """The public identifiers needed to acquire one completed match demo."""

    match_id: str
    steam_id: str = ""
    platform: str = "perfectworld"

    def __post_init__(self) -> None:
        match_id = self.match_id.strip()
        steam_id = self.steam_id.strip()
        platform = self.platform.strip().casefold()
        if not match_id or len(match_id) > 160:
            raise ValueError("match_id must contain between 1 and 160 characters")
        if any(ord(character) < 32 or ord(character) == 127 for character in match_id):
            raise ValueError("match_id must not contain control characters")
        if steam_id and (len(steam_id) != 17 or not steam_id.isdecimal()):
            raise ValueError("steam_id must be a SteamID64")
        if not _SAFE_PLATFORM_NAME.fullmatch(platform):
            raise ValueError("platform must be a safe lowercase identifier")
        object.__setattr__(self, "match_id", match_id)
        object.__setattr__(self, "steam_id", steam_id)
        object.__setattr__(self, "platform", platform)


@dataclass(frozen=True, slots=True)
class DemoArtifact:
    """One validated demo stored under the manager-owned directory."""

    path: Path
    match_id: str
    steam_id: str
    platform: str
    provider: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class DemoStorageUsage:
    """Actual regular-file usage below the manager-owned storage root."""

    file_count: int
    demo_count: int
    total_bytes: int


class DemoProvider(Protocol):
    """Pluggable source that writes one unpacked demo into a staging directory."""

    @property
    def name(self) -> str: ...

    async def fetch(self, request: DemoRequest, staging_directory: Path) -> Path | None:
        """Return a local ``.dem`` path, or ``None`` when the demo is unavailable."""


PwaDemoFetcher = Callable[[DemoRequest, Path], Awaitable[Path | None]]


class PwaDemoProvider:
    """Credential-blind adapter boundary for a verified PWA downloader.

    ``fetcher`` owns authentication, signing, HTTP, and archive extraction.  It
    must not log access tokens, signed URLs, or secret headers.  Exceptions are
    intentionally replaced with a generic error so upstream secrets cannot
    escape through logs or chat replies.
    """

    __slots__ = ("_fetcher",)

    def __init__(self, fetcher: PwaDemoFetcher) -> None:
        self._fetcher = fetcher

    @property
    def name(self) -> str:
        return "pwa"

    def __repr__(self) -> str:
        return "PwaDemoProvider(name='pwa')"

    async def fetch(self, request: DemoRequest, staging_directory: Path) -> Path | None:
        if not request.steam_id:
            raise DemoSourceError("PWA Demo provider requires a SteamID64")
        try:
            return await self._fetcher(request, staging_directory)
        except Exception:
            raise DemoSourceError("PWA Demo provider failed") from None


class DemoSourceManager:
    """Validate, deduplicate, and atomically store demos from trusted boundaries."""

    def __init__(
        self,
        storage_path: str | Path,
        import_root: str | Path,
        *,
        max_file_bytes: int = 512 * 1024 * 1024,
        providers: Iterable[DemoProvider] = (),
        shared_root_gid: int | None = None,
    ) -> None:
        if max_file_bytes < len(_CS2_DEMO_MAGIC):
            raise ValueError("max_file_bytes is too small for a demo")
        self.storage_path = Path(storage_path).expanduser().resolve()
        self.import_root = Path(import_root).expanduser().resolve()
        if _contains(self.import_root, self.storage_path) or _contains(
            self.storage_path, self.import_root
        ):
            raise ValueError("storage_path and import_root must be separate directories")
        self.max_file_bytes = max_file_bytes
        self.shared_root_gid = (
            None if shared_root_gid is None else max(0, int(shared_root_gid))
        )
        self._providers: dict[str, DemoProvider] = {}
        for provider in providers:
            self.register_provider(provider)

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(self._providers)

    def register_provider(self, provider: DemoProvider) -> None:
        name = str(provider.name).strip().casefold()
        if not _SAFE_PROVIDER_NAME.fullmatch(name):
            raise ValueError("provider name must be a safe lowercase identifier")
        if name in self._providers:
            raise ValueError(f"duplicate demo provider: {name}")
        self._providers[name] = provider

    def storage_usage(self) -> DemoStorageUsage:
        """Measure all regular files without following links.

        This deliberately includes partial downloads below ``.staging`` and
        completed managed copies.  A controller crash must not make those
        bytes invisible to the admission-control budget on the next start.
        Any unsafe or unreadable entry fails closed instead of reporting
        capacity that may not exist.
        """

        if not self.storage_path.exists():
            return DemoStorageUsage(file_count=0, demo_count=0, total_bytes=0)
        root = _resolve_managed_directory(self.storage_path, self.storage_path)
        file_count = 0
        demo_count = 0
        total_bytes = 0
        pending = [root]
        try:
            while pending:
                directory = pending.pop()
                for entry in os.scandir(directory):
                    if entry.is_symlink():
                        raise DemoSourceError("Managed Demo storage contains a symbolic link")
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        raise DemoSourceError("Managed Demo storage contains an unsafe entry")
                    size = entry.stat(follow_symlinks=False).st_size
                    if size < 0:
                        raise OSError("negative file size")
                    file_count += 1
                    total_bytes += size
                    if Path(entry.name).suffix.casefold() == ".dem":
                        demo_count += 1
        except DemoSourceError:
            raise
        except OSError:
            raise DemoSourceError("Managed Demo storage could not be inspected") from None
        return DemoStorageUsage(
            file_count=file_count,
            demo_count=demo_count,
            total_bytes=total_bytes,
        )

    def cleanup_stale_staging(
        self,
        *,
        older_than_seconds: int = 6 * 60 * 60,
        now: float | None = None,
    ) -> tuple[Path, ...]:
        """Remove abandoned provider staging directories after a safe grace period.

        Only direct, UUID-named directories created by :meth:`acquire` are
        eligible.  Links and nested directories are rejected, and every file
        must have been untouched for the full grace period.  The controller is
        single-threaded, so a live acquisition never races this maintenance.
        """

        grace = int(older_than_seconds)
        if grace < 60:
            raise ValueError("stale Demo staging grace period must be at least 60 seconds")
        staging_root = self.storage_path / ".staging"
        if not staging_root.exists():
            return ()
        root = _resolve_managed_directory(staging_root, self.storage_path)
        cutoff = (time.time() if now is None else float(now)) - grace
        removed: list[Path] = []
        try:
            candidates = tuple(root.iterdir())
        except OSError:
            raise DemoSourceError("Demo staging storage could not be inspected") from None
        for candidate in candidates:
            if candidate.is_symlink():
                raise DemoSourceError("Demo staging storage contains a symbolic link")
            if not candidate.is_dir() or not _STAGING_DIRECTORY_NAME.fullmatch(candidate.name):
                # Unknown entries are retained and counted by storage_usage().
                continue
            resolved = _resolve_managed_directory(candidate, root)
            latest_mtime = _flat_directory_latest_mtime(resolved)
            if latest_mtime > cutoff:
                continue
            try:
                shutil.rmtree(resolved)
            except OSError:
                raise DemoSourceError("Stale Demo staging data could not be removed") from None
            removed.append(resolved)
        return tuple(removed)

    async def import_local(
        self,
        source_path: str | Path,
        request: DemoRequest,
    ) -> DemoArtifact:
        """Import a demo placed below the configured manual-import root."""
        source = self._resolve_candidate(source_path, self.import_root)
        return await asyncio.to_thread(
            self._ingest_sync,
            source,
            request,
            "manual",
        )

    async def acquire(
        self,
        request: DemoRequest,
        *,
        provider_name: str | None = None,
    ) -> DemoArtifact | None:
        """Acquire one demo from a registered provider and store it safely."""
        providers = self._selected_providers(provider_name)
        if not providers:
            return None

        _mkdir_private(
            self.storage_path,
            parents=True,
            exist_ok=True,
            shared_gid=self.shared_root_gid,
        )
        staging_root = self.storage_path / ".staging"
        _mkdir_private(staging_root, parents=True, exist_ok=True)
        for name, provider in providers:
            staging_directory = staging_root / uuid.uuid4().hex
            _mkdir_private(staging_directory)
            try:
                try:
                    candidate = await provider.fetch(request, staging_directory)
                except Exception:
                    raise DemoSourceError(f"{name} Demo provider failed") from None
                if candidate is None:
                    continue
                source = self._resolve_candidate(candidate, staging_directory)
                return await asyncio.to_thread(
                    self._ingest_sync,
                    source,
                    request,
                    name,
                )
            finally:
                await asyncio.to_thread(shutil.rmtree, staging_directory, True)
        return None

    def _selected_providers(
        self,
        provider_name: str | None,
    ) -> tuple[tuple[str, DemoProvider], ...]:
        if provider_name is None:
            return tuple(self._providers.items())
        normalized = provider_name.strip().casefold()
        provider = self._providers.get(normalized)
        if provider is None:
            raise DemoSourceError("Requested Demo provider is not configured")
        return ((normalized, provider),)

    @staticmethod
    def _resolve_candidate(source_path: str | Path, allowed_root: Path) -> Path:
        raw_path = Path(source_path).expanduser()
        candidate = raw_path if raw_path.is_absolute() else allowed_root / raw_path
        try:
            if candidate.is_symlink():
                raise DemoSourceError("Demo source must not be a symbolic link")
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(allowed_root)
        except DemoSourceError:
            raise
        except (OSError, ValueError):
            raise DemoSourceError("Demo source is outside the allowed directory") from None
        if not resolved.is_file():
            raise DemoSourceError("Demo source is not a regular file")
        if resolved.suffix.casefold() != ".dem":
            raise DemoSourceError("Demo source must use the .dem extension")
        return resolved

    def _ingest_sync(
        self,
        source: Path,
        request: DemoRequest,
        provider: str,
    ) -> DemoArtifact:
        destination_directory = self.storage_path / request.platform
        _mkdir_private(destination_directory, parents=True, exist_ok=True)
        temporary_path = destination_directory / f".import-{uuid.uuid4().hex}.part"
        digest = hashlib.sha256()
        total = 0
        try:
            with source.open("rb") as input_file, temporary_path.open("xb") as output_file:
                header = input_file.read(len(_CS2_DEMO_MAGIC))
                if header not in _VALID_DEMO_MAGICS:
                    raise DemoSourceError("Demo source has an invalid file header")
                output_file.write(header)
                digest.update(header)
                total = len(header)
                while chunk := input_file.read(1024 * 1024):
                    total += len(chunk)
                    if total > self.max_file_bytes:
                        raise DemoSourceError("Demo source exceeds the configured size limit")
                    output_file.write(chunk)
                    digest.update(chunk)
                output_file.flush()
                os.fsync(output_file.fileno())

            content_digest = digest.hexdigest()
            match_digest = hashlib.sha256(request.match_id.encode("utf-8")).hexdigest()
            destination = destination_directory / (
                f"{match_digest[:16]}-{content_digest[:16]}.dem"
            )
            if destination.exists():
                if _file_sha256(destination, self.max_file_bytes) != content_digest:
                    raise DemoSourceError("Managed Demo storage contains a conflicting file")
                temporary_path.unlink()
            else:
                os.replace(temporary_path, destination)
                if os.name != "nt":
                    try:
                        destination.chmod(0o600)
                    except OSError:
                        pass
            return DemoArtifact(
                path=destination,
                match_id=request.match_id,
                steam_id=request.steam_id,
                platform=request.platform,
                provider=provider,
                size_bytes=total,
                sha256=content_digest,
            )
        except DemoSourceError:
            temporary_path.unlink(missing_ok=True)
            raise
        except OSError:
            temporary_path.unlink(missing_ok=True)
            raise DemoSourceError("Demo file could not be imported") from None


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _resolve_managed_directory(path: Path, allowed_root: Path) -> Path:
    try:
        if path.is_symlink():
            raise DemoSourceError("Managed Demo directory must not be a symbolic link")
        resolved = path.resolve(strict=True)
        resolved.relative_to(allowed_root)
    except DemoSourceError:
        raise
    except (OSError, ValueError):
        raise DemoSourceError("Managed Demo directory is outside its storage root") from None
    if not resolved.is_dir():
        raise DemoSourceError("Managed Demo directory is unavailable")
    return resolved


def _flat_directory_latest_mtime(directory: Path) -> float:
    """Return latest metadata/content time for a provider-owned flat directory."""

    try:
        latest = directory.stat().st_mtime
        for entry in os.scandir(directory):
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                raise DemoSourceError("Demo staging directory contains an unsafe entry")
            latest = max(latest, entry.stat(follow_symlinks=False).st_mtime)
        return latest
    except DemoSourceError:
        raise
    except OSError:
        raise DemoSourceError("Demo staging directory could not be inspected") from None


def _mkdir_private(
    path: Path,
    *,
    parents: bool = False,
    exist_ok: bool = False,
    shared_gid: int | None = None,
) -> None:
    path.mkdir(parents=parents, exist_ok=exist_ok)
    if os.name != "nt":
        if shared_gid is None:
            path.chmod(0o700)
        else:
            os.chown(path, -1, shared_gid)
            path.chmod(0o770)


def _file_sha256(path: Path, max_file_bytes: int) -> str:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                total += len(chunk)
                if total > max_file_bytes:
                    raise DemoSourceError("Managed Demo storage contains an oversized file")
                digest.update(chunk)
    except DemoSourceError:
        raise
    except OSError:
        raise DemoSourceError("Managed Demo storage could not be verified") from None
    return digest.hexdigest()
