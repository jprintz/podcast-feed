import asyncio
import inspect
import logging
import traceback
from dataclasses import dataclass, field

import httpx
import pytest

from podcast_feed import __version__
from podcast_feed.errors import EpisodeFailure
from podcast_feed.guard import guarded_client
from podcast_feed.sources import (
    Fetched,
    HttpFileSource,
    UploadSource,
    filename_from,
    upload_filename,
    upload_id,
    upload_ref,
)
from podcast_feed.storage import Storage

MAX_BYTES = 1000
WEB_PAGE = "that link is a web page, not an audio or video file"


@pytest.fixture
async def make_source():
    """A factory for an HttpFileSource whose client answers from a handler (no network)."""
    clients: list[httpx.AsyncClient] = []

    def make(handler, **kwargs) -> HttpFileSource:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        return HttpFileSource(client, **{"max_bytes": MAX_BYTES, **kwargs})

    yield make
    for client in clients:
        await client.aclose()


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b"abc")


def fails(exc: Exception):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


async def endless(chunk: bytes):
    while True:
        yield chunk


async def test_downloads(make_source, tmp_path):
    src = make_source(ok)
    f = await src.fetch("https://x.example/talk.mp3", tmp_path / "dl")
    assert isinstance(f, Fetched)
    assert f.path == tmp_path / "dl"
    assert f.path.read_bytes() == b"abc" and f.filename == "talk.mp3" and f.host == "x.example"


def test_kind(make_source):
    assert make_source(ok).kind == "url"


def test_fetched_is_frozen(tmp_path):
    f = Fetched(tmp_path, None, "x.example")
    with pytest.raises(AttributeError):
        f.host = "y.example"  # type: ignore[misc]


async def test_sends_a_user_agent_and_the_normalised_url(make_source, tmp_path):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=b"abc")

    await make_source(handler).fetch("  HTTPS://X.example/a b.mp3 ", tmp_path / "dl")
    assert [r.method for r in seen] == ["GET"]
    assert str(seen[0].url) == "https://x.example/a%20b.mp3"
    assert seen[0].headers["user-agent"] == f"podcast-feed/{__version__}"


def test_filename_from():
    assert filename_from('attachment; filename="Episode 1.mp3"', "https://x/y") == "Episode 1.mp3"
    assert filename_from("attachment; filename*=UTF-8''L%C3%A5ng.mp3", "https://x/y") == "Lång.mp3"
    assert filename_from(None, "https://x/a/b%20c.mp3?x=1") == "b c.mp3"
    assert filename_from(None, "https://x/") is None


def test_filename_from_prefers_the_extended_form():
    header = "attachment; filename=\"fallback.mp3\"; filename*=UTF-8''L%C3%A5ng.mp3"
    assert filename_from(header, "https://x/y") == "Lång.mp3"


@pytest.mark.parametrize("header", ["attachment", "inline", 'attachment; filename=""', "", ";;;"])
def test_filename_from_header_without_a_name_uses_the_url(header):
    assert filename_from(header, "https://x/a/talk.mp3") == "talk.mp3"


def test_filename_from_unquotes_the_url_once():
    # %2520 is a literal "%20" in the name, not a space.
    assert filename_from(None, "https://x/a%2520b.mp3") == "a%20b.mp3"


@pytest.mark.parametrize(
    "header,url,expected",
    [
        ('attachment; filename="../../etc/passwd"', "https://x/y", "passwd"),
        ('attachment; filename="C:\\music\\talk.mp3"', "https://x/y", "talk.mp3"),
        ('attachment; filename="dir/"', "https://x/y", "y"),
        (None, "https://x/a%2Fb.mp3", "b.mp3"),
        (None, "https://x/a/..", None),
        (None, "https://x/a/%2E%2E", None),
        (None, "https://x", None),
    ],
)
def test_filename_from_keeps_no_directory(header, url, expected):
    assert filename_from(header, url) == expected


