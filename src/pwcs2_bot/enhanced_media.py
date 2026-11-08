"""Safe QQ media bridge for the upstream video/image-understanding pipeline."""

from __future__ import annotations

import asyncio
import base64
import io
import re
import tempfile
import time
from collections import deque
from pathlib import Path

from PIL import Image

from pwcs2_bot.public_fetch import PublicMediaError, fetch_public_bytes

IMAGE_MIMES = ("image/png", "image/jpeg", "image/webp", "image/gif")
VIDEO_MIMES = ("video/mp4", "video/webm")


async def decoded_job(function, *args):
    """Cancellation must not release the decode slot while its thread still runs."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


def image_grid(raw):
    """Upstream's four-frame GIF strategy, with explicit decoded-pixel limits."""
    with Image.open(io.BytesIO(raw)) as im:
        if im.width * im.height > 16_000_000:
            raise PublicMediaError("Image dimensions exceed the limit")
        frames = []
        count = min(getattr(im, "n_frames", 1), 4)
        for i in range(count):
            im.seek(i)
            frame = im.convert("RGB")
            frame.thumbnail((768, 768))
            frames.append(frame)
    return grid_data_url(frames)


def grid_data_url(frames):
    if not frames:
        raise PublicMediaError("No readable frames")
    width = max(f.width for f in frames)
    height = max(f.height for f in frames)
    columns = 2 if len(frames) > 1 else 1
    canvas = Image.new(
        "RGB", (width * columns, height * ((len(frames) + columns - 1) // columns)), "white"
    )
    for i, frame in enumerate(frames):
        canvas.paste(frame, ((i % columns) * width, (i // columns) * height))
    out = io.BytesIO()
    canvas.save(out, format="PNG")
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode("ascii")


class EnhancedMedia:
    def __init__(self, engine, frame_extractor, *, clock=time.time):
        self.engine, self.frame_extractor, self.clock = engine, frame_extractor, clock
        self._decode = asyncio.Semaphore(1)
        self.recent = {}
        self.video_requests = 0

    def remember(self, gid, segments):
        media = [s for s in segments if s.get("type") in {"image", "video"}]
        if media:
            self.recent.setdefault(str(gid), deque(maxlen=4)).append((self.clock(), media))

    def previous(self, gid, prompt, kind):
        pattern = r"(?:刚才|刚刚|上面|上一|之前|那张|那段).{0,12}(?:图|视频)|(?:这张图|这段视频)"
        if not re.search(pattern, prompt):
            return []
        for stamp, items in reversed(self.recent.get(str(gid), ())):
            if 0 <= self.clock() - stamp <= 300:
                found = [s for s in items if s.get("type") == kind]
                if found:
                    return found[:1]
        return []

    async def read_image(self, source):
        async with self._decode:
            raw = await fetch_public_bytes(
                source,
                maximum=min(self.engine.config.ai_image_max_bytes, 8 * 1024 * 1024),
                timeout=20,
                data_mimes=IMAGE_MIMES,
            )
            return await decoded_job(image_grid, raw)

    async def describe_video(self, segments):
        if not segments:
            return ""
        self.video_requests += 1
        async with self._decode:
            grid = await self._video_grid(segments)
        return await self.engine.ai.describe_images(
            prompt="这些画面是同一段视频按时间顺序抽出的最多4帧。请描述可见内容和变化。"
            "抽帧可能漏掉快动作，没有音频；不要猜测声音、身份或未出现的内容。",
            image_data_urls=(grid,),
        )

    async def _video_grid(self, segments):
        data = segments[0].get("data") or {}
        source = str(data.get("url") or data.get("file") or "")
        if not source.startswith(("http://", "https://", "data:video/")):
            response = await self.engine._call_action("get_file", {"file_id": source})
            info = response.get("data") or {}
            source = str(info.get("url") or info.get("file_url") or "")
        raw = await fetch_public_bytes(
            source, maximum=24 * 1024 * 1024, timeout=30, data_mimes=VIDEO_MIMES
        )
        if raw[4:8] == b"ftyp":
            suffix = ".mp4"
        elif raw[:4] == b"\x1a\x45\xdf\xa3":
            suffix = ".webm"
        else:
            raise PublicMediaError("Only MP4/WebM video attachments are accepted")
        with tempfile.TemporaryDirectory(prefix="niuniu-video-") as directory:
            path = Path(directory) / ("input" + suffix)
            await asyncio.to_thread(path.write_bytes, raw)
            frames = await decoded_job(self.frame_extractor(), str(path), 4)
            try:
                if not frames:
                    raise PublicMediaError("Video has no readable frames")

                def compose():
                    pictures = []
                    for filename in frames[:4]:
                        with Image.open(filename) as im:
                            if im.width * im.height > 2_000_000:
                                raise PublicMediaError("Decoded video frame is too large")
                            pic = im.convert("RGB")
                            pic.thumbnail((448, 448))
                            pictures.append(pic)
                    return grid_data_url(pictures)

                return await decoded_job(compose)
            finally:
                for filename in frames:
                    p = Path(filename)
                    if p.name.startswith("f_") and p.suffix == ".jpg":
                        await asyncio.to_thread(p.unlink, missing_ok=True)
