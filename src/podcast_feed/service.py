"""The service layer: every operation on feeds and episodes, in one place.

The admin UI (Starlette runs its sync endpoints in a thread pool) and the MCP tools (through
`asyncio.to_thread`) both call it, so it runs in several threads at once, next to the worker on
the event loop, which shares the same Database and Storage. Calls into the database are plain
synchronous calls: SQLite is fast and the Database serialises them. What the database cannot
make atomic on its own is guarded by two locks here:

- `_feed_files_lock`: a feed's cover and the deletion of its folder, which would otherwise race
  (a cover written into a folder that was just removed would stay behind for good);
- `_requeue_lock`: "is the episode failed? then queue it again", so that two threads that retry or
  re-add the same failed episode queue it once and wake the worker once.

Errors are PodcastErrors whose message is shown as it is, in the admin UI and to the MCP
client. So the database's own NotFound ("There is no feed ...") never gets out: it is turned
into the messages below. `settings.public_base` is a secret and appears only in `feed_url`.
"""

import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from podcast_feed.config import Settings
from podcast_feed.db import Database, Episode, Feed
from podcast_feed.errors import Invalid, NotFound
from podcast_feed.guard import check_url
from podcast_feed.slug import slugify, valid_slug
from podcast_feed.storage import Storage

NO_FEEDS = "No feeds yet: create one in the podcast admin UI first."
NEEDS_TITLE = "A feed needs a title."
BAD_SLUG = "Slugs are 1 to 40 lowercase letters, digits and dashes."
BAD_RETENTION = "Retention must be at least 1 day."
BAD_ARTWORK = "Artwork must be a PNG or JPEG under 5 MB."
ONLY_FAILED = "Only failed episodes can be retried."
BAD_LIMIT = "The limit must be at least 1."

STATUSES = ("queued", "downloading", "converting", "ready", "failed")
# The only kind of source there is: a link to a file the worker downloads.
SOURCE_KIND = "url"

MAX_ARTWORK_BYTES = 5 * 1024 * 1024
# What a PNG and a JPEG start with, and the extension the cover gets.
_ARTWORK_MAGIC = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpg"))
_ARTWORK_EXTENSIONS = ("png", "jpg")


@dataclass(frozen=True)
class FeedSummary:
    """A feed with its number of ready episodes and the retention that applies to it."""

    feed: Feed
    episode_count: int
    retention_days: int


def _unknown_feed(slug: str) -> NotFound:
    return NotFound(f"Unknown feed '{slug}'. Call list_feeds for the slugs.")


def _no_episode(id: str) -> NotFound:
    return NotFound(f"No episode '{id}'.")


@contextmanager
def _feed_exists(slug: str) -> Iterator[None]:
    """Turn the database's NotFound for this feed into ours."""
    try:
        yield
    except NotFound:
        raise _unknown_feed(slug) from None


@contextmanager
def _episode_exists(id: str) -> Iterator[None]:
    """Turn the database's NotFound for this episode into ours."""
    try:
        yield
    except NotFound:
        raise _no_episode(id) from None


def _clean(text: str | None) -> str | None:
    """text without surrounding whitespace; None if nothing is left."""
    text = (text or "").strip()
    return text or None


def _feed_title(title: str) -> str:
    cleaned = _clean(title)
    if cleaned is None:
        raise Invalid(NEEDS_TITLE)
    return cleaned


def _retention(days: int | None) -> int | None:
    if days is None:
        return None
    if not isinstance(days, int) or days < 1:
        raise Invalid(BAD_RETENTION)
    return days


def _artwork_extension(data: bytes) -> str:
    """The cover's extension, from the first bytes; Invalid unless a PNG or JPEG up to 5 MiB."""
    if len(data) <= MAX_ARTWORK_BYTES:
        for magic, extension in _ARTWORK_MAGIC:
            if data.startswith(magic):
                return extension
    raise Invalid(BAD_ARTWORK)