def test_filename_from_drops_control_characters():
    assert filename_from("attachment; filename*=UTF-8''a%00b%0A.mp3", "https://x/y") == "ab.mp3"
    assert filename_from(None, "https://x/a%00b%0D%0A.mp3") == "ab.mp3"
    assert filename_from(None, "https://x/%00%01") is None


@pytest.mark.parametrize(
    "header",
    [
        "attachment; filename*=UTF-8''%ZZ",
        "attachment; filename*=bogus''x%41",
        "attachment; filename*",
        "attachment; filename*=",
        'attachment; filename*0="a"; filename*1="b.mp3"',
        "attachment; filename*=UTF-8''%FF%FE.mp3",
        "\x00\x01",
    ],
)
def test_filename_from_never_raises_on_odd_headers(header):
    name = filename_from(header, "https://x/u.mp3")
    assert name is None or isinstance(name, str)


async def test_filename_and_host_come_from_the_final_response(make_source, tmp_path):
    def handler(request):
        if request.url.host == "a.example":
            return httpx.Response(
                302,
                headers={
                    "Location": "https://b.example/files/real.mp3",
                    "Content-Disposition": 'attachment; filename="redirect.mp3"',
                },
            )
        return httpx.Response(200, content=b"abc")

    f = await make_source(handler).fetch("https://a.example/start", tmp_path / "dl")
    assert f.filename == "real.mp3" and f.host == "b.example"


async def test_filename_from_content_disposition(make_source, tmp_path):
    def handler(request):
        return httpx.Response(
            200, content=b"abc", headers={"Content-Disposition": 'attachment; filename="Ep 1.mp3"'}
        )

    f = await make_source(handler).fetch("https://x.example/download?id=7", tmp_path / "dl")
    assert f.filename == "Ep 1.mp3"


def redirect_chain(length: int):
    """Hop i redirects to /i+1 (a relative Location) until /length, which answers 200."""

    def handler(request):
        hop = int(request.url.path.strip("/") or 0)
        if hop < length:
            return httpx.Response(302, headers={"Location": f"/{hop + 1}"})
        return httpx.Response(200, content=b"abc")

    return handler


async def test_five_redirects_ok(make_source, tmp_path):
    f = await make_source(redirect_chain(5)).fetch("https://x.example/0", tmp_path / "dl")
    assert f.path.read_bytes() == b"abc"


async def test_six_redirects_fail(make_source, tmp_path):
    seen: list[str] = []

    def handler(request):
        seen.append(request.url.path)
        return redirect_chain(6)(request)

    with pytest.raises(EpisodeFailure) as info:
        await make_source(handler).fetch("https://x.example/0", tmp_path / "dl")
    assert str(info.value) == "too many redirects"
    assert seen == [f"/{n}" for n in range(6)]  # it never asked for the sixth redirect's target
    assert not (tmp_path / "dl").exists()


async def test_max_redirects_is_configurable(make_source, tmp_path):
    src = make_source(redirect_chain(1), max_redirects=0)
    with pytest.raises(EpisodeFailure, match="^too many redirects$"):
        await src.fetch("https://x.example/0", tmp_path / "dl")
    f = await make_source(redirect_chain(0), max_redirects=0).fetch(
        "https://x.example/0", tmp_path / "dl"
    )
    assert f.path.read_bytes() == b"abc"


async def test_redirect_loop_fails(make_source, tmp_path):
    def handler(request):
        return httpx.Response(301, headers={"Location": "https://x.example/same"})

    with pytest.raises(EpisodeFailure, match="^too many redirects$"):
        await make_source(handler).fetch("https://x.example/same", tmp_path / "dl")


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_every_redirect_status_is_followed(make_source, tmp_path, status):
    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(status, headers={"Location": "https://y.example/f.mp3"})
        return httpx.Response(200, content=b"abc")

    f = await make_source(handler).fetch("https://x.example/start", tmp_path / "dl")
    assert f.host == "y.example"


