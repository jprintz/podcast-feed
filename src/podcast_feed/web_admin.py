"""The admin UI, under /ui/: where feeds are made, the secret feed address is shown, and episodes
are added, edited, retried and removed.

It is plain HTML forms and server-rendered pages; htmx only refreshes the episode table while an
episode is still being fetched. Cloudflare Access decides who gets in, and `SameOriginMiddleware`
makes sure a page on another site cannot make the browser of someone who is signed in post to it.

Starlette runs a sync endpoint in its thread pool, but these handlers read forms, which is async,
so they are `async def`s and every service call goes through `asyncio.to_thread`: the service
blocks (SQLite, files, locks) and must never run on the event loop. Every HTML response and
every redirect says `Cache-Control: no-store`, because the feed address is on the pages, and
forbids framing (`X-Frame-Options`, `frame-ancestors`), so that no other site can lay an invisible
copy of a page over its own and click through it; `nosniff` keeps a browser from second-guessing
the content type.

An `Invalid` or `NotFound` from the service shows its message as it is, in an element with
role="alert": `Invalid` on the page the form was on (status 400), `NotFound` on a small page that
links back to the list (status 404).
"""

import asyncio
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from starlette.datastructures import Headers, UploadFile
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates
from starlette.types import ASGIApp, Receive, Scope, Send

from podcast_feed.config import MAX_RETENTION_DAYS
from podcast_feed.db import Episode
from podcast_feed.errors import Invalid, NotFound
from podcast_feed.service import BAD_RETENTION, MAX_ARTWORK_BYTES, Service

# Found through importlib.resources: the image installs the package, so there is no source tree.
TEMPLATES_DIR = Path(str(resources.files("podcast_feed") / "templates"))
PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",  # the older spelling of frame-ancestors
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
}
REFUSED = "cross-site request refused\n"
# What the worker is still busy with: the table refreshes itself until none is left.
UNFINISHED = ("queued", "downloading", "converting")
CONFIRM_DELETE = "To delete the feed, type its slug ('{slug}') in the box."

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_STATUS_WORDS = {
    "queued": "Queued",
    "downloading": "Downloading",
    "converting": "Converting",
    "ready": "Ready",
    "failed": "Failed",
}


# ------------------------------------------------------------------- same-origin protection


def _same_origin(headers: Headers) -> bool:
    """Whether a request that changes something comes from a page of this site.

    A browser says so itself in Sec-Fetch-Site. A client that does not send it (an older
    browser, curl) must at least send an Origin whose host is the Host it asked: only the
    host, because behind the tunnel the app sees http while the browser's Origin is https.
    With neither, nothing says the request comes from the admin's own pages, so it is refused.
    """
    site = headers.get("sec-fetch-site")
    if site is not None:
        return site == "same-origin"
    origin, host = headers.get("origin"), headers.get("host")
    if not origin or not host:
        return False
    try:
        origin_host = urlsplit(origin).netloc
    except ValueError:  # not even a URL
        return False
    # "null" (a sandboxed page) has no host, so it matches nothing.
    return origin_host.lower() == host.lower()


class SameOriginMiddleware:
    """Refuse every request that is not GET, HEAD or OPTIONS unless it is same-origin.

    A pure ASGI middleware, so it never touches the body. It runs in front of the whole app, the
    public routes included: those only answer GET and HEAD, and a cross-site POST to them gets
    the same 403 before the router can say 405.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["method"] not in _SAFE_METHODS
            and not _same_origin(Headers(scope=scope))
        ):
            await PlainTextResponse(REFUSED, status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)


# ------------------------------------------------------------------------- template helpers


def _source_host(episode: Episode) -> str:
    """Where an episode comes from, for a row without a title. Only the host: the link itself
    can be long and carries the file's address, which the pages never show."""
    try:
        host = urlsplit(episode.source_ref).hostname
    except ValueError:
        host = None
    return host or "unknown source"


def _status_text(episode: Episode) -> str:
    word = _STATUS_WORDS.get(episode.status, episode.status)
    # The failure reasons are plain sentences already ("too large", "timed out").
    return f"{word}: {episode.error}" if episode.status == "failed" and episode.error else word


def _added(episode: Episode) -> str:
    return episode.added_at.strftime("%Y-%m-%d %H:%M UTC")


def _templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=TEMPLATES_DIR)  # autoescape is on
    templates.env.filters["source_host"] = _source_host
    templates.env.filters["status_text"] = _status_text
    templates.env.filters["added"] = _added
    return templates


# --------------------------------------------------------------------------- reading forms


