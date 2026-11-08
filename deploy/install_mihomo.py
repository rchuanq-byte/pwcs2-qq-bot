#!/usr/bin/env python3
"""Download a verified official Mihomo binary for the private sidecar image."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import platform
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

VERSION = "1.19.29"
TAG = f"v{VERSION}"
RELEASE_API_URL = (
    f"https://api.github.com/repos/MetaCubeX/mihomo/releases/tags/{TAG}"
)
MAX_RELEASE_METADATA_BYTES = 8 * 1024 * 1024
MAX_COMPRESSED_BYTES = 64 * 1024 * 1024
MAX_BINARY_BYTES = 128 * 1024 * 1024
ALLOWED_DOWNLOAD_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
ASSET_NAMES = {
    "x86_64": f"mihomo-linux-amd64-compatible-{TAG}.gz",
    "amd64": f"mihomo-linux-amd64-compatible-{TAG}.gz",
    "aarch64": f"mihomo-linux-arm64-{TAG}.gz",
    "arm64": f"mihomo-linux-arm64-{TAG}.gz",
}
DOCKERFILE = """\
FROM scratch
COPY mihomo /mihomo
COPY ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
USER 1000:1000
ENTRYPOINT ["/mihomo"]
"""
DOCKERIGNORE = """\
*
!Dockerfile
!.dockerignore
!mihomo
!ca-certificates.crt
"""


class InstallationError(RuntimeError):
    """A safe-to-display installation failure."""


def asset_name_for_machine(machine: str) -> str:
    normalized = machine.strip().casefold()
    try:
        return ASSET_NAMES[normalized]
    except KeyError as exc:
        raise InstallationError(f"暂不支持该 CPU 架构：{machine}") from exc


def read_limited(response: object, limit: int) -> bytes:
    data = response.read(limit + 1)  # type: ignore[attr-defined]
    if len(data) > limit:
        raise InstallationError("Mihomo 发布元数据体积异常")
    return data


def find_release_asset(payload: object, asset_name: str) -> tuple[str, str, int]:
    if not isinstance(payload, dict) or payload.get("tag_name") != TAG:
        raise InstallationError("GitHub 返回的 Mihomo 版本不匹配")
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise InstallationError("GitHub 返回的 Mihomo 发布信息格式异常")
    asset = next(
        (
            item
            for item in assets
            if isinstance(item, dict) and item.get("name") == asset_name
        ),
        None,
    )
    if asset is None:
        raise InstallationError("官方发布中未找到当前架构的 Mihomo 文件")

    url = asset.get("browser_download_url")
    digest = asset.get("digest")
    size = asset.get("size")
    expected_prefix = (
        f"https://github.com/MetaCubeX/mihomo/releases/download/{TAG}/"
    )
    if not isinstance(url, str) or not url.startswith(expected_prefix):
        raise InstallationError("官方 Mihomo 下载地址格式异常")
    if (
        not isinstance(digest, str)
        or not digest.startswith("sha256:")
        or len(digest) != 71
    ):
        raise InstallationError("官方 Mihomo 文件缺少 SHA-256 摘要")
    expected_digest = digest.removeprefix("sha256:").casefold()
    if any(character not in "0123456789abcdef" for character in expected_digest):
        raise InstallationError("官方 Mihomo SHA-256 摘要格式异常")
    if (
        not isinstance(size, int)
        or size <= 0
        or size > MAX_COMPRESSED_BYTES
    ):
        raise InstallationError("官方 Mihomo 文件体积异常")
    return url, expected_digest, size


def request(url: str) -> object:
    return urllib.request.urlopen(
        urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "pwcs2-qq-bot-mihomo-installer/1",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        ),
        timeout=30,
    )


def download_verified_gzip(
    url: str,
    expected_digest: str,
    expected_size: int,
    destination: Path,
) -> None:
    digest = hashlib.sha256()
    total = 0
    with request(url) as response, destination.open("wb") as handle:
        final_host = (urlsplit(response.geturl()).hostname or "").casefold()
        if final_host not in ALLOWED_DOWNLOAD_HOSTS:
            raise InstallationError("Mihomo 下载发生了不安全的重定向")
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_COMPRESSED_BYTES:
                raise InstallationError("Mihomo 下载文件体积异常")
            digest.update(chunk)
            handle.write(chunk)
    if total != expected_size:
        raise InstallationError("Mihomo 下载文件大小与官方记录不一致")
    if digest.hexdigest().casefold() != expected_digest:
        raise InstallationError("Mihomo 下载文件 SHA-256 校验失败")


def verify_local_archive(
    path: Path,
    expected_digest: str,
    expected_size: int,
) -> None:
    if not path.is_file() or path.stat().st_size != expected_size:
        raise InstallationError("本地 Mihomo 文件大小与官方记录不一致")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest().casefold() != expected_digest:
        raise InstallationError("本地 Mihomo 文件 SHA-256 校验失败")


def decompress_binary(source: Path, destination: Path) -> None:
    total = 0
    with gzip.open(source, "rb") as compressed, destination.open("wb") as output:
        while chunk := compressed.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_BINARY_BYTES:
                raise InstallationError("Mihomo 解压后体积异常")
            output.write(chunk)
    if total < 1024 * 1024:
        raise InstallationError("Mihomo 解压后的程序体积异常")
    with destination.open("rb") as handle:
        if handle.read(4) != b"\x7fELF":
            raise InstallationError("Mihomo 文件不是有效的 Linux ELF 程序")


def write_text_atomic(path: Path, text: str, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
        path.chmod(mode)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def install(
    root: Path,
    machine: str,
    *,
    archive_path: Path | None = None,
) -> tuple[str, str]:
    asset_name = asset_name_for_machine(machine)
    try:
        with request(RELEASE_API_URL) as response:
            raw_metadata = read_limited(response, MAX_RELEASE_METADATA_BYTES)
        payload = json.loads(raw_metadata)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise InstallationError("无法连接 GitHub 获取 Mihomo 官方发布信息") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallationError("GitHub 返回的 Mihomo 发布信息不是有效 JSON") from exc

    url, expected_digest, expected_size = find_release_asset(payload, asset_name)
    image_dir = root / "data" / "mihomo" / "image"
    image_dir.mkdir(parents=True, exist_ok=True)
    image_dir.chmod(0o700)
    ca_source = Path("/etc/ssl/certs/ca-certificates.crt")
    if not ca_source.is_file():
        raise InstallationError("服务器缺少系统 CA 证书包")

    with tempfile.TemporaryDirectory(prefix=".download.", dir=image_dir) as temporary:
        temporary_dir = Path(temporary)
        downloaded_archive_path = temporary_dir / asset_name
        binary_path = temporary_dir / "mihomo"
        try:
            if archive_path is None:
                download_verified_gzip(
                    url,
                    expected_digest,
                    expected_size,
                    downloaded_archive_path,
                )
                source_archive_path = downloaded_archive_path
            else:
                source_archive_path = archive_path.expanduser().resolve()
                verify_local_archive(
                    source_archive_path,
                    expected_digest,
                    expected_size,
                )
            decompress_binary(source_archive_path, binary_path)
        except (urllib.error.URLError, TimeoutError, OSError, gzip.BadGzipFile) as exc:
            if isinstance(exc, InstallationError):
                raise
            raise InstallationError("Mihomo 官方程序下载或解压失败") from exc
        binary_path.chmod(0o755)
        binary_path.replace(image_dir / "mihomo")

    shutil.copyfile(ca_source, image_dir / "ca-certificates.crt")
    (image_dir / "ca-certificates.crt").chmod(0o644)
    write_text_atomic(image_dir / "Dockerfile", DOCKERFILE, 0o600)
    write_text_atomic(image_dir / ".dockerignore", DOCKERIGNORE, 0o600)
    return asset_name, expected_digest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install a SHA-256-verified official Mihomo release.",
    )
    parser.add_argument(
        "--archive",
        type=Path,
        help="Use an already downloaded official .gz asset; it is still verified.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    asset_name, digest = install(
        root,
        platform.machine(),
        archive_path=args.archive,
    )
    print(f"OK 已安装官方 Mihomo {TAG}")
    print(f"asset={asset_name}")
    print(f"sha256={digest}")
    print("下一步：docker compose build steam-mihomo")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (InstallationError, OSError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        raise SystemExit(1) from None