@pytest.mark.parametrize(
    "location",
    [
        "ftp://x.example/f.mp3",
        "file:///etc/passwd",
        "https://user:pw@x.example/f.mp3",
        "http://x.example:99999/f.mp3",
        "//user:pw@y.example/f.mp3",
    ],
)
async def test_redirect_to_a_link_the_guard_rejects(make_source, tmp_path, location):
    requests: list[httpx.Request] = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": location})

    with pytest.raises(EpisodeFailure) as info:
        await make_source(handler).fetch("https://x.example/start", tmp_path / "dl")
    assert str(info.value) == "blocked address"
    assert len(requests) == 1


async def test_a_location_httpx_cannot_parse_fails_cleanly(make_source, tmp_path):
    def handler(request):
        return httpx.Response(302, headers={"Location": "http://[::1"})

    # httpx parses the Location itself and reports a bad one as a protocol error.
    with pytest.raises(EpisodeFailure, match="^could not connect$"):
        await make_source(handler).fetch("https://x.example/start", tmp_path / "dl")


async def test_redirect_to_ftp(make_source, tmp_path):
    def handler(request):
        return httpx.Response(302, headers={"Location": "ftp://x.example/f.mp3"})

    with pytest.raises(EpisodeFailure) as info:
        await make_source(handler).fetch("https://x.example/start", tmp_path / "dl")
    assert str(info.value) == "blocked address"
    assert not (tmp_path / "dl").exists()


@pytest.mark.parametrize(
    "url",
    ["ftp://x.example/f.mp3", "https://u:p@x.example/f.mp3", "nope", "http://xn--a-ecp.ru/f.mp3"],
)
async def test_first_link_goes_through_the_guard_too(make_source, tmp_path, url):
    def handler(request):
        raise AssertionError("no request may be made")

    with pytest.raises(EpisodeFailure) as info:
        await make_source(handler).fetch(url, tmp_path / "dl")
    assert str(info.value) == "blocked address"


async def test_redirect_without_location_is_an_http_error(make_source, tmp_path):
    def handler(request):
        return httpx.Response(302)

    with pytest.raises(EpisodeFailure) as info:
        await make_source(handler).fetch("https://x.example/start", tmp_path / "dl")
    assert str(info.value) == "the server answered HTTP 302"


async def test_http_error(make_source, tmp_path):
    def handler(request):
        return httpx.Response(404, content=b"nope")

    with pytest.raises(EpisodeFailure) as info:
        await make_source(handler).fetch("https://x.example/f.mp3", tmp_path / "dl")
    assert str(info.value) == "the server answered HTTP 404"
    assert not (tmp_path / "dl").exists()


async def test_http_error_after_a_redirect(make_source, tmp_path):
    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/gone"})
        return httpx.Response(503)

    with pytest.raises(EpisodeFailure, match="^the server answered HTTP 503$"):
        await make_source(handler).fetch("https://x.example/start", tmp_path / "dl")


@pytest.mark.parametrize(
    "content_type", ["text/html", "text/html; charset=utf-8", "Text/HTML", " TEXT/HTML ;charset=x"]
)
async def test_web_page(make_source, tmp_path, content_type):
    def handler(request):
        return httpx.Response(200, content=b"<html>", headers={"Content-Type": content_type})

    with pytest.raises(EpisodeFailure) as info:
        await make_source(handler).fetch("https://x.example/page", tmp_path / "dl")
    assert str(info.value) == WEB_PAGE
    assert not (tmp_path / "dl").exists()


@pytest.mark.parametrize(
    "content_type", [None, "audio/mpeg", "video/mp4", "application/octet-stream", "text/plain"]
)
async def test_other_content_types_pass(make_source, tmp_path, content_type):
    headers = {"Content-Type": content_type} if content_type else {}

    def handler(request):
        return httpx.Response(200, content=b"abc", headers=headers)

    f = await make_source(handler).fetch("https://x.example/f", tmp_path / "dl")
    assert f.path.read_bytes() == b"abc"