def _retention(text: str) -> int | None:
    """Retention from its form field: empty is the default; anything but digits is not a number
    of days (a sign, a decimal point and "1_0" included)."""
    text = text.strip()
    if not text:
        return None
    if not (text.isascii() and text.isdigit()):
        raise Invalid(BAD_RETENTION)
    # int() refuses thousands of digits; a number this long is above the cap whatever it is.
    return int(text) if len(text) <= 9 else MAX_RETENTION_DAYS + 1


async def _fields(request: Request) -> dict[str, str]:
    """The text fields of a form (a file sent in a text field is no value)."""
    async with request.form() as form:
        return {key: value for key, value in form.items() if isinstance(value, str)}


async def _fields_and_artwork(request: Request) -> tuple[dict[str, str], bytes]:
    """The text fields and the `artwork` file of a form, empty when none was chosen.

    The upload is read up to the limit plus one byte, so that the service can tell "too large"
    from "just fits" and the handler never holds more than that in memory. (Starlette's form
    parser has already spooled the whole body to a temporary file by now: only a limit on the
    request body, in front of the app, would stop that.)
    """
    async with request.form() as form:
        fields = {key: value for key, value in form.items() if isinstance(value, str)}
        upload = form.get("artwork")
        data = await upload.read(MAX_ARTWORK_BYTES + 1) if isinstance(upload, UploadFile) else b""
    return fields, data


# ------------------------------------------------------------------------ what a page shows


def _episodes_context(service: Service, slug: str) -> dict[str, Any]:
    """The feed and its episodes, for the table. Runs in a worker thread."""
    feed = service.get_feed(slug)
    episodes = service.list_episodes(slug, limit=None)
    return {
        "feed": feed,
        "episodes": episodes,
        "polling": any(episode.status in UNFINISHED for episode in episodes),
    }


def _feed_context(service: Service, slug: str) -> dict[str, Any]:
    """Everything the feed page shows. Runs in a worker thread."""
    context = _episodes_context(service, slug)
    context["feed_url"] = service.feed_url(slug)  # the secret: this page only
    context["retention_days"] = next(
        (s.retention_days for s in service.list_feeds() if s.feed.id == context["feed"].id), None
    )
    return context


def _redirect(url: str) -> Response:
    return RedirectResponse(url, status_code=303, headers=PAGE_HEADERS)


def _feed_location(slug: str) -> str:
    return f"/ui/feeds/{quote(slug, safe='')}"


