"""Optional Perfect World Arena demo downloader adapter.

The third-party package is imported lazily and only when this source is
explicitly enabled.  Signed URLs contain the PWA access token, so this module
never logs URLs, request headers, or upstream exception text.
"""

from __future__ import annotations

import asyncio
import bz2
import ipaddress
import os
import socket
import stat
import uuid
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib import import_module, metadata
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Protocol, cast
from urllib.parse import urljoin, urlsplit

import httpx

from .demo_source import DemoRequest, DemoSourceError, PwaDemoProvider

PWA_DEMO_ENABLED_ENV = "PWCS2_PWA_DEMO_ENABLED"
PWA_DEMO_TOKEN_ENV = "PERFECTWORLD_PWA_ACCESS_TOKEN"
PWA_DEMO_PUBLIC_IP_ENV = "PWCS2_PWA_PUBLIC_IP"
PWA_DEMO_ACCOUNT_STEAM_ID_ENV = "PERFECTWORLD_MY_STEAM_ID"
_UPSTREAM_DISTRIBUTION = "cs-demo-downloader"
_SUPPORTED_UPSTREAM_VERSION = "1.1.0"
_DEMO_MAGIC = b"PBDEMS2\x00"
_LEGACY_DEMO_MAGIC = b"HL2DEMO\x00"
_DEMO_MAGICS = (_DEMO_MAGIC, _LEGACY_DEMO_MAGIC)
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 5


class _PwaFunctions(Protocol):
    def get_demo_url(self, match_id: str, access_token: str) -> str: ...

    def build_download_headers(
        self,
        steam_id: str,
        public_ip: str | None = None,
    ) -> Mapping[str, str]: ...


class _HttpClientFactory(Protocol):
    def __call__(self) -> httpx.Client: ...


@dataclass(frozen=True, slots=True)
class _ImportedPwaFunctions:
    get_demo_url: Callable[[str, str], str]
    build_download_headers: Callable[[str], Mapping[str, str]]


@dataclass(frozen=True, slots=True)
class PwaDemoAdapterSettings:
    """Non-secret limits plus credentials sourced from the environment."""

    access_token: str = field(repr=False)
    public_ip: str | None = None
    account_steam_id: str | None = None
    timeout_seconds: float = 180.0
    max_download_bytes: int = 600 * 1024 * 1024
    max_demo_bytes: int = 512 * 1024 * 1024

    def __post_init__(self) -> None:
        _validate_token(self.access_token)
        public_ip = (self.public_ip or "").strip()
        if public_ip:
            try:
                address = ipaddress.ip_address(public_ip)
            except ValueError:
                raise DemoSourceError("PWA Demo public IP is invalid") from None
            if not isinstance(address, ipaddress.IPv4Address) or not address.is_global:
                raise DemoSourceError("PWA Demo public IP must be a public IPv4 address")
            object.__setattr__(self, "public_ip", str(address))
        else:
            object.__setattr__(self, "public_ip", None)
        account_steam_id = (self.account_steam_id or "").strip()
        if account_steam_id and (
            len(account_steam_id) != 17 or not account_steam_id.isdecimal()
        ):
            raise DemoSourceError("PWA Demo account SteamID64 is invalid")
        object.__setattr__(self, "account_steam_id", account_steam_id or None)
        if not 5 <= self.timeout_seconds <= 900:
            raise ValueError("timeout_seconds must be between 5 and 900")
        if self.max_demo_bytes < len(_DEMO_MAGIC):
            raise ValueError("max_demo_bytes is too small")
        if self.max_download_bytes < len(_DEMO_MAGIC):
            raise ValueError("max_download_bytes is too small")


