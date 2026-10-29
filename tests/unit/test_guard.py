import asyncio
import socket

import httpx
import pytest

from podcast_feed.errors import EpisodeFailure, Invalid
from podcast_feed.guard import (
    DEFAULT_TIMEOUT,
    GuardedBackend,
    check_url,
    guarded_client,
    is_public_ip,
    system_resolve,
)


def test_check_url_normalises():
    assert check_url("  HTTPS://Example.com/a b.mp3 ") == "https://example.com/a%20b.mp3"


@pytest.mark.parametrize(
    "url,msg",
    [
        ("ftp://x.example/y", "Only http and https"),
        ("file:///etc/passwd", "Only http and https"),
        ("https://user:pw@x.example/y", "username or password"),
        ("https:///nohost", "Only http and https"),
        ("not a url", "Only http and https"),
        ("javascript:alert(1)", "Only http and https"),
        ("//x.example/y", "Only http and https"),
        ("http:/x.example/y", "Only http and https"),
        ("http://x.example:abc/y", "Only http and https"),
        # httpx parses these ports; connecting to one would fail with an ExceptionGroup.
        ("http://93.184.216.34:99999/y", "Only http and https"),
        ("http://x.example:-1/y", "Only http and https"),
        ("http://x.example:0/y", "Only http and https"),
        ("https://user@x.example/y", "username or password"),
    ],
)
def test_check_url_rejects(url, msg):
    with pytest.raises(Invalid, match=msg):
        check_url(url)


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "::",
        # As getaddrinfo can return a link-local address: with its scope.
        "fe80::1%eth0",
        "::ffff:10.0.0.1",
        # IPv4-compatible and NAT64 forms of 127.0.0.1: global, but reserved.
        "::7f00:1",
        "64:ff9b::7f00:1",
        "255.255.255.255",
        "240.0.0.1",
    ],
)
def test_not_public(ip):
    assert not is_public_ip(ip)


@pytest.mark.parametrize("ip", ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"])
def test_public(ip):
    assert is_public_ip(ip)


@pytest.mark.parametrize("text", ["", "example.com", "[::1]", "127.0.0.1:80"])
def test_not_an_address_is_not_public(text):
    assert not is_public_ip(text)


def resolver(answers):
    async def resolve(host, port):
        return answers[host]

    return resolve


async def test_blocked_after_resolve():
    async with guarded_client(resolve=resolver({"evil.test": ["127.0.0.1"]})) as c:
        with pytest.raises(EpisodeFailure, match="blocked address"):
            await c.get("http://evil.test/x")


async def test_any_private_answer_blocks():
    r = resolver({"mixed.test": ["93.184.216.34", "10.0.0.1"]})
    async with guarded_client(resolve=r) as c:
        with pytest.raises(EpisodeFailure, match="blocked address"):
            await c.get("http://mixed.test/x")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9/x",
        "http://[::1]:9/x",
        "http://[::ffff:127.0.0.1]:9/x",
        # Not literals to ipaddress: the system resolver reads them as 127.0.0.1.
        "http://127.1:9/x",
        "http://2130706433:9/x",
    ],
)
async def test_ip_literal_blocked(url):
    async with guarded_client() as c:
        with pytest.raises(EpisodeFailure, match="blocked address"):
            await c.get(url)


async def test_connects_to_the_resolved_address(local_http_server):
    r = resolver({"files.test": ["127.0.0.1"]})
    async with guarded_client(resolve=r, is_allowed=lambda ip: True) as c:
        resp = await c.get(f"http://files.test:{local_http_server.port}/x")
    assert (
        resp.status_code == 200
        and local_http_server.last_host == f"files.test:{local_http_server.port}"
    )


async def test_every_connection_is_checked(local_http_server):
    """A name that answers differently the second time (DNS rebinding) is caught then."""
    answers = iter([["127.0.0.1"], ["10.0.0.1"]])

    async def rebinding(host, port):
        return next(answers)

    url = f"http://files.test:{local_http_server.port}/x"
    # 127.0.0.1 stands in for a public address here; the server closes each connection.
    async with guarded_client(resolve=rebinding, is_allowed=lambda ip: ip == "127.0.0.1") as c:
        assert (await c.get(url)).status_code == 200
        with pytest.raises(EpisodeFailure, match="blocked address"):
            await c.get(url)


async def test_tls_uses_the_original_host_name(local_http_server):
    r = resolver({"files.test": ["127.0.0.1"]})
    async with guarded_client(resolve=r, is_allowed=lambda ip: True) as c:
        # The test server hangs up after the ClientHello.
        with pytest.raises(httpx.ConnectError):
            await c.get(f"https://files.test:{local_http_server.port}/x")
    # Python sends no SNI for an IP address, so this is the name, not 127.0.0.1.
    assert b"files.test" in local_http_server.last_tls_hello


async def test_no_addresses():
    async with guarded_client(resolve=resolver({"empty.test": []})) as c:
        with pytest.raises(EpisodeFailure, match="could not connect"):
            await c.get("http://empty.test/x")


@pytest.mark.parametrize(
    "error",
    [
        socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known"),
        OSError("Network is unreachable"),
        # What getaddrinfo raises for a label longer than 63 characters.
        UnicodeError("label too long"),
    ],
)
async def test_lookup_failure(error):
    async def failing(host, port):
        raise error

    async with guarded_client(resolve=failing) as c:
        with pytest.raises(EpisodeFailure, match="could not connect"):
            await c.get("http://nowhere.test/x")


async def test_slow_lookup_times_out():
    async def slow(host, port):
        await asyncio.sleep(5)
        return ["93.184.216.34"]

    async with guarded_client(resolve=slow, timeout=httpx.Timeout(1, connect=0.05)) as c:
        with pytest.raises(EpisodeFailure, match="timed out"):
            await c.get("http://slow.test/x")


async def test_no_unix_sockets():
    backend = GuardedBackend(resolver({}), lambda ip: True)
    with pytest.raises(EpisodeFailure, match="blocked address"):
        await backend.connect_unix_socket("/run/podman/podman.sock")


async def test_guarded_client_settings():
    async with guarded_client() as c:
        assert c.follow_redirects is False and c.trust_env is False
        assert c.timeout == DEFAULT_TIMEOUT == httpx.Timeout(connect=10, read=60, write=60, pool=10)


async def test_system_resolve_ip_literal():
    assert await system_resolve("127.0.0.1", 80) == ["127.0.0.1"]


async def test_system_resolve_gives_unique_addresses_in_order(monkeypatch):
    calls = []

    async def getaddrinfo(host, port, *, family=0, type=0, proto=0, flags=0):
        calls.append((host, port, type))
        v6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:2800::1", port, 0, 0))
        v4 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
        return [v6, v4, v6]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)
    assert await system_resolve("files.test", 443) == ["2606:2800::1", "93.184.216.34"]
    assert calls == [("files.test", 443, socket.SOCK_STREAM)]
