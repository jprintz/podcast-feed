import asyncio
import contextlib
import dataclasses
import inspect
import logging
import shutil
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from podcast_feed import worker as worker_module
from podcast_feed.config import GIB, Settings
from podcast_feed.db import Database, Episode, Feed
from podcast_feed.errors import EpisodeFailure
from podcast_feed.sources import Fetched
from podcast_feed.storage import Storage
from podcast_feed.worker import Worker

T0 = datetime(2026, 10, 29, 12, 0, 0, tzinfo=UTC)
REF = "https://x.example/tone.mp3"


class FakeClock:
    """A settable clock; with a step it moves on by that much after every reading."""

    def __init__(self, now: datetime = T0, step: timedelta = timedelta(0)):
        self.now = now
        self.step = step

    def __call__(self) -> datetime:
        now = self.now
        self.now += self.step
        return now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


class FakeSource:
    """Copies `file` to dest, or raises `error`. It writes a partial file first, which a real
    source would delete on failure, so the tests see the worker's own clean-up. `during` runs
    in the middle of the download (it may be a coroutine function)."""

    kind = "url"

    def __init__(self) -> None:
        self.file: Path | None = None
        self.error: BaseException | None = None
        self.during: Callable[[], object] | None = None
        self.refs: list[str] = []

    async def fetch(self, ref: str, dest: Path) -> Fetched:
        self.refs.append(ref)
        dest.write_bytes(b"partial download")
        if self.during is not None:
            result = self.during()
            if inspect.isawaitable(result):
                await result
        if self.error is not None:
            raise self.error
        assert self.file is not None, "the test did not give the source a file"
        shutil.copyfile(self.file, dest)
        return Fetched(dest, self.file.name, "x.example")


@dataclass
class W:
    db: Database
    storage: Storage
    settings: Settings
    source: FakeSource
    clock: FakeClock
    feed: Feed
    media: Path
    worker: Worker | None = None

    def rebuild(self, **kwargs) -> Worker:
        """A new worker; settings=... replaces the settings, the rest go to Worker()."""
        settings = kwargs.pop("settings", self.settings)
        options = {"clock": self.clock, "free_bytes": lambda: 100 * GIB, **kwargs}
        self.worker = Worker(self.db, self.storage, settings, {"url": self.source}, **options)
        return self.worker


@pytest.fixture
def w(tmp_path):
    clock = FakeClock()
    settings = Settings(
        public_base="https://pod.example/s3cret",
        data_dir=tmp_path / "data",
        media_dir=tmp_path / "media",
        retention_days=30,
        max_bytes=GIB,
        max_hours=6.0,
        min_free_bytes=5 * GIB,
        web_port=8080,
        mcp_port=8000,
        host="127.0.0.1",
    )
    db = Database(":memory:", clock=clock)
    storage = Storage(settings.media_dir)
    storage.ensure_layout()
    feed = db.create_feed("talks", "Talks")
    state = W(db, storage, settings, FakeSource(), clock, feed, settings.media_dir)
    state.rebuild()
    yield state
    db.close()


def add(w: W, ref: str = REF, title: str | None = None, description: str | None = None):
    episode, created = w.db.add_or_get_episode(w.feed.id, "url", ref, title, description)
    assert created
    return episode


def published(w: W, ref: str, *, artwork: bool = False) -> Episode:
    """An episode made ready by hand, with its files in the feed folder."""
    e = add(w, ref)
    folder = w.storage.feed_dir("talks")
    folder.mkdir(exist_ok=True)
    (folder / f"{e.id}.mp3").write_bytes(b"audio")
    art = None
    if artwork:
        art = f"{e.id}.png"
        (folder / art).write_bytes(b"picture")
    assert w.db.mark_ready(
        e.id,
        title="T",
        description=None,
        artwork=art,
        file=f"{e.id}.mp3",
        mime="audio/mpeg",
        size_bytes=5,
        duration_s=2.0,
    )
    return w.db.get_episode(e.id)


async def until(condition: Callable[[], bool], timeout: float = 10) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


class Spinning(BaseException):
    """Stops a run() that spins: it would never give the event loop a turn, so the test (and
    in production both web servers) would hang instead of failing."""