class PwaDownloadAdapter:
    """Async provider fetcher backed by ``cs-demo-downloader==1.1.0``.

    All synchronous signing, public-IP discovery, HTTP, and archive work is
    moved to a worker thread so the QQ gateway event loop stays responsive.
    """

    __slots__ = ("_client_factory", "_functions", "_settings")

    def __init__(
        self,
        settings: PwaDemoAdapterSettings,
        *,
        functions: _PwaFunctions | None = None,
        client_factory: _HttpClientFactory | None = None,
    ) -> None:
        self._settings = settings
        self._functions = functions
        self._client_factory = client_factory or self._default_client

    def __repr__(self) -> str:
        return "PwaDownloadAdapter(configured=True)"

    async def fetch(self, request: DemoRequest, staging_directory: Path) -> Path | None:
        if request.platform not in {"perfectworld", "pwa"}:
            return None
        try:
            return await asyncio.to_thread(self._fetch_sync, request, staging_directory)
        except DemoSourceError:
            raise
        except Exception:
            raise DemoSourceError("PWA Demo download failed") from None

    @staticmethod
    def _default_client() -> httpx.Client:
        return httpx.Client(follow_redirects=False, trust_env=False)

    def _fetch_sync(self, request: DemoRequest, staging_directory: Path) -> Path | None:
        staging = _resolve_staging_directory(staging_directory)
        functions = self._functions or _load_pwa_functions()
        try:
            signed_url = functions.get_demo_url(
                _pwa_download_match_id(request.match_id),
                self._settings.access_token,
            )
            if len(request.steam_id) != 17 or not request.steam_id.isdecimal():
                raise DemoSourceError("PWA Demo request requires a SteamID64")
            headers = dict(
                functions.build_download_headers(
                    self._settings.account_steam_id or request.steam_id,
                    public_ip=self._settings.public_ip,
                )
            )
        except Exception:
            raise DemoSourceError("PWA Demo signing failed") from None

        _validate_initial_url(signed_url)
        temporary = staging / f".{uuid.uuid4().hex}.download"
        _create_private_file_placeholder(temporary)
        try:
            downloaded = self._download(signed_url, headers, temporary)
            if not downloaded:
                return None
            return _materialize_demo(
                temporary,
                staging,
                max_demo_bytes=self._settings.max_demo_bytes,
            )
        finally:
            temporary.unlink(missing_ok=True)

    def _download(self, signed_url: str, headers: Mapping[str, str], target: Path) -> bool:
        current_url = signed_url
        with self._client_factory() as client:
            for redirect_count in range(_MAX_REDIRECTS + 1):
                try:
                    with client.stream(
                        "GET",
                        current_url,
                        headers=headers,
                        timeout=httpx.Timeout(self._settings.timeout_seconds),
                    ) as response:
                        if response.status_code in _REDIRECT_STATUSES:
                            if redirect_count >= _MAX_REDIRECTS:
                                raise DemoSourceError("PWA Demo download redirected too many times")
                            location = response.headers.get("location", "")
                            next_url = urljoin(current_url, location)
                            _validate_redirect_url(next_url)
                            current_url = next_url
                            continue
                        if response.status_code in {204, 404, 410}:
                            return False
                        if response.status_code != 200:
                            raise DemoSourceError("PWA Demo is unavailable")
                        content_length = _parse_content_length(
                            response.headers.get("content-length")
                        )
                        if (
                            content_length is not None
                            and content_length > self._settings.max_download_bytes
                        ):
                            raise DemoSourceError("PWA Demo download exceeds the size limit")
                        total = 0
                        with target.open("wb") as output:
                            for chunk in response.iter_bytes(1024 * 1024):
                                total += len(chunk)
                                if total > self._settings.max_download_bytes:
                                    raise DemoSourceError(
                                        "PWA Demo download exceeds the size limit"
                                    )
                                output.write(chunk)
                            output.flush()
                            os.fsync(output.fileno())
                        if total == 0:
                            return False
                        return True
                except DemoSourceError:
                    raise
                except (httpx.HTTPError, OSError, ValueError):
                    raise DemoSourceError("PWA Demo download failed") from None
        raise DemoSourceError("PWA Demo download failed")


def _pwa_download_match_id(match_id: str) -> str:
    """Translate the bot's namespaced Perfect World ID to the PWA file ID."""

    if not match_id.startswith("PVP@"):
        return match_id
    numeric = match_id.removeprefix("PVP@")
    if not numeric.isdecimal():
        raise DemoSourceError("PWA Demo match id is invalid")
    return numeric


def create_pwa_demo_provider(
    access_token: str,
    *,
    public_ip: str | None = None,
    account_steam_id: str | None = None,
    timeout_seconds: float = 180.0,
    max_download_bytes: int = 600 * 1024 * 1024,
    max_demo_bytes: int = 512 * 1024 * 1024,
) -> PwaDemoProvider:
    """Create an enabled provider from credentials already read securely."""
    settings = PwaDemoAdapterSettings(
        access_token=access_token,
        public_ip=public_ip,
        account_steam_id=account_steam_id,
        timeout_seconds=timeout_seconds,
        max_download_bytes=max_download_bytes,
        max_demo_bytes=max_demo_bytes,
    )
    adapter = PwaDownloadAdapter(settings)
    return PwaDemoProvider(adapter.fetch)


