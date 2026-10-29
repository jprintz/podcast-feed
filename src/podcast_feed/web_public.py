"""The public routes: a feed's RSS and the files it points at, and nothing else.

They are reachable from the internet through the secret-in-URL gateway, so there are exactly two
of them, both read-only (`GET` and `HEAD`; Starlette answers any other method with 405 and the
admin pages live elsewhere):

    /feeds/<slug>.xml          the feed
    /media/<slug>/<name>       an audio file, an episode's artwork or the feed's cover

What the media route serves is decided by `Service.media_file`, from the database, never from
what happens to be in the folder. The handlers are plain `def`s: Starlette runs them in its
thread pool, where the service's SQLite and file checks cannot block the event loop.
"""

import hashlib
import re
from email.utils import format_datetime

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from podcast_feed.errors import Invalid, NotFound
from podcast_feed.service import Service

FEED_TYPE = "application/rss+xml; charset=utf-8"
# Episodes and covers are never edited in place, and a replaced cover gets a new ?v= in the feed.
MEDIA_CACHE = "private, max-age=86400"


def _etag_matches(header: str, etag: str) -> bool:
    """Whether an If-None-Match value (`*`, or a comma-separated list of entity tags, weak or
    not) names `etag`. A weak comparison, as RFC 9110 prescribes for If-None-Match."""
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate == "*" or candidate.removeprefix("W/") == etag:
            return True
    return False


# One byte range of a Range header: first-last, first- or -suffix. The digits are capped so that
# int() below stays cheap; no file is anywhere near that long.
_RANGE_SPEC = re.compile(r"([0-9]{1,18})-([0-9]{1,18})?|-[0-9]{1,18}")


def _is_valid_range(value: str) -> bool:
    """Whether `value` is a well-formed `Range: bytes=...` header (RFC 9110, section 14.1)."""
    unit, _, specs = value.partition("=")
    if unit.strip().lower() != "bytes":
        return False
    for spec in specs.split(","):
        found = _RANGE_SPEC.fullmatch(spec.strip())
        if found is None or (found[2] is not None and int(found[1]) > int(found[2])):
            return False
    return True


class _MediaFile(FileResponse):
    """A FileResponse that ignores a Range header that is not well-formed.

    That is what RFC 9110 (section 14.2) tells a server to do, and the answer then is the whole
    file, 200. Starlette would answer 400, and a podcast app's odd request should still play.
    Well-formed ranges are Starlette's: 206 (one range or several), 416 (past the end).
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        value = Headers(scope=scope).get("range")
        if value is not None and not _is_valid_range(value):
            headers = [(name, raw) for name, raw in scope["headers"] if name != b"range"]
            scope = {**scope, "headers": headers}
        await super().__call__(scope, receive, send)


def public_routes(service: Service) -> list[Route]:
    def feed(request: Request) -> Response:
        try:
            body, changed_at = service.feed_document(request.path_params["slug"])
        except (NotFound, Invalid):
            return PlainTextResponse("no such feed\n", status_code=404)
        # The hash of the bytes, not of the change time: that has whole-second resolution, and
        # a feed can change twice in a second.
        etag = f'"{hashlib.sha1(body, usedforsecurity=False).hexdigest()}"'
        headers = {"ETag": etag, "Last-Modified": format_datetime(changed_at, usegmt=True)}
        if _etag_matches(",".join(request.headers.getlist("if-none-match")), etag):
            return Response(status_code=304, headers=headers)
        # For HEAD the ASGI server drops the body and keeps the headers, Content-Length included.
        return Response(body, headers=headers, media_type=FEED_TYPE)

    def media(request: Request) -> Response:
        try:
            path, mime = service.media_file(
                request.path_params["slug"], request.path_params["name"]
            )
            stat = path.stat()  # also here, where blocking is fine; FileResponse reuses it
        except (NotFound, Invalid, OSError):  # OSError: the file went since media_file looked
            return PlainTextResponse("not found\n", status_code=404)
        return _MediaFile(
            path, media_type=mime, headers={"Cache-Control": MEDIA_CACHE}, stat_result=stat
        )

    return [
        Route("/feeds/{slug}.xml", feed, methods=["GET", "HEAD"]),
        Route("/media/{slug}/{name}", media, methods=["GET", "HEAD"]),
    ]
