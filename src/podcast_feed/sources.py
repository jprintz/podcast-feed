"""Where episode files come from. A source puts a file at a path on disk and says what it got.

`HttpFileSource` downloads a link: it follows redirects itself, so that the guard checks
every hop, and it refuses web pages and files over the size limit before they fill the disk.
The whole download has a time limit too: httpx's timeouts are per read, and a server that
sends a byte now and then would hold the (single) worker for ever.

`UploadSource` takes a file the admin UI received: it is in media/.tmp/ already, so the
"download" is a move. Its source reference is the episode's id and the file's name, see
`upload_ref`.
"""

import asyncio
import os
import unicodedata
import urllib.parse
from dataclasses import dataclass
from email.message import Message
from email.utils import collapse_rfc2231_value
from pathlib import Path
from typing import Protocol

import httpx

from podcast_feed import __version__
from podcast_feed.errors import EpisodeFailure, Invalid
from podcast_feed.guard import check_url
from podcast_feed.storage import Storage

CHUNK_SIZE = 64 * 1024
UPLOAD_KIND = "upload"
# Enough for any file name, and the name is shown in the admin UI and kept in the database.
MAX_UPLOAD_NAME = 200


@dataclass(frozen=True)
class Fetched:
    """A downloaded file: where it is, the name the server gave it (if any) and its host."""

    path: Path
    filename: str | None
    host: str


class Source(Protocol):
    kind: str

    async def fetch(self, ref: str, dest: Path) -> Fetched:
        """Download ref to dest. Raises EpisodeFailure (nothing left at dest) if it cannot."""
        ...


def _basename(name: str) -> str | None:
    """The last path component of name without control characters, or None if nothing is left."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(ch for ch in base if unicodedata.category(ch) != "Cc").strip()
    return None if base in ("", ".", "..") else base


def _disposition_filename(content_disposition: str) -> str:
    """The filename in a Content-Disposition header; `filename*` wins over `filename` (RFC 6266)."""
    message = Message()
    message["Content-Disposition"] = content_disposition
    values = [v for k, v in message.get_params(header="content-disposition") if k == "filename"]
    # email decodes an extended value (RFC 5987) into a (charset, language, text) tuple.
    values.sort(key=lambda v: not isinstance(v, tuple))
    return collapse_rfc2231_value(values[0]).strip() if values else ""


def filename_from(content_disposition: str | None, url: str) -> str | None:
    """The file name the server gave: the Content-Disposition one, else the URL's last segment.

    Only the last path component is returned, so a name cannot point outside a directory.
    """
    if content_disposition:
        name = _basename(_disposition_filename(content_disposition))
        if name:
            return name
    segment = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
    return _basename(urllib.parse.unquote(segment))


def _is_web_page(content_type: str | None) -> bool:
    return (content_type or "").split(";", 1)[0].strip().lower() == "text/html"


def _declared_length(response: httpx.Response) -> int | None:
    try:
        return int(response.headers["content-length"])
    except (KeyError, ValueError):
        return None


class HttpFileSource:
    """Downloads http and https links. Each hop of a redirect chain goes through check_url."""

    kind = "url"

    def __init__(
        self,
        client: httpx.AsyncClient,
        max_bytes: int,
        max_redirects: int = 5,
        max_seconds: float = 7200,
    ) -> None:
        # The client must not follow redirects itself (guard.guarded_client does not).
        self._client = client
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._max_seconds = max_seconds

    async def fetch(self, ref: str, dest: Path) -> Fetched:
        try:
            try:
                # From the first request to the last byte: redirects and the body included.
                async with asyncio.timeout(self._max_seconds):
                    return await self._download(ref, dest)
            except TimeoutError:  # httpx's own timeouts are EpisodeFailures by now
                raise EpisodeFailure("timed out") from None
        except BaseException:  # failures and cancellation alike leave no half file behind
            dest.unlink(missing_ok=True)
            raise

    async def _download(self, ref: str, dest: Path) -> Fetched:
        try:
            return await self._follow(ref, dest)
        except httpx.TimeoutException as exc:
            raise EpisodeFailure("timed out") from exc
        except httpx.TransportError as exc:
            raise EpisodeFailure("could not connect") from exc
        except UnicodeError:
            # httpx decodes the host of a redirect's Location itself, before _checked sees the
            # link, and idna's errors (UnicodeErrors) quote the label. A host that cannot be
            # decoded is a link the guard would refuse; the error is not kept, so that nothing
            # of the label reaches a log through it. (EpisodeFailure is not a UnicodeError:
            # the guard's own failures pass through.)
            raise EpisodeFailure("blocked address") from None

    async def _follow(self, ref: str, dest: Path) -> Fetched:
        url = _checked(ref)
        for _ in range(self._max_redirects + 1):
            async with self._client.stream(
                "GET", url, headers={"User-Agent": f"podcast-feed/{__version__}"}
            ) as response:
                location = response.headers.get("location")
                if not (300 <= response.status_code < 400 and location):
                    return await self._save(response, url, dest)
            # httpx has parsed the Location already (a bad one raised before this point).
            url = _checked(str(httpx.URL(url).join(location)))
        raise EpisodeFailure("too many redirects")

    async def _save(self, response: httpx.Response, url: str, dest: Path) -> Fetched:
        if not 200 <= response.status_code < 300:
            raise EpisodeFailure(f"the server answered HTTP {response.status_code}")
        if _is_web_page(response.headers.get("content-type")):
            raise EpisodeFailure("that link is a web page, not an audio or video file")
        declared = _declared_length(response)
        if declared is not None and declared > self._max_bytes:
            raise EpisodeFailure("too large")
        written = 0
        with dest.open("wb") as out:
            async for chunk in response.aiter_bytes(CHUNK_SIZE):
                written += len(chunk)
                if written > self._max_bytes:
                    raise EpisodeFailure("too large")
                out.write(chunk)
        filename = filename_from(response.headers.get("content-disposition"), url)
        return Fetched(dest, filename, httpx.URL(url).host)


def _checked(url: str) -> str:
    """check_url, with a refused link reported as the failure reason it is."""
    try:
        return check_url(url)
    except Invalid as exc:
        raise EpisodeFailure("blocked address") from exc


def upload_ref(episode_id: str, filename: str | None) -> str:
    """The source reference of an upload: `<episode id>/<file name>`, or the id alone when the
    browser gave no usable name. The name is cleaned like a downloaded one (last path component,
    no control characters) and cut. The id makes every upload its own episode."""
    name = _basename(filename or "")
    return f"{episode_id}/{name[:MAX_UPLOAD_NAME]}" if name else episode_id


def upload_id(ref: str) -> str:
    return ref.partition("/")[0]


def upload_filename(ref: str) -> str | None:
    return ref.partition("/")[2] or None


class UploadSource:
    """Hands the worker an upload: moves media/.tmp/<id>.upload to where the job wants it."""

    kind = UPLOAD_KIND

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    async def fetch(self, ref: str, dest: Path) -> Fetched:
        try:
            os.replace(self._storage.upload_ready(upload_id(ref)), dest)
        except FileNotFoundError:
            # A restart empties media/.tmp/ before the worker starts, and a queued upload with it.
            raise EpisodeFailure("upload lost") from None
        return Fetched(dest, upload_filename(ref), "upload")