def create_pwa_demo_provider_from_env(
    environ: Mapping[str, str] | None = None,
) -> PwaDemoProvider | None:
    """Return an opt-in provider, without importing its dependency when disabled."""
    values = os.environ if environ is None else environ
    if not _env_enabled(values.get(PWA_DEMO_ENABLED_ENV, "")):
        return None
    token = values.get(PWA_DEMO_TOKEN_ENV, "")
    public_ip = values.get(PWA_DEMO_PUBLIC_IP_ENV, "")
    account_steam_id = values.get(PWA_DEMO_ACCOUNT_STEAM_ID_ENV, "")
    return create_pwa_demo_provider(
        token,
        public_ip=public_ip or None,
        account_steam_id=account_steam_id or None,
    )


def _load_pwa_functions() -> _ImportedPwaFunctions:
    try:
        installed_version = metadata.version(_UPSTREAM_DISTRIBUTION)
        if installed_version != _SUPPORTED_UPSTREAM_VERSION:
            raise DemoSourceError("Unsupported PWA Demo downloader version")
        module = import_module("cs_demo_downloader.core.downloader_pwa")
        get_demo_url = _callable_attribute(module, "get_demo_url")
        build_download_headers = _callable_attribute(module, "build_download_headers")
    except DemoSourceError:
        raise
    except Exception:
        raise DemoSourceError(
            "PWA Demo downloader is not installed; install the pwa-demo extra"
        ) from None
    return _ImportedPwaFunctions(
        get_demo_url=cast(Callable[[str, str], str], get_demo_url),
        build_download_headers=cast(
            Callable[[str], Mapping[str, str]],
            build_download_headers,
        ),
    )


def _callable_attribute(module: ModuleType, name: str) -> Callable[..., object]:
    value = getattr(module, name, None)
    if not callable(value):
        raise DemoSourceError("PWA Demo downloader API is incompatible")
    return value


def _resolve_staging_directory(path: Path) -> Path:
    try:
        if path.is_symlink():
            raise DemoSourceError("PWA Demo staging directory must not be a symbolic link")
        resolved = path.resolve(strict=True)
    except DemoSourceError:
        raise
    except OSError:
        raise DemoSourceError("PWA Demo staging directory is unavailable") from None
    if not resolved.is_dir():
        raise DemoSourceError("PWA Demo staging directory is unavailable")
    if os.name != "nt":
        try:
            resolved.chmod(0o700)
        except OSError:
            raise DemoSourceError("PWA Demo staging directory could not be secured") from None
    return resolved


