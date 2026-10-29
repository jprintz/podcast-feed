"""Shared pytest fixtures. Later tasks add to this file."""

import asyncio
from dataclasses import dataclass

import pytest

_TLS_HANDSHAKE = 0x16


@dataclass
class LocalHttpServer:
    """What the local test server saw of the last request it was sent."""

    port: int = 0
    last_host: str | None = None
    # The first TLS record of a TLS connection (the ClientHello, which carries the SNI).
    last_tls_hello: bytes | None = None


@pytest.fixture
async def local_http_server():
    """An HTTP server on 127.0.0.1 that answers every request with `200 ok`, then hangs up.

    A TLS connection gets its ClientHello recorded and is hung up on.
    """
    seen = LocalHttpServer()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            start = await reader.readexactly(5)
            if start[0] == _TLS_HANDSHAKE:
                length = int.from_bytes(start[3:5], "big")
                seen.last_tls_hello = start + await reader.readexactly(length)
                return
            head = (start + await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            for line in head.split("\r\n")[1:]:
                name, _, value = line.partition(":")
                if name.strip().lower() == "host":
                    seen.last_host = value.strip()
            writer.write(
                b"HTTP/1.1 200 OK\r\ncontent-type: text/plain\r\ncontent-length: 2\r\n"
                b"connection: close\r\n\r\nok"
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    seen.port = server.sockets[0].getsockname()[1]
    async with server:
        yield seen