async def test_content_length_too_large(make_source, tmp_path):
    read: list[bool] = []

    async def body():
        read.append(True)
        yield b"x"

    def handler(request):
        return httpx.Response(200, content=body(), headers={"Content-Length": str(MAX_BYTES + 1)})

    with pytest.raises(EpisodeFailure) as info:
        await make_source(handler).fetch("https://x.example/f.mp3", tmp_path / "dl")
    assert str(info.value) == "too large"
    assert read == []  # the body was never read
    assert not (tmp_path / "dl").exists()


async def test_exactly_max_bytes_is_fine(make_source, tmp_path):
    def handler(request):
        return httpx.Response(200, content=b"x" * MAX_BYTES)

    f = await make_source(handler).fetch("https://x.example/f.mp3", tmp_path / "dl")
    assert f.path.stat().st_size == MAX_BYTES


async def test_one_byte_over_without_a_content_length(make_source, tmp_path):
    async def body():
        yield b"x" * MAX_BYTES
        yield b"y"

    def handler(request):
        return httpx.Response(200, content=body())

    with pytest.raises(EpisodeFailure, match="^too large$"):
        await make_source(handler).fetch("https://x.example/f.mp3", tmp_path / "dl")
    assert not (tmp_path / "dl").exists()


async def test_stream_too_large(make_source, tmp_path):
    chunk = b"x" * 65536  # the first chunk is written; the second takes it past max_bytes

    def handler(request):
        return httpx.Response(200, content=endless(chunk))

    src = make_source(handler, max_bytes=100_000)
    with pytest.raises(EpisodeFailure) as info:
        await src.fetch("https://x.example/f.mp3", tmp_path / "dl")
    assert str(info.value) == "too large"
    assert not (tmp_path / "dl").exists()


@pytest.mark.parametrize("length", ["abc", "", "-5", "1e9"])
async def test_a_content_length_that_is_not_a_size_is_ignored(make_source, tmp_path, length):
    def handler(request):
        return httpx.Response(200, content=b"abc", headers={"Content-Length": length})

    f = await make_source(handler).fetch("https://x.example/f.mp3", tmp_path / "dl")
    assert f.path.read_bytes() == b"abc"


async def test_timeout(make_source, tmp_path):
    src = make_source(fails(httpx.ReadTimeout("slow")))
    with pytest.raises(EpisodeFailure) as info:
        await src.fetch("https://x.example/f.mp3", tmp_path / "dl")
    assert str(info.value) == "timed out"


@pytest.mark.parametrize(
    "exc", [httpx.ConnectTimeout("c"), httpx.WriteTimeout("w"), httpx.PoolTimeout("p")]
)
async def test_every_timeout_is_timed_out(make_source, tmp_path, exc):
    with pytest.raises(EpisodeFailure, match="^timed out$"):
        await make_source(fails(exc)).fetch("https://x.example/f.mp3", tmp_path / "dl")


async def test_connect_error(make_source, tmp_path):
    src = make_source(fails(httpx.ConnectError("refused")))
    with pytest.raises(EpisodeFailure) as info:
        await src.fetch("https://x.example/f.mp3", tmp_path / "dl")
    assert str(info.value) == "could not connect"


@pytest.mark.parametrize(
    "exc", [httpx.RemoteProtocolError("bad"), httpx.ReadError("reset"), httpx.WriteError("w")]
)
async def test_other_transport_errors_are_could_not_connect(make_source, tmp_path, exc):
    with pytest.raises(EpisodeFailure, match="^could not connect$"):
        await make_source(fails(exc)).fetch("https://x.example/f.mp3", tmp_path / "dl")


async def trickle():
    """A body that gives a first chunk at once (more than a read's worth, so that it is on disk)
    and then a byte every 50 ms for as long as anyone reads: no read ever times out."""
    yield b"x" * 70_000
    while True:
        await asyncio.sleep(0.05)
        yield b"y"


async def slow_response(request: httpx.Request) -> httpx.Response:
    await asyncio.sleep(30)  # the server takes its time to answer at all
    return httpx.Response(200, content=b"abc")


