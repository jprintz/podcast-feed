"""The MCP server: five tools to add, poll, list and remove episodes.

Claude reaches it through a Cloudflare MCP portal. The tools never return a feed URL (it is
the secret that protects the feed), the link an episode came from or a file name, and the
server never logs the arguments of a call: one line per call says which tool ran, which
episode id or feed slug it concerned and how it ended (a bug adds the frames of its traceback,
never the exception's message).

Every service call blocks (SQLite, locks, files), so each goes through `asyncio.to_thread`.
"""

import asyncio
import logging
import time
import traceback
from collections.abc import Callable
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import AfterValidator, BeforeValidator, Field

from podcast_feed import __version__
from podcast_feed.db import Episode
from podcast_feed.errors import PodcastError
from podcast_feed.service import STATUSES, FeedSummary, Service
from podcast_feed.slug import valid_slug

log = logging.getLogger("podcast_feed.mcp")

MAX_LIMIT = 500

INSTRUCTIONS = (
    "Manages julius's private podcast feeds. add_episode takes a direct link to an audio or "
    "video file (not a web page or a YouTube link), returns at once with status 'queued', and "
    "the file is downloaded and converted in the background: call get_episode until it is "
    "'ready' or 'failed'. Without feed, the default feed is used; list_feeds shows the slugs. "
    "Feeds are created in the admin UI. Feed URLs are never returned here."
)


def _hints(
    title: str, *, read_only: bool, destructive: bool, open_world: bool = False
) -> ToolAnnotations:
    """All four hints, always: a client must not have to guess any of them."""
    return ToolAnnotations(
        title=title,
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=True,
        open_world_hint=open_world,
    )


def _name(value: str) -> str:
    """An episode ID or a feed slug as the service should see it: stripped, and only ever made
    of lowercase letters, digits and dashes (so a blank one stays blank).

    The service's messages quote the ID or slug they were given ("No episode 'x'."), and the SDK
    logs the message of every failed call. A link or a sentence in one of these arguments
    would end up in the log that way, so it is rejected here instead: for a rejected
    argument the SDK logs the field name only.
    """
    value = value.strip()
    if value and not valid_slug(value):
        raise ValueError("not an ID or slug: use only lowercase letters, digits and dashes")
    return value


Name = Annotated[str, AfterValidator(_name)]
EpisodeId = Annotated[Name, Field(description="Episode ID, from add_episode or list_episodes")]


def _blank_to_none(value: Any) -> Any:
    """A status without surrounding whitespace, a blank one as None (any), like a blank feed."""
    if isinstance(value, str):
        return value.strip() or None
    return value


# Before the check, so that a link in this argument is rejected like any other value that is
# not a status (and logged by the SDK as a field name only), never quoted by the service.
Status = Annotated[Literal[*STATUSES] | None, BeforeValidator(_blank_to_none)]


def _episode(episode: Episode) -> dict[str, Any]:
    """What a client may know of an episode: no link it came from, no file names."""
    return {
        "id": episode.id,
        "feed": episode.feed_slug,
        "title": episode.title,
        "description": episode.description,
        "status": episode.status,
        "error": episode.error,
        "added_at": episode.added_at.isoformat(),
        "duration_s": episode.duration_s,
    }


def _feed(summary: FeedSummary) -> dict[str, Any]:
    """A feed without its URL, which is the secret. retention_days is the effective one."""
    feed = summary.feed
    return {
        "slug": feed.slug,
        "title": feed.title,
        "description": feed.description,
        "episode_count": summary.episode_count,
        "retention_days": summary.retention_days,
        "default": feed.is_default,
    }


async def _run(
    name: str,
    target: str | None,
    call: Callable[[], dict[str, Any]],
    *,
    target_from_result: Callable[[dict[str, Any]], str] | None = None,
) -> dict[str, Any]:
    """Run the blocking `call` in a thread and log the tool, its target and the outcome.

    Never the arguments. `target` is the episode ID or feed slug the caller passed (None or
    blank: "-"), which `_name` has checked; `target_from_result` takes it from a successful
    result instead. A PodcastError becomes a ToolError carrying its message, which is written
    for the user.

    Anything else is a bug, and its message could quote what the caller passed (a library's
    error about a link, say). The client gets "internal error" and the log gets the exception's
    type and the traceback's frames (file, line, function and code), never its message; the
    SDK's own record of an unexpected exception, which carries the message, is thereby avoided.
    Cancellation is logged as an error too.
    """
    start = time.monotonic()
    outcome = "ok"
    frames = ""
    try:
        result = await asyncio.to_thread(call)
        if target_from_result is not None:
            target = target_from_result(result)
        return result
    except PodcastError as e:
        outcome = f"error:{type(e).__name__}"
        raise ToolError(str(e)) from None
    except Exception as e:
        outcome = f"error:{type(e).__name__}"
        frames = "\n" + "".join(traceback.format_tb(e.__traceback__)).rstrip()
        raise ToolError("internal error") from None
    except BaseException as e:  # cancellation: not an exception to turn into a result
        outcome = f"error:{type(e).__name__}"
        raise
    finally:
        ms = round((time.monotonic() - start) * 1000)
        level = logging.ERROR if frames else logging.INFO
        log.log(
            level, "tool=%s target=%s outcome=%s ms=%d%s", name, target or "-", outcome, ms, frames
        )