class Service:
    def __init__(
        self,
        db: Database,
        storage: Storage,
        settings: Settings,
        wake: Callable[[], None],
    ) -> None:
        """`wake` tells the worker an episode was queued (Worker.wake: safe from any thread)."""
        self._db = db
        self._storage = storage
        self._settings = settings
        self._wake = wake
        self._feed_files_lock = threading.Lock()
        self._requeue_lock = threading.Lock()

    # ------------------------------------------------------------------------ feeds

    def list_feeds(self) -> list[FeedSummary]:
        """Every feed, oldest first, with its ready episodes and its effective retention."""
        counts = self._db.ready_counts()
        return [
            FeedSummary(
                feed=feed,
                episode_count=counts.get(feed.id, 0),
                retention_days=(
                    self._settings.retention_days
                    if feed.retention_days is None
                    else feed.retention_days
                ),
            )
            for feed in self._db.list_feeds()
        ]

    def get_feed(self, slug: str) -> Feed:
        feed = self._db.get_feed(slug)
        if feed is None:
            raise _unknown_feed(slug)
        return feed

    def create_feed(
        self,
        title: str,
        slug: str | None = None,
        description: str | None = None,
        retention_days: int | None = None,
    ) -> Feed:
        """Create a feed; the first one becomes the default.

        The title is stripped and must not be empty. The slug (a blank one counts as none)
        defaults to the title's; description and retention_days (None: the global default)
        are optional.
        """
        title = _feed_title(title)
        chosen = _clean(slug) or slugify(title)
        if not valid_slug(chosen):
            raise Invalid(BAD_SLUG)
        return self._db.create_feed(chosen, title, _clean(description), _retention(retention_days))

    def update_feed(
        self, slug: str, *, title: str, description: str | None, retention_days: int | None
    ) -> Feed:
        title = _feed_title(title)
        description = _clean(description)
        retention_days = _retention(retention_days)
        with _feed_exists(slug):
            return self._db.update_feed(
                slug, title=title, description=description, retention_days=retention_days
            )

    def set_feed_artwork(self, slug: str, data: bytes) -> Feed:
        """Make `data`, a PNG or JPEG of at most 5 MiB, the feed's cover (cover.png or
        cover.jpg in its folder, replacing the other kind)."""
        extension = _artwork_extension(data)
        name = f"cover.{extension}"
        with self._feed_files_lock:
            self.get_feed(slug)  # before any file is written
            folder = self._storage.feed_dir(slug)
            folder.mkdir(parents=True, exist_ok=True)
            # A hidden name is never served, and the lock means nobody else uses it now.
            pending = folder / ".cover.upload"
            try:
                pending.write_bytes(data)
                os.replace(pending, folder / name)
            except BaseException:
                pending.unlink(missing_ok=True)
                raise
            with _feed_exists(slug):
                feed = self._db.set_feed_artwork(slug, name)
            for other in _ARTWORK_EXTENSIONS:
                if other != extension:
                    (folder / f"cover.{other}").unlink(missing_ok=True)
            return feed

    def set_default(self, slug: str) -> None:
        with _feed_exists(slug):
            self._db.set_default(slug)

    def delete_feed(self, slug: str) -> None:
        """Delete the feed, its episodes and its folder. The rows go first: a file that outlives
        its row is only clutter, a row without its file is a broken feed."""
        with self._feed_files_lock:
            with _feed_exists(slug):
                self._db.delete_feed(slug)
            self._storage.delete_feed(slug)

    def feed_url(self, slug: str) -> str:
        """The feed's public address, which carries the secret: for the admin UI only."""
        self.get_feed(slug)
        return f"{self._settings.public_base}/feeds/{slug}.xml"

    # --------------------------------------------------------------------- episodes

    def add_episode(
        self,
        url: str,
        feed: str | None = None,
        title: str | None = None,
        description: str | None = None,
    ) -> Episode:
        """Queue the file at `url` in the feed (slug), or in the default feed.

        Returns at once: the worker does the rest. title and description are stripped, and
        blank ones count as none. The link is the episode's identity within a feed, so adding
        it again gives the existing episode: one that failed is queued again, any other is
        returned as it is, and title and description given the second time are ignored.
        The worker is woken exactly when this call queued an episode.
        """
        link = check_url(url)
        target = self._feed_for_add(feed)
        try:
            episode, created = self._db.add_or_get_episode(
                target.id, SOURCE_KIND, link, _clean(title), _clean(description)
            )
        except sqlite3.IntegrityError:
            # The feed was deleted after it was looked up: the episode has no feed to belong to.
            raise _unknown_feed(target.slug) from None
        if created:
            self._wake()
        elif episode.status == "failed":
            episode, queued = self._requeue_if_failed(episode.id)
            if queued:
                self._wake()
        return episode

    def list_episodes(
        self, feed: str | None = None, status: str | None = None, limit: int | None = 50
    ) -> list[Episode]:
        """Episodes, newest first, of one feed (slug) or of all, optionally with one status.
        limit=None returns them all."""
        if status is not None and status not in STATUSES:
            raise Invalid(f"Unknown status '{status}'. Use one of: {', '.join(STATUSES)}.")
        if limit is not None and limit < 1:  # SQLite would read a negative one as "no limit"
            raise Invalid(BAD_LIMIT)
        slug = _clean(feed)
        feed_id = None if slug is None else self.get_feed(slug).id
        return self._db.list_episodes(feed_id=feed_id, status=status, limit=limit)

    def get_episode(self, id: str) -> Episode:
        episode = self._db.get_episode(id)
        if episode is None:
            raise _no_episode(id)
        return episode

    def update_episode(self, id: str, title: str | None, description: str | None) -> Episode:
        """Set the title and description; blank ones count as none."""
        with _episode_exists(id):
            return self._db.update_episode_text(id, _clean(title), _clean(description))

    def retry_episode(self, id: str) -> Episode:
        """Queue a failed episode again; any other is Invalid."""
        episode, queued = self._requeue_if_failed(id)
        if not queued:
            raise Invalid(ONLY_FAILED)
        self._wake()
        return episode

    def remove_episode(self, id: str) -> None:
        """Delete the episode and its files. The row goes first, then every `<id>.*` file in
        the feed's folder, not only the names on the row that was read: a worker that publishes
        the episode between that read and the delete leaves files the row does not name."""
        episode = self.get_episode(id)
        self._db.delete_episode(id)
        self._storage.delete_episode_files(episode.feed_slug, episode)
        self._storage.delete_episode_files_by_id(episode.feed_slug, episode.id)

    # ---------------------------------------------------------------------- helpers

    def _feed_for_add(self, slug: str | None) -> Feed:
        slug = _clean(slug)
        if slug is not None:
            return self.get_feed(slug)
        default = self._db.default_feed()  # there is one as long as there is any feed
        if default is None:
            raise Invalid(NO_FEEDS)
        return default

    def _requeue_if_failed(self, id: str) -> tuple[Episode, bool]:
        """Queue the episode again if it failed. Returns it as it is now, and whether this call
        queued it: of several threads that try at once, exactly one does."""
        with self._requeue_lock:
            episode = self.get_episode(id)
            if episode.status != "failed":
                return episode, False
            self._db.requeue(id)
            # requeue ignores a missing episode, and the sweep may just have deleted this one.
            return self.get_episode(id), True