@pytest.mark.parametrize("slowness", ["body", "response"])
async def test_a_download_has_a_total_time_limit(make_source, tmp_path, slowness):
    """The per-read timeouts never fire on a server that keeps sending something, so the whole
    download is limited: it ends as `timed out`, nothing is left behind and nothing of the
    link is in the failure."""
    link = "https://x.example/" + "private-path" + "?token=" + "hunter2"
    handler = (
        slow_response
        if slowness == "response"
        else lambda r: httpx.Response(200, content=trickle())
    )
    dest = tmp_path / "dl"
    source = make_source(handler, max_bytes=10**9, max_seconds=0.2)
    with pytest.raises(EpisodeFailure) as info:
        await asyncio.wait_for(source.fetch(link, dest), 5)  # a missing limit fails, not hangs
    assert str(info.value) == "timed out"
    assert not dest.exists() and list(tmp_path.iterdir()) == []
    assert info.value.__cause__ is None and info.value.__suppress_context__
    for leak in ("private-path", "hunter2"):
        assert leak not in "".join(traceback.format_exception(info.value)), leak


async def test_the_time_limit_covers_every_redirect_hop(make_source, tmp_path):
    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/slow"})
        return slow_response(request)

    with pytest.raises(EpisodeFailure, match="^timed out$"):
        await make_source(handler, max_seconds=0.2).fetch(
            "https://x.example/start", tmp_path / "dl"
        )


async def test_a_download_within_the_time_limit_is_untouched(make_source, tmp_path):
    f = await make_source(ok, max_seconds=5).fetch("https://x.example/f.mp3", tmp_path / "dl")
    assert f.path.read_bytes() == b"abc"


def test_the_default_time_limit_is_two_hours():
    assert inspect.signature(HttpFileSource).parameters["max_seconds"].default == 7200


@pytest.mark.parametrize("reason", ["blocked address", "could not connect", "timed out"])
async def test_the_guards_failure_passes_through_unchanged(make_source, tmp_path, reason):
    failure = EpisodeFailure(reason)
    with pytest.raises(EpisodeFailure) as info:
        await make_source(fails(failure)).fetch("https://x.example/f.mp3", tmp_path / "dl")
    assert info.value is failure


@pytest.mark.parametrize(
    "location",
    [
        "http://xn--a-ecp.ru/f.mp3",  # decodes to a⒈, a codepoint idna does not allow
        "http://xn--/f.mp3",  # a malformed A-label
        "//xn--9999999999.example/f.mp3",  # an invalid one, and no scheme
    ],
)
async def test_redirect_to_a_host_idna_cannot_decode_is_blocked(
    make_source, tmp_path, caplog, location
):
    """httpx decodes the host of a Location itself, before the guard sees the link, and idna's
    error quotes the label: it must end as `blocked address` and leave no trace of the label."""
    caplog.set_level(logging.DEBUG)
    requests: list[httpx.Request] = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": location})

    dest = tmp_path / "dl"
    dest.write_bytes(b"left over")
    with pytest.raises(EpisodeFailure) as info:
        await make_source(handler).fetch("https://x.example/start", dest)
    assert str(info.value) == "blocked address"
    assert len(requests) == 1  # nothing was requested from the bad host
    assert not dest.exists()
    assert info.value.__cause__ is None and info.value.__suppress_context__
    for leak in ("a\u2488", "xn--a-ecp", "xn--9999999999"):
        assert leak not in caplog.text, leak
        assert all(leak not in r.getMessage() for r in caplog.records), leak
        assert leak not in "".join(traceback.format_exception(info.value)), leak


async def test_the_guards_failure_on_a_redirect_hop(make_source, tmp_path):
    def handler(request):
        if request.url.host == "x.example":
            return httpx.Response(302, headers={"Location": "https://internal.example/f.mp3"})
        raise EpisodeFailure("blocked address")

    with pytest.raises(EpisodeFailure, match="^blocked address$"):
        await make_source(handler).fetch("https://x.example/start", tmp_path / "dl")


