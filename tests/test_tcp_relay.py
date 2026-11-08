from __future__ import annotations

import asyncio

from pwcs2_bot.tcp_relay import create_relay_server


async def test_tcp_relay_forwards_bidirectionally() -> None:
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.read(1024)
        writer.write(b"echo:" + data)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    upstream_port = upstream.sockets[0].getsockname()[1]
    relay = await create_relay_server("127.0.0.1", 0, "127.0.0.1", upstream_port)
    relay_port = relay.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)
        writer.write(b"hello")
        await writer.drain()
        assert await reader.read(1024) == b"echo:hello"
        writer.close()
        await writer.wait_closed()
    finally:
        relay.close()
        upstream.close()
        await relay.wait_closed()
        await upstream.wait_closed()
