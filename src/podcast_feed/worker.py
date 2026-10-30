"""The worker: turns queued episodes into podcast audio in their feed's folder, one at a time.

It runs on the event loop next to the two web servers. The admin UI and the MCP tools add
episodes from worker threads and then call `wake()`, which is safe from any thread.

A job checks the disk floor, downloads into media/.tmp/ (`downloading`; an upload is already
there and only moves), inspects and converts (`converting`), then moves the audio and its cover
into media/<slug>/ and publishes the episode (`ready`). A failure sets `failed` with its reason.
The job's temporary files, an upload's included, are always deleted. Log lines name the episode id and the outcome, never the link: a link can
carry a private token.
"""

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from podcast_feed.audio import Probe, convert, extract_cover, plan_output, probe
from podcast_feed.config import Settings
from podcast_feed.db import Database, Episode, utcnow
from podcast_feed.errors import EpisodeFailure
from podcast_feed.metadata import episode_description, episode_title
from podcast_feed.sources import Fetched, Source
from podcast_feed.storage import Storage

log = logging.getLogger(__name__)

DISK_NEARLY_FULL = "disk nearly full"
TOO_LONG = "too long"
INTERNAL_ERROR = "internal error"
# How long run() waits before it tries again an episode that process() could not even start
# (the database refuses writes), unless wake() comes first.
STUCK_RETRY_S = 60.0

_REMOVED = "was removed while it was being processed"


