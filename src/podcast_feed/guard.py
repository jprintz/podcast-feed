"""The URL guard: keeps downloads away from addresses only the VPS can reach.

Links come from the admin UI and from Claude, and a hostile web page could talk Claude
into adding `http://kavita:5000/...`. So the guard does two things:

- `check_url` accepts http and https links with a host and no credentials (syntax only);
- `guarded_client` gives an httpx client whose network backend looks the host name up
  itself, refuses the connection unless every address is public, and then connects to
  one of the addresses it checked. The check therefore runs on every connection,
  redirects included, and a name that answers differently the next time (DNS rebinding)
  is checked again then. TLS still sends and verifies the link's host name, because
  httpcore hands the request's host, not the address, to `start_tls`.

httpx has no public hook for the network backend, so the client's transport replaces the
connection pool httpx built; a test that connects through it catches a change in httpx.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable

import httpcore
import httpx

from podcast_feed.errors import EpisodeFailure, Invalid

Resolver = Callable[[str, int], Awaitable[list[str]]]

DEFAULT_TIMEOUT = httpx.Timeout(connect=10, read=60, write=60, pool=10)

# httpx's own defaults, for the pool that replaces httpx's.
_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=5)


async def system_resolve(host: str, port: int) -> list[str]:
    """The addresses the system resolver gives for host, each once, in its order."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


def is_public_ip(ip: str) -> bool:
    """Whether ip is an address on the public internet; anything that does not parse is not."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and not (
        address.is_multicast or address.is_unspecified or address.is_reserved
    )


def _parts(url: str) -> tuple[str, str, int | None, bytes, str] | None:
    """The scheme, host, port, userinfo and normalised form of the link; None if it does not parse.

    Everything is read here because httpx decodes the host on access, and idna can fail then
    as well as when the URL is built. Its errors are UnicodeErrors whose messages quote the host
    label, so they are swallowed here and `check_url` raises its own, outside this handler,
    which leaves no trace of them: the link must not reach a log through an exception.
    """
    try:
        parsed = httpx.URL(url.strip())
        return parsed.scheme, parsed.host, parsed.port, parsed.userinfo, str(parsed)
    except (httpx.InvalidURL, UnicodeError):
        return None


def check_url(url: str) -> str:
    """The link, normalised, if it is an http or https link with a host and no credentials."""
    parts = _parts(url)
    if parts is None:
        raise Invalid("Only http and https links to a file work.")
    scheme, host, port, userinfo, link = parts
    if scheme not in ("http", "https") or not host or (port is not None and not 1 <= port <= 65535):
        raise Invalid("Only http and https links to a file work.")
    if userinfo:
        raise Invalid("Links with a username or password are not accepted.")
    return link


class GuardedBackend(httpcore.AsyncNetworkBackend):
    """A network backend that connects only to addresses is_allowed accepts.

    Every address the name resolves to must be allowed, not just the one connected to.
    The connect timeout bounds the lookup and the connection each.
    """

    def __init__(self, resolve: Resolver, is_allowed: Callable[[str], bool]) -> None:
        self._resolve = resolve
        self._is_allowed = is_allowed
        self._network = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await self._addresses(host, port, timeout)
        if not addresses:
            raise EpisodeFailure("could not connect")
        if not all(self._is_allowed(address) for address in addresses):
            raise EpisodeFailure("blocked address")
        return await self._network.connect_tcp(
            addresses[0],
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def _addresses(self, host: str, port: int, timeout: float | None) -> list[str]:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            return [host]
        try:
            async with asyncio.timeout(timeout):
                return await self._resolve(host, port)
        # TimeoutError is an OSError, so it goes first. getaddrinfo raises UnicodeError
        # for a name it cannot encode (a label over 63 characters).
        except TimeoutError as exc:
            raise EpisodeFailure("timed out") from exc
        except (OSError, UnicodeError) as exc:
            raise EpisodeFailure("could not connect") from exc

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise EpisodeFailure("blocked address")

    async def sleep(self, seconds: float) -> None:
        await self._network.sleep(seconds)


class _GuardedTransport(httpx.AsyncHTTPTransport):
    """httpx's transport with a connection pool that connects through the guard."""

    def __init__(self, backend: GuardedBackend) -> None:
        ssl_context = httpx.create_ssl_context()
        super().__init__(verify=ssl_context, limits=_LIMITS)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=_LIMITS.max_connections,
            max_keepalive_connections=_LIMITS.max_keepalive_connections,
            keepalive_expiry=_LIMITS.keepalive_expiry,
            network_backend=backend,
        )


def guarded_client(
    *,
    resolve: Resolver = system_resolve,
    is_allowed: Callable[[str], bool] = is_public_ip,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
) -> httpx.AsyncClient:
    """An httpx client that refuses every connection to an address is_allowed rejects.

    It follows no redirects (the caller checks each hop) and ignores proxy and .netrc
    settings from the environment.
    """
    return httpx.AsyncClient(
        transport=_GuardedTransport(GuardedBackend(resolve, is_allowed)),
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    )
