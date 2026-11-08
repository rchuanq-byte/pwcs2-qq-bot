"""Bounded public-only downloads shared by web and multimodal extensions."""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import aiohttp


class PublicMediaError(ValueError):
    pass


def validate_public_url(url):
    try:
        p = urlsplit(url)
        if p.scheme not in {"https", "http"} or not p.hostname or p.username or p.password:
            raise ValueError
        if p.port not in {None, 80, 443} or "%" in p.hostname:
            raise ValueError
        try:
            address = ipaddress.ip_address(p.hostname)
        except ValueError:
            if p.hostname.casefold() == "localhost" or p.hostname.casefold().endswith(".localhost"):
                raise PublicMediaError("Only public hosts are allowed") from None
        else:
            if not address.is_global:
                raise PublicMediaError("Only public hosts are allowed")
    except (ValueError, TypeError) as exc:
        raise PublicMediaError("Only public HTTP(S) URLs on standard ports are allowed") from exc


class PublicResolver(aiohttp.abc.AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, family, socket.SOCK_STREAM)
        result = []
        for fam, _type, proto, _canonical, addr in infos:
            address = addr[0].split("%")[0]
            if not ipaddress.ip_address(address).is_global:
                raise PublicMediaError("DNS resolved to a non-public address")
            result.append(
                {
                    "hostname": host,
                    "host": address,
                    "port": port,
                    "family": fam,
                    "proto": proto,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        if not result:
            raise PublicMediaError("No public address found")
        return result

    async def close(self):
        pass


async def fetch_public_bytes(source, *, maximum=8 * 1024 * 1024, timeout=20, data_mimes=()):  # noqa: ASYNC109
    """Never reads a local path or accepts private/redirect/rebinding destinations."""
    if source.startswith("data:"):
        try:
            header, payload = source.split(",", 1)
            if header not in {f"data:{mime};base64" for mime in data_mimes}:
                raise ValueError
            if len(payload) > (maximum + 2) // 3 * 4:
                raise ValueError
            raw = base64.b64decode(payload, validate=True)
            if len(raw) > maximum:
                raise ValueError
            return raw
        except (ValueError, binascii.Error) as exc:
            raise PublicMediaError("Invalid or oversized media data") from exc
    validate_public_url(source)
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False)
    async with aiohttp.ClientSession(
        connector=connector, trust_env=False, timeout=aiohttp.ClientTimeout(total=timeout)
    ) as session:
        for _ in range(6):
            validate_public_url(source)
            async with session.get(source, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    source = urljoin(source, response.headers.get("Location", ""))
                    continue
                response.raise_for_status()
                if response.content_length is not None and response.content_length > maximum:
                    raise PublicMediaError("Media exceeds size limit")
                result = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    result.extend(chunk)
                    if len(result) > maximum:
                        raise PublicMediaError("Media exceeds size limit")
                return bytes(result)
    raise PublicMediaError("Too many redirects")