@pytest.mark.parametrize(
    "exc,reason",
    [
        (httpx.ReadTimeout("slow"), "timed out"),
        (httpx.RemoteProtocolError("cut off"), "could not connect"),
    ],
)
async def test_failure_part_way_through_the_body_deletes_dest(make_source, tmp_path, exc, reason):
    async def body():
        yield b"x" * 70_000  # past the 64 KiB chunk size, so the first chunk is on disk
        yield b"y"
        raise exc

    def handler(request):
        return httpx.Response(200, content=body())

    src = make_source(handler, max_bytes=1_000_000)
    with pytest.raises(EpisodeFailure) as info:
        await src.fetch("https://x.example/f.mp3", tmp_path / "dl")
    assert str(info.value) == reason
    assert not (tmp_path / "dl").exists()


async def test_a_leftover_dest_is_removed_when_the_fetch_fails(make_source, tmp_path):
    dest = tmp_path / "dl"
    dest.write_bytes(b"stale")

    def handler(request):
        return httpx.Response(404)

    with pytest.raises(EpisodeFailure):
        await make_source(handler).fetch("https://x.example/f.mp3", dest)
    assert not dest.exists()


async def test_a_leftover_dest_is_replaced_when_the_fetch_works(make_source, tmp_path):
    dest = tmp_path / "dl"
    dest.write_bytes(b"stale and longer than the new file")
    f = await make_source(ok).fetch("https://x.example/f.mp3", dest)
    assert f.path.read_bytes() == b"abc"


async def test_dest_is_not_written_until_the_response_is_accepted(make_source, tmp_path):
    dest = tmp_path / "dl"
    seen: list[bool] = []

    def handler(request):
        seen.append(dest.exists())  # a request goes out before any check can have passed
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/f.mp3"})
        return httpx.Response(200, content=b"abc")

    await make_source(handler).fetch("https://x.example/start", dest)
    assert seen == [False, False]
    assert dest.read_bytes() == b"abc"


