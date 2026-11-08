"""Restricted TCP relay from Docker's host gateway to an SSH loopback tunnel."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

LISTEN_HOST = "172.17.0.1"
LISTEN_PORT = 18443
UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 18442
MAX_CONNECTIONS = 32
CONNECT_TIMEOUT_SECONDS = 10

logger = logging.getLogger(__name__)


async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65_536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(ConnectionError, OSError):
        await writer.wait_closed()


def relay_handler(
    upstream_host: str,
    upstream_port: int,
    *,
    max_connections: int = MAX_CONNECTIONS,
) -> Callable[
    [asyncio.StreamReader, asyncio.StreamWriter],
    Awaitable[None],
]:
    semaphore = asyncio.Semaphore(max_connections)

    async def handle(
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        if semaphore.locked():
            await _close_writer(client_writer)
            return
        async with semaphore:
            try:
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(upstream_host, upstream_port),
                    timeout=CONNECT_TIMEOUT_SECONDS,
                )
            except (TimeoutError, ConnectionError, OSError):
                await _close_writer(client_writer)
                return

            tasks = {
                asyncio.create_task(_copy(client_reader, upstream_writer)),
                asyncio.create_task(_copy(upstream_reader, client_writer)),
            }
            _done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await _close_writer(upstream_writer)
            await _close_writer(client_writer)

    return handle


async def create_relay_server(
    listen_host: str = LISTEN_HOST,
    listen_port: int = LISTEN_PORT,
    upstream_host: str = UPSTREAM_HOST,
    upstream_port: int = UPSTREAM_PORT,
) -> asyncio.Server:
    handler = relay_handler(upstream_host, upstream_port)
    return await asyncio.start_server(handler, listen_host, listen_port, backlog=64)


async def main() -> None:
    server = await create_relay_server()
    addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets or ())
    logger.warning(
        "AI tunnel relay listening on %s and forwarding to %s:%s",
        addresses,
        UPSTREAM_HOST,
        UPSTREAM_PORT,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