def create_mcp(service: Service) -> MCPServer:
    mcp = MCPServer("podcast_feed", version=__version__, instructions=INSTRUCTIONS)

    def tool(title: str, *, read_only: bool, destructive: bool, open_world: bool = False):
        return mcp.tool(
            title=title,
            annotations=_hints(
                title, read_only=read_only, destructive=destructive, open_world=open_world
            ),
        )

    @tool("List podcast feeds", read_only=True, destructive=False)
    async def list_feeds() -> dict[str, Any]:
        """List the podcast feeds: slug, title, description, how many ready episodes each has,
        how many days episodes are kept, and which feed is the default. Use a slug as the feed
        of add_episode or list_episodes. Feeds are created in the admin UI; their URLs are
        never returned."""
        return await _run(
            "list_feeds",
            None,
            lambda: {"feeds": [_feed(summary) for summary in service.list_feeds()]},
        )

    @tool("Add an episode from a file link", read_only=False, destructive=False, open_world=True)
    async def add_episode(
        url: Annotated[
            str,
            Field(
                description="Direct link to an audio or video file, http or https. "
                "Not a web page and not a YouTube link."
            ),
        ],
        feed: Annotated[
            Name | None,
            Field(description="Feed slug from list_feeds; leave out for the default feed"),
        ] = None,
        title: Annotated[
            str | None,
            Field(description="Episode title; leave out to take it from the file's tags"),
        ] = None,
        description: Annotated[
            str | None,
            Field(description="Episode description; leave out to take it from the file's tags"),
        ] = None,
    ) -> dict[str, Any]:
        """Add an episode to a feed from a direct link to an audio or video file.

        Returns at once with status 'queued': the file is downloaded and converted in the
        background, so call get_episode until the status is 'ready' or 'failed' (the reason
        is then in error). Without feed the default feed is used. Adding a link that is
        already in the feed returns that episode; one that failed is queued again.
        """
        return await _run(
            "add_episode",
            feed,
            lambda: _episode(service.add_episode(url, feed, title, description)),
            target_from_result=lambda result: result["id"],
        )

    @tool("List episodes", read_only=True, destructive=False)
    async def list_episodes(
        feed: Annotated[
            Name | None,
            Field(description="Only this feed (slug from list_feeds); leave out for all feeds"),
        ] = None,
        status: Annotated[
            Status, Field(description="Only episodes with this status; leave out for any")
        ] = None,
        limit: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_LIMIT,
                description=f"How many episodes to return at most, 1 to {MAX_LIMIT}",
            ),
        ] = 50,
    ) -> dict[str, Any]:
        """List episodes, newest first: id, feed, title, description, status, error, added_at
        and duration_s. Filter by feed and by status."""
        return await _run(
            "list_episodes",
            feed,
            lambda: {
                "episodes": [
                    _episode(episode) for episode in service.list_episodes(feed, status, limit)
                ]
            },
        )

    @tool("Get an episode's status", read_only=True, destructive=False)
    async def get_episode(episode_id: EpisodeId) -> dict[str, Any]:
        """Show one episode. Its status is queued, downloading, converting, ready or failed; for
        a failed one, error says why. Call it after add_episode until it is ready or failed."""
        return await _run(
            "get_episode", episode_id, lambda: _episode(service.get_episode(episode_id))
        )

    @tool("Remove an episode", read_only=False, destructive=True)
    async def remove_episode(episode_id: EpisodeId) -> dict[str, Any]:
        """Remove an episode and its files from its feed. This cannot be undone; to get the
        episode back, add its link again."""

        def remove() -> dict[str, Any]:
            service.remove_episode(episode_id)
            return {"removed": episode_id}

        return await _run("remove_episode", episode_id, remove)

    return mcp