@contextlib.asynccontextmanager
async def running(w: W):
    """run() as a task for the duration of the block, cancelled at its end.

    A run() that reads the queue over and over instead of waiting is stopped with Spinning,
    and the test fails.
    """
    real_next_queued = w.db.next_queued
    reads = 0

    def next_queued():
        nonlocal reads
        reads += 1
        if reads > 200:
            raise Spinning
        return real_next_queued()

    w.db.next_queued = next_queued
    task = asyncio.create_task(w.worker.run())
    try:
        yield task
    finally:
        task.cancel()
        try:
            await asyncio.wait_for(task, 10)  # a swallowed cancel fails instead of hanging
        except asyncio.CancelledError:
            pass
        except Spinning:
            pytest.fail("run() kept reading the queue instead of waiting")
        finally:
            del w.db.next_queued


def status(w: W, e: Episode) -> str | None:
    current = w.db.get_episode(e.id)
    return None if current is None else current.status


def feed_files(w: W) -> list[str]:
    folder = w.media / "talks"
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


# -------------------------------------------------------------------------------- process


async def test_mp3_becomes_ready(w, media):
    w.source.file = media("mp3")
    e = add(w, "https://x.example/tone.mp3")
    await w.worker.process(e.id)
    r = w.db.get_episode(e.id)
    assert (r.status, r.mime, r.title, r.file) == ("ready", "audio/mpeg", "Tone", f"{e.id}.mp3")
    assert (w.media / "talks" / r.file).stat().st_size == r.size_bytes and r.published_at
    assert r.error is None and r.artwork is None and abs(r.duration_s - 2) < 0.2
    assert w.source.refs == ["https://x.example/tone.mp3"]
    assert feed_files(w) == [f"{e.id}.mp3"] and not any(w.storage.tmp.iterdir())


async def test_given_title_kept(w, media):
    w.source.file = media("mp3")
    e = add(w, title="Mine", description="About it")
    await w.worker.process(e.id)
    r = w.db.get_episode(e.id)
    assert (r.status, r.title, r.description) == ("ready", "Mine", "About it")


async def test_title_edited_while_processing_is_kept(w, media):
    w.source.file = media("mp3")
    e = add(w)
    w.source.during = lambda: w.db.update_episode_text(e.id, "Edited", "Later")
    await w.worker.process(e.id)
    r = w.db.get_episode(e.id)
    assert (r.status, r.title, r.description) == ("ready", "Edited", "Later")


async def test_video_becomes_m4a(w, media):
    w.source.file = media("video")
    e = add(w, "https://x.example/clip.mp4")
    await w.worker.process(e.id)
    r = w.db.get_episode(e.id)
    assert r.status == "ready" and r.mime == "audio/mp4" and r.file.endswith(".m4a")
    assert r.file == f"{e.id}.m4a" and (w.media / "talks" / r.file).stat().st_size == r.size_bytes
    assert not any(w.storage.tmp.iterdir())


async def test_cover_saved(w, media):
    w.source.file = media("mp3_cover")
    e = add(w)
    await w.worker.process(e.id)
    r = w.db.get_episode(e.id)
    assert (r.status, r.artwork, r.file) == ("ready", f"{e.id}.png", f"{e.id}.mp3")
    assert (w.media / "talks" / f"{e.id}.png").read_bytes().startswith(b"\x89PNG")
    assert feed_files(w) == [f"{e.id}.mp3", f"{e.id}.png"] and not any(w.storage.tmp.iterdir())


async def test_failure_reason_and_clean_tmp(w):
    w.source.error = EpisodeFailure("the server answered HTTP 404")
    e = add(w)
    await w.worker.process(e.id)
    r = w.db.get_episode(e.id)
    assert (r.status, r.error) == ("failed", "the server answered HTTP 404") and not any(
        w.storage.tmp.iterdir()
    )


async def test_not_media_fails_and_clears_the_download(w, media):
    w.source.file = media("text")
    e = add(w)
    await w.worker.process(e.id)
    r = w.db.get_episode(e.id)
    assert (r.status, r.error) == ("failed", "not an audio or video file")
    assert not any(w.storage.tmp.iterdir()) and feed_files(w) == []


async def test_job_clears_only_its_own_temp_files(w):
    other = w.storage.tmp / "zzzzzzzzzzzz.download"
    other.write_bytes(b"another job")
    w.source.error = EpisodeFailure("timed out")
    e = add(w)
    await w.worker.process(e.id)
    assert status(w, e) == "failed"
    assert [p.name for p in w.storage.tmp.iterdir()] == ["zzzzzzzzzzzz.download"]