def _create_private_file_placeholder(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
    except OSError:
        raise DemoSourceError("PWA Demo staging file could not be created") from None


def _materialize_demo(
    archive: Path,
    staging: Path,
    *,
    max_demo_bytes: int,
) -> Path | None:
    try:
        with archive.open("rb") as source:
            prefix = source.read(8)
    except OSError:
        raise DemoSourceError("PWA Demo download could not be read") from None

    destination = staging / f"{uuid.uuid4().hex}.dem"
    if prefix in _DEMO_MAGICS:
        _copy_limited(archive, destination, max_demo_bytes=max_demo_bytes)
        return destination
    if prefix.startswith(b"PK\x03\x04"):
        _extract_zip_demo(archive, destination, max_demo_bytes=max_demo_bytes)
        return destination
    if prefix.startswith(b"BZh"):
        _extract_bz2_demo(archive, destination, max_demo_bytes=max_demo_bytes)
        return destination
    return None


def _copy_limited(source: Path, destination: Path, *, max_demo_bytes: int) -> None:
    try:
        with source.open("rb") as input_file, destination.open("xb") as output_file:
            _copy_stream_limited(input_file, output_file, max_demo_bytes=max_demo_bytes)
            output_file.flush()
            os.fsync(output_file.fileno())
        _verify_demo_header(destination)
        _chmod_private(destination)
    except DemoSourceError:
        destination.unlink(missing_ok=True)
        raise
    except OSError:
        destination.unlink(missing_ok=True)
        raise DemoSourceError("PWA Demo could not be staged") from None


def _extract_zip_demo(archive: Path, destination: Path, *, max_demo_bytes: int) -> None:
    try:
        with zipfile.ZipFile(archive) as bundle:
            candidates = [
                info
                for info in bundle.infolist()
                if not info.is_dir() and PurePosixPath(info.filename).suffix.casefold() == ".dem"
            ]
            if len(candidates) != 1:
                raise DemoSourceError("PWA Demo archive must contain exactly one demo")
            info = candidates[0]
            _validate_zip_member(info, max_demo_bytes=max_demo_bytes)
            with bundle.open(info, "r") as input_file, destination.open("xb") as output_file:
                _copy_stream_limited(input_file, output_file, max_demo_bytes=max_demo_bytes)
                output_file.flush()
                os.fsync(output_file.fileno())
        _verify_demo_header(destination)
        _chmod_private(destination)
    except DemoSourceError:
        destination.unlink(missing_ok=True)
        raise
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
        destination.unlink(missing_ok=True)
        raise DemoSourceError("PWA Demo archive is invalid") from None


def _validate_zip_member(info: zipfile.ZipInfo, *, max_demo_bytes: int) -> None:
    normalized = PurePosixPath(info.filename.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts:
        raise DemoSourceError("PWA Demo archive contains an unsafe path")
    unix_mode = info.external_attr >> 16
    if unix_mode and stat.S_ISLNK(unix_mode):
        raise DemoSourceError("PWA Demo archive contains a symbolic link")
    if info.flag_bits & 0x1:
        raise DemoSourceError("PWA Demo archive must not be encrypted")
    if info.file_size > max_demo_bytes:
        raise DemoSourceError("PWA Demo exceeds the configured size limit")


def _extract_bz2_demo(archive: Path, destination: Path, *, max_demo_bytes: int) -> None:
    try:
        with bz2.open(archive, "rb") as input_file, destination.open("xb") as output_file:
            _copy_stream_limited(input_file, output_file, max_demo_bytes=max_demo_bytes)
            output_file.flush()
            os.fsync(output_file.fileno())
        _verify_demo_header(destination)
        _chmod_private(destination)
    except DemoSourceError:
        destination.unlink(missing_ok=True)
        raise
    except (OSError, EOFError):
        destination.unlink(missing_ok=True)
        raise DemoSourceError("PWA Demo archive is invalid") from None


def _copy_stream_limited(input_file: object, output_file: object, *, max_demo_bytes: int) -> None:
    total = 0
    while True:
        chunk = input_file.read(1024 * 1024)  # type: ignore[attr-defined]
        if not chunk:
            break
        total += len(chunk)
        if total > max_demo_bytes:
            raise DemoSourceError("PWA Demo exceeds the configured size limit")
        output_file.write(chunk)  # type: ignore[attr-defined]


def _verify_demo_header(path: Path) -> None:
    try:
        with path.open("rb") as demo:
            header = demo.read(8)
    except OSError:
        raise DemoSourceError("PWA Demo could not be verified") from None
    if header not in _DEMO_MAGICS:
        raise DemoSourceError("PWA Demo has an invalid file header")


def _chmod_private(path: Path) -> None:
    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError:
            raise DemoSourceError("PWA Demo staging file could not be secured") from None


def _parse_content_length(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _validate_initial_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "pwaweblogin.wmpvp.com"
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise DemoSourceError("PWA Demo signer returned an invalid endpoint")


def _validate_redirect_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise DemoSourceError("PWA Demo redirect endpoint is invalid")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError:
        raise DemoSourceError("PWA Demo redirect endpoint could not be resolved") from None
    if not addresses:
        raise DemoSourceError("PWA Demo redirect endpoint could not be resolved")
    for address in addresses:
        candidate = ipaddress.ip_address(address[4][0])
        if not candidate.is_global:
            raise DemoSourceError("PWA Demo redirect endpoint is not public")


def _validate_token(token: str) -> None:
    if not 16 <= len(token) <= 4096:
        raise DemoSourceError("PWA Demo access token is not configured")
    if any(character.isspace() or ord(character) < 32 for character in token):
        raise DemoSourceError("PWA Demo access token is invalid")


def _env_enabled(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"", "0", "false", "no", "off"}:
        return False
    if normalized in {"1", "true", "yes", "on"}:
        return True
    raise DemoSourceError(f"{PWA_DEMO_ENABLED_ENV} must be true or false")