async def test_cancellation_deletes_dest(make_source, tmp_path):
    dest = tmp_path / "dl"
    in_body = asyncio.Event()

    async def body():
        yield b"abc"
        in_body.set()  # the first chunk has been written by the time the consumer asks again
        await asyncio.Event().wait()
        yield b"never"

    def handler(request):
        return httpx.Response(200, content=body())

    task = asyncio.create_task(make_source(handler).fetch("https://x.example/f.mp3", dest))
    await asyncio.wait_for(in_body.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not dest.exists()


async def test_nothing_is_left_in_the_directory_on_failure(make_source, tmp_path):
    def handler(request):
        return httpx.Response(200, content=b"<html>", headers={"Content-Type": "text/html"})

    with pytest.raises(EpisodeFailure):
        await make_source(handler).fetch("https://x.example/page", tmp_path / "dl")
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------ the guard and the source together


@dataclass
class RedirectServer:
    """A server on 127.0.0.1 and the request lines it was sent, in order."""

    port: int = 0
    requests: list[str] = field(default_factory=list)


@pytest.fixture
async def redirect_server():
    """Answers `/start` with a 302 to http://internal.test:<its own port>/secret and any other
    path with a little audio, and hangs up after each answer."""
    seen = RedirectServer()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            request_line = head.split("\r\n")[0]
            seen.requests.append(request_line)
            if request_line.split()[1] == "/start":
                answer = (
                    f"HTTP/1.1 302 Found\r\nLocation: http://internal.test:{seen.port}/secret\r\n"
                    "content-length: 0\r\nconnection: close\r\n\r\n"
                )
            else:
                answer = (
                    "HTTP/1.1 200 OK\r\ncontent-type: audio/mpeg\r\ncontent-length: 3\r\n"
                    "connection: close\r\n\r\nabc"
                )
            writer.write(answer.encode())
            await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    seen.port = server.sockets[0].getsockname()[1]
    async with server:
        yield seen


def guard_with_a_private_name():
    """The real guard. files.test is on 127.0.0.1, which stands in for a public address (the
    server must be reachable); internal.test is on 127.0.0.2, which is refused."""
    answers = {"files.test": ["127.0.0.1"], "internal.test": ["127.0.0.2"]}

    async def resolve(host, port):
        return answers[host]

    return guarded_client(resolve=resolve, is_allowed=lambda ip: ip == "127.0.0.1")


async def test_a_link_to_an_allowed_address_downloads(redirect_server, tmp_path):
    async with guard_with_a_private_name() as client:
        link = f"http://files.test:{redirect_server.port}/file"
        fetched = await HttpFileSource(client, MAX_BYTES).fetch(link, tmp_path / "dl")
    assert fetched.path.read_bytes() == b"abc"
    assert redirect_server.requests == ["GET /file HTTP/1.1"]


async def test_a_redirect_to_a_private_address_is_blocked_before_any_request(
    redirect_server, tmp_path
):
    """The real guarded client and the real source together: the guard's mock-transport tests
    cannot show that a hop that resolves to a refused address is never connected to."""
    link = f"http://files.test:{redirect_server.port}/start"
    dest = tmp_path / "dl"
    async with guard_with_a_private_name() as client:
        with pytest.raises(EpisodeFailure) as info:
            await HttpFileSource(client, MAX_BYTES).fetch(link, dest)
    assert str(info.value) == "blocked address"
    assert redirect_server.requests == ["GET /start HTTP/1.1"]  # /secret was never asked for
    assert not dest.exists()


# ------------------------------------------------------------------------------------ uploads


def test_an_upload_ref_holds_the_episode_id_and_the_clean_file_name():
    ref = upload_ref("abcdefghijkl", "C:\\Users\\me\\Talk\x07 One.mp3")
    assert ref == "abcdefghijkl/Talk One.mp3"
    assert upload_id(ref) == "abcdefghijkl" and upload_filename(ref) == "Talk One.mp3"


@pytest.mark.parametrize("name", [None, "", "  ", "..", "a/"])
def test_an_upload_without_a_usable_name_has_the_id_alone_as_ref(name):
    ref = upload_ref("abcdefghijkl", name)
    assert ref == "abcdefghijkl"
    assert upload_id(ref) == "abcdefghijkl" and upload_filename(ref) is None


def test_a_very_long_upload_name_is_cut():
    ref = upload_ref("abcdefghijkl", "x" * 1000 + ".mp3")
    assert len(upload_filename(ref)) == 200


@pytest.fixture
def uploads(tmp_path):
    storage = Storage(tmp_path / "media")
    storage.ensure_layout()
    return storage


async def test_the_upload_source_moves_the_ready_file_to_dest(uploads, tmp_path):
    uploads.upload_ready("abcdefghijkl").write_bytes(b"audio")
    dest = tmp_path / "dl"
    fetched = await UploadSource(uploads).fetch("abcdefghijkl/Talk.mp3", dest)
    assert fetched == Fetched(dest, "Talk.mp3", "upload")
    assert dest.read_bytes() == b"audio" and not uploads.upload_ready("abcdefghijkl").exists()


async def test_the_upload_source_without_a_name(uploads, tmp_path):
    uploads.upload_ready("abcdefghijkl").write_bytes(b"audio")
    fetched = await UploadSource(uploads).fetch("abcdefghijkl", tmp_path / "dl")
    assert fetched.filename is None


async def test_a_lost_upload_fails_with_a_reason_and_leaves_no_dest(uploads, tmp_path):
    dest = tmp_path / "dl"
    with pytest.raises(EpisodeFailure) as info:
        await UploadSource(uploads).fetch("abcdefghijkl/Talk.mp3", dest)
    assert str(info.value) == "upload lost" and not dest.exists()


async def test_the_part_of_an_unfinished_upload_is_never_taken(uploads, tmp_path):
    uploads.upload_part("abcdefghijkl").write_bytes(b"half")
    with pytest.raises(EpisodeFailure):
        await UploadSource(uploads).fetch("abcdefghijkl", tmp_path / "dl")
    assert uploads.upload_part("abcdefghijkl").read_bytes() == b"half"


def test_the_upload_source_kind():
    assert UploadSource.kind == "upload"
