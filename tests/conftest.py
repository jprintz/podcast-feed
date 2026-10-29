"""Shared pytest fixtures. Later tasks add to this file."""

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.media import make_media

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


@pytest.fixture
def ffmpeg_tools() -> None:
    """Skip the test when ffmpeg or ffprobe is missing; CI sets REQUIRE_FFMPEG=1 to fail instead."""
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if not missing:
        return
    reason = f"{' and '.join(missing)} not found on PATH"
    if os.environ.get("REQUIRE_FFMPEG") == "1":
        pytest.fail(f"{reason} (REQUIRE_FFMPEG=1)")
    pytest.skip(reason)


@pytest.fixture
def media(ffmpeg_tools: None, tmp_path: Path):
    """A factory for tiny media files in tmp_path: `media("mp3")`, see tests/media.py for the kinds."""

    def make(kind: str) -> Path:
        return make_media(tmp_path, kind)

    return make