async def test_unexpected_error(w, caplog):
    caplog.set_level(logging.INFO, logger="podcast_feed")
    w.source.error = RuntimeError("boom")
    e = add(w)
    await w.worker.process(e.id)
    r = w.db.get_episode(e.id)
    assert (r.status, r.error) == ("failed", "internal error")
    assert not any(w.storage.tmp.iterdir())
    record = next(rec for rec in caplog.records if rec.exc_info)
    assert e.id in record.getMessage() and "boom" in caplog.text  # the traceback is logged
    assert REF not in caplog.text and "x.example" not in caplog.text


async def test_log_lines_name_the_episode_never_the_link(w, media, caplog):
    caplog.set_level(logging.DEBUG, logger="podcast_feed")
    w.source.file = media("mp3")
    ok = add(w)
    await w.worker.process(ok.id)
    w.source.error = EpisodeFailure("timed out")
    bad = add(w, "https://x.example/private?token=abc")
    await w.worker.process(bad.id)
    messages = [r.getMessage() for r in caplog.records]
    assert any(ok.id in m and "ready" in m for m in messages)
    assert any(bad.id in m and "timed out" in m for m in messages)
    assert "x.example" not in caplog.text and "token" not in caplog.text


async def test_disk_floor(w, media):
    w.source.file = media("mp3")
    w.rebuild(free_bytes=lambda: 1)
    e = add(w)
    await w.worker.process(e.id)
    r = w.db.get_episode(e.id)
    assert (r.status, r.error) == ("failed", "disk nearly full") and w.source.refs == []


async def test_disk_floor_asks_the_storage_by_default(w, monkeypatch):
    monkeypatch.setattr(w.storage, "free_bytes", lambda: 5 * GIB - 1)
    w.rebuild(free_bytes=None)
    e = add(w)
    await w.worker.process(e.id)
    assert w.db.get_episode(e.id).error == "disk nearly full" and w.source.refs == []


async def test_too_long(w, media):
    w.source.file = media("mp3")
    w.rebuild(settings=dataclasses.replace(w.settings, max_hours=0.0001))
    e = add(w)
    await w.worker.process(e.id)
    r = w.db.get_episode(e.id)
    assert (r.status, r.error) == ("failed", "too long")
    assert not any(w.storage.tmp.iterdir()) and feed_files(w) == []


async def test_unknown_duration(w, media, monkeypatch):
    real_probe = worker_module.probe

    async def no_duration(path):
        return dataclasses.replace(await real_probe(path), duration_s=None)

    monkeypatch.setattr(worker_module, "probe", no_duration)
    w.source.file = media("mp3")
    # Any known duration would be too long now.
    w.rebuild(settings=dataclasses.replace(w.settings, max_hours=0.0001))
    e = add(w)
    await w.worker.process(e.id)
    r = w.db.get_episode(e.id)
    assert (r.status, r.duration_s, r.error) == ("ready", None, None)


async def test_feed_deleted_mid_job(w, media):
    w.source.file = media("mp3")
    e = add(w)
    w.source.during = lambda: w.db.delete_feed("talks")
    await w.worker.process(e.id)
    assert (
        w.db.get_episode(e.id) is None
        and not list((w.media).rglob("*.mp3"))
        and not any(w.storage.tmp.iterdir())
    )


async def test_removed_before_mark_ready(w, media, monkeypatch):
    w.source.file = media("mp3_cover")
    e = add(w)
    real_mark_ready = w.db.mark_ready
    seen_in_feed = []

    def delete_first(id, **fields):
        seen_in_feed.extend(feed_files(w))  # the files were moved in already
        w.db.delete_episode(id)
        return real_mark_ready(id, **fields)

    monkeypatch.setattr(w.db, "mark_ready", delete_first)
    await w.worker.process(e.id)
    assert seen_in_feed == [f"{e.id}.mp3", f"{e.id}.png"]
    assert w.db.get_episode(e.id) is None
    assert feed_files(w) == [] and not any(w.storage.tmp.iterdir())


async def test_mark_ready_error_takes_the_files_back_out(w, media, monkeypatch):
    w.source.file = media("mp3_cover")
    e = add(w)

    def broken(id, **fields):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(w.db, "mark_ready", broken)
    await w.worker.process(e.id)
    assert (status(w, e), w.db.get_episode(e.id).error) == ("failed", "internal error")
    assert feed_files(w) == [] and not any(w.storage.tmp.iterdir())