def admin_routes(service: Service) -> list[Route]:
    templates = _templates()

    def render(request: Request, name: str, context: dict[str, Any], status: int = 200) -> Response:
        return templates.TemplateResponse(
            request, name, context, status_code=status, headers=PAGE_HEADERS
        )

    def not_found(request: Request, error: NotFound) -> Response:
        return render(request, "notfound.html", {"error": str(error)}, 404)

    async def feeds_page(
        request: Request,
        error: str | None = None,
        status: int = 200,
        values: dict[str, str] | None = None,
    ) -> Response:
        summaries = await asyncio.to_thread(service.list_feeds)
        context = {"summaries": summaries, "error": error, "values": values or {}}
        return render(request, "feeds.html", context, status)

    async def feed_page(
        request: Request,
        slug: str,
        error: str | None = None,
        status: int = 200,
        values: dict[str, dict[str, str]] | None = None,
    ) -> Response:
        """The page of one feed; `values` are what someone typed into a form that was refused,
        by form name, so that they can correct it instead of typing it again."""
        try:
            context = await asyncio.to_thread(_feed_context, service, slug)
        except NotFound as exc:
            return not_found(request, exc)
        context |= {"error": error, "values": values or {}}
        return render(request, "feed.html", context, status)

    # ---------------------------------------------------------------------------- pages

    async def index(request: Request) -> Response:
        return await feeds_page(request)

    async def feed(request: Request) -> Response:
        return await feed_page(request, request.path_params["slug"])

    async def episodes(request: Request) -> Response:
        """The table alone: what htmx fetches while episodes are being processed."""
        try:
            context = await asyncio.to_thread(
                _episodes_context, service, request.path_params["slug"]
            )
        except NotFound as exc:
            return not_found(request, exc)
        return render(request, "_episodes.html", context)

    # --------------------------------------------------------------------------- feeds

    async def create_feed(request: Request) -> Response:
        fields, artwork = await _fields_and_artwork(request)
        values = {
            name: fields.get(name, "")
            for name in ("title", "slug", "description", "retention_days")
        }
        try:
            created = await asyncio.to_thread(
                service.create_feed,
                fields.get("title", ""),
                fields.get("slug"),
                fields.get("description"),
                _retention(fields.get("retention_days", "")),
            )
        except Invalid as exc:
            return await feeds_page(request, str(exc), 400, values)
        if artwork:  # nothing chosen is no artwork, not a bad one
            try:
                await asyncio.to_thread(service.set_feed_artwork, created.slug, artwork)
            except Invalid as exc:
                # The feed exists now, so show its page: the artwork form there tries again.
                error = f"The feed was created, but its artwork was not set. {exc}"
                return await feed_page(request, created.slug, error, 400)
            except NotFound as exc:  # deleted in the meantime
                return not_found(request, exc)
        return _redirect(_feed_location(created.slug))

    async def settings(request: Request) -> Response:
        slug = request.path_params["slug"]
        fields = await _fields(request)
        typed = {name: fields.get(name, "") for name in ("title", "description", "retention_days")}
        try:
            await asyncio.to_thread(
                service.update_feed,
                slug,
                title=typed["title"],
                description=typed["description"],
                retention_days=_retention(typed["retention_days"]),
            )
        except Invalid as exc:
            return await feed_page(request, slug, str(exc), 400, {"settings": typed})
        except NotFound as exc:
            return not_found(request, exc)
        return _redirect(_feed_location(slug))

    async def artwork(request: Request) -> Response:
        slug = request.path_params["slug"]
        _, data = await _fields_and_artwork(request)
        try:
            await asyncio.to_thread(service.set_feed_artwork, slug, data)
        except Invalid as exc:  # no file chosen is empty data, which the service refuses too
            return await feed_page(request, slug, str(exc), 400)
        except NotFound as exc:
            return not_found(request, exc)
        return _redirect(_feed_location(slug))

    async def make_default(request: Request) -> Response:
        slug = request.path_params["slug"]
        try:
            await asyncio.to_thread(service.set_default, slug)
        except NotFound as exc:
            return not_found(request, exc)
        return _redirect(_feed_location(slug))

    async def delete_feed(request: Request) -> Response:
        slug = request.path_params["slug"]
        fields = await _fields(request)
        if fields.get("confirm") != slug:  # exactly the slug: no trimming, no other case
            return await feed_page(request, slug, CONFIRM_DELETE.format(slug=slug), 400)
        try:
            await asyncio.to_thread(service.delete_feed, slug)
        except NotFound as exc:
            return not_found(request, exc)
        return _redirect("/ui/")

    # ------------------------------------------------------------------------ episodes

    async def add_episode(request: Request) -> Response:
        slug = request.path_params["slug"]
        fields = await _fields(request)
        typed = {"url": fields.get("url", ""), "title": fields.get("title", "")}
        try:
            await asyncio.to_thread(
                service.add_episode, typed["url"], feed=slug, title=typed["title"]
            )
        except Invalid as exc:
            return await feed_page(request, slug, str(exc), 400, {"add": typed})
        except NotFound as exc:
            return not_found(request, exc)
        return _redirect(_feed_location(slug))

    async def on_episode(
        request: Request, action: Callable[..., object], *args: str | None
    ) -> Response:
        """Look the episode up, run `action(its id, *args)`, and go back to its feed. The
        address to go back to is the episode's own feed, never something the form says."""
        episode_id = request.path_params["episode_id"]
        try:
            episode = await asyncio.to_thread(service.get_episode, episode_id)
            await asyncio.to_thread(action, episode_id, *args)
        except NotFound as exc:
            return not_found(request, exc)
        except Invalid as exc:
            return await feed_page(request, episode.feed_slug, str(exc), 400)
        return _redirect(_feed_location(episode.feed_slug))

    async def edit_episode(request: Request) -> Response:
        fields = await _fields(request)
        return await on_episode(
            request, service.update_episode, fields.get("title"), fields.get("description")
        )

    async def retry_episode(request: Request) -> Response:
        return await on_episode(request, service.retry_episode)

    async def remove_episode(request: Request) -> Response:
        return await on_episode(request, service.remove_episode)

    return [
        Route("/ui/", index, methods=["GET"]),
        Route("/ui/feeds", create_feed, methods=["POST"]),
        Route("/ui/feeds/{slug}", feed, methods=["GET"]),
        Route("/ui/feeds/{slug}/episodes", episodes, methods=["GET"]),
        Route("/ui/feeds/{slug}/episodes", add_episode, methods=["POST"]),
        Route("/ui/feeds/{slug}/settings", settings, methods=["POST"]),
        Route("/ui/feeds/{slug}/artwork", artwork, methods=["POST"]),
        Route("/ui/feeds/{slug}/default", make_default, methods=["POST"]),
        Route("/ui/feeds/{slug}/delete", delete_feed, methods=["POST"]),
        Route("/ui/episodes/{episode_id}/edit", edit_episode, methods=["POST"]),
        Route("/ui/episodes/{episode_id}/retry", retry_episode, methods=["POST"]),
        Route("/ui/episodes/{episode_id}/remove", remove_episode, methods=["POST"]),
    ]