class Worker:
    def __init__(
        self,
        db: Database,
        storage: Storage,
        settings: Settings,
        sources: Mapping[str, Source],
        *,
        clock: Callable[[], datetime] = utcnow,
        free_bytes: Callable[[], int] | None = None,
    ) -> None:
        self._db = db
        self._storage = storage
        self._settings = settings
        self._sources = sources
        self._clock = clock
        self._free_bytes = free_bytes if free_bytes is not None else storage.free_bytes
        # Set by run(): one attribute, so another thread never sees half of it.
        self._waker: tuple[asyncio.AbstractEventLoop, asyncio.Event] | None = None

    # --------------------------------------------------------------- start and queue

    def recover(self) -> None:
        """Prepare media/ and queue again what a restart interrupted. Call it before run()."""
        self._storage.ensure_layout()
        self._storage.clear_tmp()
        requeued = self._db.reset_unfinished()
        if requeued:
            log.info("Queued %d interrupted episode(s) again", requeued)

    def wake(self) -> None:
        """Tell run() that an episode was queued. Safe from any thread, at any time."""
        waker = self._waker
        if waker is None:
            return  # run() has not started; it looks at the queue first when it does
        loop, event = waker
        with contextlib.suppress(RuntimeError):  # the loop is closed: nobody is waiting
            loop.call_soon_threadsafe(event.set)

    async def run(self) -> None:
        """Process queued episodes, oldest first, until cancelled; wait for wake() when none
        are left. Cancelling it mid-job leaves that episode's status as it is, for recover()."""
        event = asyncio.Event()
        self._waker = (asyncio.get_running_loop(), event)
        while True:
            # Cleared before the queue is read, so a wake from now on is never lost.
            event.clear()
            episode = self._db.next_queued()
            if episode is None:
                await event.wait()
                continue
            await self.process(episode.id)
            after = self._db.get_episode(episode.id)
            if after is not None and after.status == "queued":
                # process() could not even mark it as started, so the database refuses
                # writes: wait a while (or for the next wake) rather than spin on it.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(event.wait(), STUCK_RETRY_S)

    # ------------------------------------------------------------------------ one job

    async def process(self, episode_id: str) -> None:
        """Fetch, convert and publish one episode. Never raises: a failure marks the episode
        failed. Only cancellation goes through (after the temporary files are deleted)."""
        try:
            outcome = await self._job(episode_id)
        except EpisodeFailure as exc:
            log.info("Episode %s failed: %s", episode_id, exc)
            self._fail(episode_id, str(exc))
        except Exception:
            log.exception("Episode %s failed: %s", episode_id, INTERNAL_ERROR)
            self._fail(episode_id, INTERNAL_ERROR)
        else:
            log.info("Episode %s %s", episode_id, outcome)
        finally:
            self._clear_job_files(episode_id)

    async def _job(self, episode_id: str) -> str:
        """Do the job; returns how it ended. Failures raise (see process)."""
        episode = self._db.get_episode(episode_id)
        if episode is None:
            return "no longer exists"
        if self._free_bytes() < self._settings.min_free_bytes:
            raise EpisodeFailure(DISK_NEARLY_FULL)
        tmp = self._storage.tmp

        self._db.set_status(episode_id, "downloading")
        source = self._sources[episode.source_kind]
        fetched = await source.fetch(episode.source_ref, tmp / f"{episode_id}.download")

        self._db.set_status(episode_id, "converting")
        info = await probe(fetched.path)
        if info.duration_s is not None and info.duration_s > self._settings.max_hours * 3600:
            raise EpisodeFailure(TOO_LONG)
        plan, extension, mime = plan_output(info)
        # The cover comes first: the "keep" plan moves the download away.
        cover = await extract_cover(fetched.path, tmp / episode_id, info)
        audio = tmp / f"{episode_id}{extension}"
        await convert(fetched.path, audio, plan)

        # Re-read: it may have been edited or deleted meanwhile. get_episode joins the feed,
        # so a deleted feed (whose episodes go with it) shows up here as None too.
        current = self._db.get_episode(episode_id)
        if current is None:
            return _REMOVED
        return self._publish(current, info, fetched, audio, cover, mime)

    def _publish(
        self,
        episode: Episode,
        info: Probe,
        fetched: Fetched,
        audio: Path,
        cover: Path | None,
        mime: str,
    ) -> str:
        """Move the files into the feed's folder and mark the episode ready.

        No await in here: once the files are in place, nothing else runs before mark_ready.
        """
        slug = episode.feed_slug
        self._storage.feed_dir(slug).mkdir(parents=True, exist_ok=True)
        size = audio.stat().st_size
        moved: list[Path] = []
        published = False
        try:
            for src in (audio, cover):
                if src is not None:
                    dest = self._storage.path(slug, src.name)
                    os.replace(src, dest)
                    moved.append(dest)
            published = self._db.mark_ready(
                episode.id,
                title=episode_title(
                    episode.title, info.tags, fetched.filename, fetched.host, self._clock().date()
                ),
                description=episode_description(episode.description, info.tags),
                artwork=cover.name if cover is not None else None,
                file=audio.name,
                mime=mime,
                size_bytes=size,
                duration_s=info.duration_s,
            )
        finally:
            if not published:  # deleted meanwhile, or something failed: no files left behind
                for path in moved:
                    path.unlink(missing_ok=True)
        return "is ready" if published else _REMOVED

    def _fail(self, episode_id: str, reason: str) -> None:
        try:
            self._db.set_status(episode_id, "failed", reason)
        except Exception:
            log.exception("Episode %s: could not record the failure", episode_id)

    def _clear_job_files(self, episode_id: str) -> None:
        """Delete this job's temporary files (media/.tmp/<id>*), and nothing else."""
        try:
            for path in self._storage.tmp.iterdir():
                if path.name.startswith(episode_id):
                    path.unlink(missing_ok=True)
        except OSError:
            log.exception("Episode %s: could not delete its temporary files", episode_id)

    # ------------------------------------------------------------------------ retention

    def sweep(self) -> int:
        """Remove expired episodes, and ready ones whose audio file is gone, with their files.

        Returns how many episodes were removed.
        """
        doomed = {e.id: e for e in self._db.expired(self._settings.retention_days)}
        for e in self._db.list_episodes(status="ready", limit=None):
            if e.file is None or not self._storage.exists(e.feed_slug, e.file):
                doomed.setdefault(e.id, e)
        for e in doomed.values():
            # Files first: if deleting the row fails, the next sweep finds the row again.
            self._storage.delete_episode_files(e.feed_slug, e)
            self._db.delete_episode(e.id)
        if doomed:
            log.info("Swept %d episode(s)", len(doomed))
        return len(doomed)

    async def sweep_loop(self, interval_s: float = 3600) -> None:
        """Sweep now, then every interval_s seconds, until cancelled. A failed sweep is
        logged and tried again at the next interval."""
        while True:
            try:
                self.sweep()
            except Exception:
                log.exception("The retention sweep failed; it runs again in %g s", interval_s)
            await asyncio.sleep(interval_s)