async def test_gone_episode_is_ignored(w):
    await w.worker.process("aaaaaaaaaaaa")
    assert w.source.refs == []


async def test_process_never_raises_even_if_the_database_refuses_writes(w, monkeypatch, caplog):
    def refuse(*args, **kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(w.db, "set_status", refuse)
    e = add(w)
    await w.worker.process(e.id)
    assert status(w, e) == "queued" and w.source.refs == []
    assert "could not record" in caplog.text


async def test_cancelled_job_keeps_its_status_and_clears_tmp(w):
    started = asyncio.Event()

    async def hang():
        started.set()
        await asyncio.Event().wait()

    w.source.during = hang
    e = add(w)
    job = asyncio.create_task(w.worker.process(e.id))
    await started.wait()
    assert any(w.storage.tmp.iterdir())  # the partial download
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(job, 10)
    assert status(w, e) == "downloading" and not any(w.storage.tmp.iterdir())
    w.worker.recover()
    assert status(w, e) == "queued"


# --------------------------------------------------------------------- recover and sweep


def test_recover(w):
    (w.media / "CACHEDIR.TAG").unlink()
    (w.storage.tmp / "abcdefghijkl.download").write_bytes(b"x")
    (w.storage.tmp / "sub").mkdir()
    (w.storage.tmp / "sub" / "f").write_bytes(b"x")
    down, conv, failed, waiting = (add(w, f"https://x.example/{n}") for n in "abcd")
    w.db.set_status(down.id, "downloading")
    w.db.set_status(conv.id, "converting")
    w.db.set_status(failed.id, "failed", "timed out")
    ready = published(w, "https://x.example/e")
    w.worker.recover()
    assert w.storage.tmp.is_dir() and not any(w.storage.tmp.iterdir())
    assert (w.media / "CACHEDIR.TAG").read_text().startswith("Signature: ")
    assert [status(w, e) for e in (down, conv, failed, waiting, ready)] == [
        "queued",
        "queued",
        "failed",
        "queued",
        "ready",
    ]


def test_recover_makes_a_fresh_media_folder(w, tmp_path):
    w.storage = Storage(tmp_path / "fresh")
    w.rebuild()
    w.worker.recover()
    assert (tmp_path / "fresh" / ".tmp").is_dir()
    assert (tmp_path / "fresh" / "CACHEDIR.TAG").is_file()


def test_sweep(w):
    old = published(w, "https://x.example/old", artwork=True)
    w.clock.advance(days=31)
    lost = published(w, "https://x.example/lost", artwork=True)
    (w.media / "talks" / lost.file).unlink()
    fresh = published(w, "https://x.example/fresh", artwork=True)
    assert w.worker.sweep() == 2
    assert w.db.get_episode(old.id) is None and w.db.get_episode(lost.id) is None
    assert w.db.get_episode(fresh.id).status == "ready"
    assert feed_files(w) == sorted([fresh.file, fresh.artwork])


def test_sweep_removes_expired_failed_episodes_and_leaves_running_ones(w):
    failed, queued, downloading = (add(w, f"https://x.example/{n}") for n in "abc")
    w.db.set_status(failed.id, "failed", "timed out")
    w.db.set_status(downloading.id, "downloading")
    w.clock.advance(days=31)
    assert w.worker.sweep() == 1
    assert [status(w, e) for e in (failed, queued, downloading)] == [
        None,
        "queued",
        "downloading",
    ]


def test_sweep_honours_the_feeds_retention(w):
    short = w.db.create_feed("short", "Short", retention_days=2)
    e, _ = w.db.add_or_get_episode(short.id, "url", REF, None, None)
    w.db.set_status(e.id, "failed", "timed out")
    kept = add(w)
    w.db.set_status(kept.id, "failed", "timed out")
    w.clock.advance(days=3)
    assert w.worker.sweep() == 1
    assert status(w, e) is None and status(w, kept) == "failed"


def test_sweep_sees_more_than_fifty_episodes(w):
    episodes = [published(w, f"https://x.example/{n}") for n in range(60)]
    for e in episodes:
        (w.media / "talks" / e.file).unlink()
    assert w.worker.sweep() == 60
    assert w.db.list_episodes(limit=None) == []


def test_sweep_with_nothing_to_do(w):
    kept = published(w, "https://x.example/kept")
    assert w.worker.sweep() == 0
    assert status(w, kept) == "ready" and feed_files(w) == [kept.file]


async def test_sweep_loop_sweeps_at_once_and_retries_after_a_failure(w, monkeypatch, caplog):
    calls = []

    def sweep():
        calls.append(None)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return 0

    monkeypatch.setattr(w.worker, "sweep", sweep)
    task = asyncio.create_task(w.worker.sweep_loop(interval_s=0.01))
    try:
        await until(lambda: len(calls) >= 3)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert "sweep failed" in caplog.text and "database is locked" in caplog.text


async def test_sweep_loop_waits_for_the_interval(w, monkeypatch):
    calls = []
    monkeypatch.setattr(w.worker, "sweep", lambda: calls.append(None) or 0)
    task = asyncio.create_task(w.worker.sweep_loop(interval_s=3600))
    try:
        await until(lambda: len(calls) == 1)
        await asyncio.sleep(0.05)
        assert len(calls) == 1
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------- run and wake


async def test_run_processes_in_order(w, media):
    w.source.file = media("mp3")
    w.clock.step = timedelta(seconds=1)
    first, second = add(w, "https://x.example/1"), add(w, "https://x.example/2")
    async with running(w):
        w.worker.wake()
        await until(lambda: status(w, first) == status(w, second) == "ready")
    a, b = w.db.get_episode(first.id), w.db.get_episode(second.id)
    assert w.source.refs == ["https://x.example/1", "https://x.example/2"]
    assert a.published_at < b.published_at


async def test_run_takes_what_was_queued_before_it_started(w, media):
    w.source.file = media("mp3")
    w.worker.wake()  # before run(): nothing to wake, and no error
    e = add(w)
    async with running(w):
        await until(lambda: status(w, e) == "ready")


async def test_wake_from_another_thread(w, media):
    w.source.file = media("mp3")
    async with running(w):
        await asyncio.sleep(0.05)  # run() finds nothing and waits
        e = add(w)
        await asyncio.sleep(0.1)
        assert status(w, e) == "queued"  # nothing polls the queue
        await asyncio.to_thread(w.worker.wake)
        await until(lambda: status(w, e) == "ready")


async def test_wake_while_busy_is_not_lost(w, media):
    w.source.file = media("mp3")
    gate = asyncio.Event()
    w.source.during = gate.wait
    first = add(w, "https://x.example/1")
    async with running(w):
        await until(lambda: status(w, first) == "downloading")
        second = add(w, "https://x.example/2")
        thread = threading.Thread(target=w.worker.wake)
        thread.start()
        thread.join()
        await asyncio.sleep(0.01)
        gate.set()
        await until(lambda: status(w, first) == status(w, second) == "ready")


async def test_cancelling_run_mid_job(w):
    started = asyncio.Event()

    async def hang():
        started.set()
        await asyncio.Event().wait()

    w.source.during = hang
    e = add(w)
    task = asyncio.create_task(w.worker.run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    assert task.cancelled()
    assert status(w, e) == "downloading" and not any(w.storage.tmp.iterdir())


def test_wake_after_the_loop_closed(w):
    async def start_and_stop():
        async with running(w):
            await asyncio.sleep(0.01)

    asyncio.run(start_and_stop())
    w.worker.wake()  # the loop is closed: nothing to wake, and no error
    thread = threading.Thread(target=w.worker.wake)
    thread.start()
    thread.join()  # an exception there would fail the test (-W error)


async def test_run_does_not_spin_when_the_database_refuses_writes(w, monkeypatch):
    def refuse(*args, **kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    tries = []

    def free_bytes():  # the first thing each try calls
        tries.append(None)
        if len(tries) > 2:
            raise Spinning
        return 100 * GIB

    monkeypatch.setattr(w.db, "set_status", refuse)
    w.rebuild(free_bytes=free_bytes)
    e = add(w)
    task = asyncio.create_task(w.worker.run())
    try:
        await until(lambda: tries or task.done())
        await asyncio.sleep(0.1)
        assert len(tries) == 1 and not task.done()  # waiting, not spinning
        assert status(w, e) == "queued"
        w.worker.wake()
        await until(lambda: len(tries) == 2 or task.done())  # a wake tries it again at once
        assert len(tries) == 2 and not task.done()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Spinning):
            await task
