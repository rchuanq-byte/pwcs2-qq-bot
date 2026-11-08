"""Safe OneBot video delivery for completed highlight renders."""

from __future__ import annotations

import asyncio
import base64
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlparse

from pwcs2_bot.highlight_protocol import HighlightProtocolError

OneBotAction = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
PublicURL = Callable[[Path], str | None]
ReadyCheck = Callable[[], bool]


class HighlightPreDispatchError(HighlightProtocolError):
    """A retryable failure proven to have occurred before OneBot was called."""


@dataclass(frozen=True, slots=True)
class PreparedHighlightVideo:
    """A fully validated OneBot request that has not been dispatched yet."""

    action: str
    params: dict[str, Any]


class HighlightVideoDelivery:
    """Send a rendered MP4 as a real OneBot video segment.

    Nanobot 0.2.2 treats every ``OutboundMessage.media`` item as an image.
    This adapter therefore calls NapCat's OneBot action directly.  Small files
    use base64; larger files require a trusted HTTPS URL supplied by the host.
    """

    def __init__(
        self,
        call_action: OneBotAction,
        *,
        allowed_groups: set[str] | tuple[str, ...] = (),
        max_inline_bytes: int = 16 * 1024 * 1024,
        public_url: PublicURL | None = None,
        trusted_public_hosts: set[str] | tuple[str, ...] = (),
        ready_check: ReadyCheck | None = None,
        napcat_media_root: str = "",
    ) -> None:
        self.call_action = call_action
        self.allowed_groups = {str(item) for item in allowed_groups}
        self.max_inline_bytes = max(1, int(max_inline_bytes))
        self.public_url = public_url
        self.ready_check = ready_check
        root = str(napcat_media_root).strip()
        if root and (
            not root.startswith("/")
            or "\x00" in root
            or ".." in PurePosixPath(root).parts
        ):
            raise ValueError("NapCat media root must be an absolute safe POSIX path")
        self.napcat_media_root = root.rstrip("/")
        self.trusted_public_hosts = {
            str(host).casefold().rstrip(".") for host in trusted_public_hosts if str(host)
        }

    async def send(
        self,
        group_id: str,
        video_path: str | Path,
        *,
        caption: str = "",
        mention_qq_id: str = "",
    ) -> dict[str, Any]:
        prepared = await self.prepare(
            group_id, video_path, caption=caption, mention_qq_id=mention_qq_id
        )
        return await self.dispatch(prepared)

    async def prepare(
        self,
        group_id: str,
        video_path: str | Path,
        *,
        caption: str = "",
        mention_qq_id: str = "",
    ) -> PreparedHighlightVideo:
        """Validate and materialize a request without contacting OneBot.

        The queue claims the job only after this method succeeds.  Therefore
        filesystem, size, URL, and base64 failures are known pre-dispatch
        failures and remain safe to retry.
        """

        self.preflight()
        group_id = str(group_id)
        if not group_id.isdigit() or (
            self.allowed_groups and group_id not in self.allowed_groups
        ):
            raise HighlightProtocolError("highlight target group is not allowed")
        mention_qq_id = str(mention_qq_id).strip()
        if mention_qq_id and re.fullmatch(r"[1-9][0-9]{4,19}", mention_qq_id) is None:
            raise HighlightProtocolError("highlight mention must be a single valid QQ ID")
        path, size = await asyncio.to_thread(_resolve_video, video_path)
        if path.suffix.casefold() != ".mp4" or size <= 0 or not await asyncio.to_thread(
            _has_mp4_signature, path
        ):
            raise HighlightProtocolError("highlight output must be a valid MP4 file")
        if self.napcat_media_root:
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,127}\.mp4", path.name) is None:
                raise HighlightProtocolError("highlight output name is unsafe for NapCat")
            media_path = str(PurePosixPath(self.napcat_media_root) / path.name)
            file_reference = "file://" + quote(media_path, safe="/")
        elif size <= self.max_inline_bytes:
            raw = await asyncio.to_thread(path.read_bytes)
            file_reference = "base64://" + base64.b64encode(raw).decode("ascii")
        else:
            if self.public_url is None:
                raise HighlightProtocolError(
                    "highlight is too large for inline QQ delivery and no public URL is configured"
                )
            file_reference = str(self.public_url(path) or "")
            self._validate_public_url(file_reference)
        message: list[dict[str, Any]] = [
            {"type": "video", "data": {"file": file_reference}}
        ]
        if mention_qq_id:
            message.append({"type": "at", "data": {"qq": mention_qq_id}})
        normalized_caption = " ".join(caption.split())
        if normalized_caption:
            message.append(
                {
                    "type": "text",
                    "data": {"text": normalized_caption[:500]},
                }
            )
        return PreparedHighlightVideo(
            action="send_group_msg",
            params={"group_id": int(group_id), "message": message},
        )

    async def dispatch(self, prepared: PreparedHighlightVideo) -> dict[str, Any]:
        """Contact OneBot once; callers must treat exceptions as ambiguous."""

        if not isinstance(prepared, PreparedHighlightVideo):
            raise TypeError("prepared highlight request is invalid")
        self.preflight()
        return await self.call_action(prepared.action, prepared.params)

    def preflight(self) -> None:
        """Fail retryably when the transport is known to be disconnected."""

        if self.ready_check is not None and not self.ready_check():
            raise HighlightPreDispatchError("OneBot transport is not connected")

    def _validate_public_url(self, value: str) -> None:
        parsed = urlparse(value)
        hostname = (parsed.hostname or "").casefold().rstrip(".")
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or not hostname
            or parsed.fragment
            or hostname not in self.trusted_public_hosts
        ):
            raise HighlightProtocolError("highlight public URL is not on a trusted HTTPS host")


def _has_mp4_signature(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            header = handle.read(32)
    except OSError:
        return False
    return len(header) >= 12 and header[4:8] == b"ftyp"


def _resolve_video(video_path: str | Path) -> tuple[Path, int]:
    try:
        source = Path(video_path)
        if source.is_symlink():
            raise HighlightProtocolError("highlight output must not be a symbolic link")
        path = source.resolve(strict=True)
        return path, path.stat().st_size
    except HighlightProtocolError:
        raise
    except OSError:
        raise HighlightProtocolError("highlight output is unavailable") from None
