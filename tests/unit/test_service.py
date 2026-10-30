import asyncio
import dataclasses
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import pytest

from podcast_feed.config import GIB, Settings
from podcast_feed.db import Database, Episode
from podcast_feed.errors import Invalid, NotFound, TooLarge
from podcast_feed.service import MAX_UPLOAD_BYTES, FeedSummary, Service
from podcast_feed.storage import Storage

URL = "https://x.example/a.mp3"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 10
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 10
FIVE_MIB = 5 * 1024 * 1024


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def storage(tmp_path):
    store = Storage(tmp_path / "media")
    store.ensure_layout()
    return store


@pytest.fixture
def wakes():
    """One entry per call of the worker's wake(); list.append is safe from any thread."""
    return []


@pytest.fixture
def svc(db, storage, tmp_path, wakes):
    settings = Settings(
        public_base="https://podcast-api.example.com/s3cr3t",
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
    return Service(db, storage, settings, lambda: wakes.append(None))


def publish(db: Database, storage: Storage, episode: Episode, *, artwork: bool = False) -> Episode:
    """What the worker does when it is done: files in the feed's folder, then mark_ready."""
    folder = storage.feed_dir(episode.feed_slug)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{episode.id}.mp3").write_bytes(b"audio")
    if artwork:
        (folder / f"{episode.id}.png").write_bytes(b"art")
    assert db.mark_ready(
        episode.id,
        title="Ready",
        description=None,
        artwork=f"{episode.id}.png" if artwork else None,
        file=f"{episode.id}.mp3",
        mime="audio/mpeg",
        size_bytes=5,
        duration_s=1.0,
    )
    got = db.get_episode(episode.id)
    assert got is not None
    return got


def files(storage: Storage, slug: str) -> list[str]:
    folder = storage.feed_dir(slug)
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


def slow_requeue(db: Database, monkeypatch) -> None:
    """Make requeue take long enough that two threads would overlap if nothing serialised them."""
    real = db.requeue

    def slow(id: str) -> None:
        time.sleep(0.05)
        real(id)

    monkeypatch.setattr(db, "requeue", slow)


def in_threads(count: int, fn):
    """Run fn() in `count` threads that start together; returns [result or exception, ...]."""
    barrier = threading.Barrier(count)

    def run():
        barrier.wait()
        try:
            return fn()
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(count) as pool:
        return [f.result() for f in [pool.submit(run) for _ in range(count)]]


# ------------------------------------------------------------------ adding episodes


def test_no_feeds(svc, wakes):
    with pytest.raises(Invalid, match="No feeds yet") as exc:
        svc.add_episode("https://x.example/a.mp3")
    assert str(exc.value) == "No feeds yet: create one in the podcast admin UI first."
    assert wakes == []


def test_default_and_named_feed(svc, wakes):
    first = svc.create_feed("Talks")
    svc.create_feed("B")
    a = svc.add_episode(URL)
    assert (a.feed_slug, a.feed_id, a.status, a.source_kind, a.source_ref) == (
        "talks",
        first.id,
        "queued",
        "url",
        URL,
    )
    assert len(wakes) == 1
    b = svc.add_episode(URL, feed="b")
    assert (b.feed_slug, b.status) == ("b", "queued") and b.id != a.id
    assert len(wakes) == 2


def test_a_moved_default_gets_the_unnamed_adds(svc):
    svc.create_feed("Talks")
    svc.create_feed("B")
    svc.set_default("b")
    assert svc.add_episode(URL).feed_slug == "b"


def test_blank_feed_means_the_default(svc):
    svc.create_feed("Talks")
    assert svc.add_episode(URL, feed="  ").feed_slug == "talks"


def test_unknown_feed(svc, wakes):
    svc.create_feed("Talks")
    with pytest.raises(NotFound, match="Unknown feed 'nope'") as exc:
        svc.add_episode("https://x/a.mp3", feed="nope")
    assert str(exc.value) == "Unknown feed 'nope'. Call list_feeds for the slugs."
    assert svc.list_episodes() == [] and wakes == []


def test_dedupe_and_requeue(svc, db, wakes):
    svc.create_feed("Talks")
    first = svc.add_episode(URL)
    again = svc.add_episode(URL)
    assert again.id == first.id and again.status == "queued"
    assert len(wakes) == 1  # the second call changed nothing, so it did not wake the worker
    db.set_status(first.id, "failed", "timed out")
    retried = svc.add_episode(URL)
    assert retried.id == first.id and (retried.status, retried.error) == ("queued", None)
    assert db.get_episode(first.id).status == "queued"
    assert len(wakes) == 2
    assert len(svc.list_episodes()) == 1


@pytest.mark.parametrize("status", ["queued", "downloading", "converting", "ready"])
def test_readding_an_episode_that_is_not_failed_changes_nothing(svc, db, storage, wakes, status):
    svc.create_feed("Talks")
    first = svc.add_episode(URL)
    if status == "ready":
        publish(db, storage, first)
    else:
        db.set_status(first.id, status)
    wakes.clear()
    again = svc.add_episode(URL)
    assert again == db.get_episode(first.id) and again.status == status
    assert wakes == []


def test_readding_keeps_the_existing_title(svc, db):
    svc.create_feed("Talks")
    first = svc.add_episode(URL, title="First", description="One")
    db.set_status(first.id, "failed", "timed out")
    again = svc.add_episode(URL, title="Second", description="Two")
    assert (again.title, again.description) == ("First", "One")


def test_the_same_link_in_two_feeds_is_two_episodes(svc):
    svc.create_feed("Talks")
    svc.create_feed("B")
    assert svc.add_episode(URL).id != svc.add_episode(URL, feed="b").id


def test_equivalent_links_are_one_episode(svc):
    svc.create_feed("Talks")
    a = svc.add_episode(URL)
    b = svc.add_episode("  https://X.example/a.mp3  ")
    assert a.id == b.id and b.source_ref == URL and len(svc.list_episodes()) == 1


async def test_concurrent_add_same_url(svc, wakes):
    svc.create_feed("Talks")
    a, b = await asyncio.gather(
        *(asyncio.to_thread(svc.add_episode, "https://x.example/a.mp3") for _ in range(2))
    )
    assert a.id == b.id and len(svc.list_episodes()) == 1
    assert len(wakes) == 1  # one call created it; the other found it


def test_concurrent_readd_of_a_failed_episode_wakes_once(svc, db, wakes, monkeypatch):
    svc.create_feed("Talks")
    first = svc.add_episode(URL)
    db.set_status(first.id, "failed", "timed out")
    wakes.clear()
    slow_requeue(db, monkeypatch)
    results = in_threads(2, lambda: svc.add_episode(URL))
    assert all(isinstance(r, Episode) and r.id == first.id for r in results)
    assert db.get_episode(first.id).status == "queued"
    assert len(wakes) == 1


@pytest.mark.parametrize(
    "url",
    ["ftp://x.example/a.mp3", "not a url", "", "file:///etc/passwd", "https:///a.mp3"],
)
def test_bad_url(svc, db, wakes, url):
    svc.create_feed("Talks")
    with pytest.raises(Invalid, match="Only http and https"):
        svc.add_episode(url)
    assert svc.list_episodes() == [] and wakes == []


def test_url_with_a_password_is_refused(svc):
    svc.create_feed("Talks")
    with pytest.raises(Invalid, match="username or password"):
        svc.add_episode("https://user:pw@x.example/a.mp3")


def test_bad_url_is_reported_before_the_feed(svc):
    with pytest.raises(Invalid, match="Only http and https"):
        svc.add_episode("ftp://x.example/a.mp3", feed="nope")


def test_episode_text_is_stripped_and_blank_means_none(svc):
    svc.create_feed("Talks")
    a = svc.add_episode(URL, title="  A talk \n", description=" About things ")
    assert (a.title, a.description) == ("A talk", "About things")
    b = svc.add_episode("https://x.example/b.mp3", title="   ", description="")
    assert (b.title, b.description) == (None, None)
    c = svc.add_episode("https://x.example/c.mp3")
    assert (c.title, c.description) == (None, None)


def test_episode_text_is_cut_to_300_and_4000_characters(svc):
    svc.create_feed("Talks")
    long_title, long_text = "t" * 400, "d" * 5000
    a = svc.add_episode(URL, title=long_title, description=long_text)
    assert (a.title, a.description) == ("t" * 300, "d" * 4000)
    # Exactly the limit is kept, and the cut is followed by a strip: no space at the end.
    b = svc.add_episode("https://x.example/b.mp3", title="t" * 300, description="d" * 4000)
    assert (b.title, b.description) == ("t" * 300, "d" * 4000)
    c = svc.add_episode(
        "https://x.example/c.mp3", title=" " + "t" * 299 + " u ", description="d" * 3999 + " e"
    )
    assert (c.title, c.description) == ("t" * 299, "d" * 3999)
    # The same when the text is edited, and what is stored is the cut text.
    edited = svc.update_episode(a.id, " " + "u" * 301, " " + "e" * 4001)
    assert (edited.title, edited.description) == ("u" * 300, "e" * 4000)
    assert svc.get_episode(a.id) == edited


def test_lone_surrogates_are_dropped_from_text(svc):
    # JSON can carry one (\ud800), and SQLite cannot store it: it used to be an internal error.
    feed = svc.create_feed(" Ta\ud800lks\udfff ", description="\udc80 About")
    assert (feed.title, feed.description) == ("Talks", "About")
    a = svc.add_episode(URL, title="Ti\ud800tle", description="\udc80Text\udbff")
    assert (a.title, a.description) == ("Title", "Text")
    b = svc.add_episode("https://x.example/b.mp3", title="\ud800\udc00", description="\udfff")
    assert (b.title, b.description) == (None, None)  # nothing but surrogates is blank
    edited = svc.update_episode(a.id, "\ud800 New", "N\udc80ew")
    assert (edited.title, edited.description) == ("New", "New")
    assert svc.update_episode(a.id, "\udc80", "\udc80").title is None
    with pytest.raises(Invalid, match="needs a title"):
        svc.create_feed("\ud800")


def test_the_feed_is_deleted_while_the_episode_is_added(svc, db, wakes, monkeypatch):
    svc.create_feed("Talks")
    real = db.add_or_get_episode

    def late(*args, **kwargs):
        db.delete_feed("talks")
        return real(*args, **kwargs)

    monkeypatch.setattr(db, "add_or_get_episode", late)
    with pytest.raises(NotFound) as exc:
        svc.add_episode(URL, feed="talks")
    assert str(exc.value) == "Unknown feed 'talks'. Call list_feeds for the slugs."
    assert wakes == []


# --------------------------------------------------------------------- feeds


def test_create_feed(svc):
    f = svc.create_feed("  Talks & More!  ", description="  Some talks ", retention_days=7)
    assert (f.slug, f.title, f.description, f.retention_days) == (
        "talks-more",
        "Talks & More!",
        "Some talks",
        7,
    )
    assert f.is_default
    g = svc.create_feed("Second", slug="my-second")
    assert (g.slug, g.description, g.retention_days, g.is_default) == (
        "my-second",
        None,
        None,
        False,
    )
    assert svc.get_feed("my-second") == g


def test_create_feed_blank_slug_and_description(svc):
    f = svc.create_feed("Talks", slug="  ", description="   ")
    assert (f.slug, f.description) == ("talks", None)


def test_create_feed_validation(svc):
    with pytest.raises(Invalid) as exc:
        svc.create_feed("")
    assert str(exc.value) == "A feed needs a title."
    with pytest.raises(Invalid, match="A feed needs a title."):
        svc.create_feed("   ")
    for slug in ("Bad Slug", "UPPER", "-a", "a-", "a_b", "a" * 41, "ä"):
        with pytest.raises(Invalid) as exc:
            svc.create_feed("Talks", slug=slug)
        assert str(exc.value) == "Slugs are 1 to 40 lowercase letters, digits and dashes."
    assert svc.list_feeds() == []  # nothing was created by any of those


@pytest.mark.parametrize("days", [0, -1, -(10**30), 1.5, "7", True, False])
def test_retention_below_one_or_not_a_whole_number(svc, days):
    with pytest.raises(Invalid) as exc:
        svc.create_feed("Talks", retention_days=days)
    assert str(exc.value) == "Retention must be at least 1 day."
    assert svc.list_feeds() == []
    svc.create_feed("Talks")
    with pytest.raises(Invalid) as exc:
        svc.update_feed("talks", title="Talks", description=None, retention_days=days)
    assert str(exc.value) == "Retention must be at least 1 day."
    assert svc.get_feed("talks").retention_days is None


@pytest.mark.parametrize("days", [3651, 10**6, 10**30])
def test_retention_over_ten_years(svc, days):
    # The retention sweep subtracts it from the clock, which overflows far above this.
    with pytest.raises(Invalid) as exc:
        svc.create_feed("Talks", retention_days=days)
    assert str(exc.value) == "Retention can be at most 3650 days."
    assert svc.list_feeds() == []
    svc.create_feed("Talks")
    with pytest.raises(Invalid) as exc:
        svc.update_feed("talks", title="Talks", description=None, retention_days=days)
    assert str(exc.value) == "Retention can be at most 3650 days."
    assert svc.get_feed("talks").retention_days is None


def test_retention_limits_are_accepted(svc):
    assert svc.create_feed("Short", retention_days=1).retention_days == 1
    assert svc.create_feed("Long", retention_days=3650).retention_days == 3650
    f = svc.update_feed("short", title="Short", description=None, retention_days=3650)
    assert f.retention_days == 3650


def test_the_longest_retention_does_not_break_the_sweep(svc, db):
    # What the bound is for: expired() subtracts the retention from the clock.
    svc.create_feed("Talks", retention_days=3650)
    db.set_status(svc.add_episode(URL).id, "failed", "timed out")  # only failed/ready are checked
    assert db.expired(30) == []


def test_create_feed_slug_from_title_and_taken(svc):
    assert svc.create_feed("Hello World").slug == "hello-world"
    assert svc.create_feed("Åäö").slug == "aao"
    assert svc.create_feed("???").slug == "feed"
    with pytest.raises(Invalid, match="The slug 'hello-world' is taken."):
        svc.create_feed("Hello  World")
    with pytest.raises(Invalid, match="The slug 'hello-world' is taken."):
        svc.create_feed("Other", slug="hello-world")


def test_create_feed_longest_slug(svc):
    assert svc.create_feed("Talks", slug="a" * 40).slug == "a" * 40


def test_get_feed(svc):
    f = svc.create_feed("Talks")
    assert svc.get_feed("talks") == f
    with pytest.raises(NotFound) as exc:
        svc.get_feed("nope")
    assert str(exc.value) == "Unknown feed 'nope'. Call list_feeds for the slugs."


def test_list_feeds(svc, db, storage):
    a = svc.create_feed("Talks")
    b = svc.create_feed("Short", retention_days=3)
    ready = publish(db, storage, svc.add_episode(URL))
    svc.add_episode("https://x.example/b.mp3")  # queued: not counted
    svc.add_episode("https://x.example/c.mp3", feed="short")
    summaries = svc.list_feeds()
    assert all(isinstance(s, FeedSummary) for s in summaries)
    assert [(s.feed.id, s.feed.slug, s.episode_count, s.retention_days) for s in summaries] == [
        (a.id, "talks", 1, 30),  # the one ready episode is counted; the queued one is not
        (b.id, "short", 0, 3),  # its own retention, not the default
    ]
    assert summaries[0].feed.is_default and not summaries[1].feed.is_default
    assert ready.status == "ready"
    assert dataclasses.is_dataclass(FeedSummary)
    with pytest.raises(dataclasses.FrozenInstanceError):
        summaries[0].episode_count = 5  # type: ignore[misc]


def test_list_feeds_counts_a_feed_without_episodes(svc):
    svc.create_feed("Talks")
    [summary] = svc.list_feeds()
    assert summary.episode_count == 0


def test_update_feed(svc, wakes):
    svc.create_feed("Talks", description="Old", retention_days=7)
    f = svc.update_feed("talks", title="  New title ", description="  ", retention_days=None)
    assert (f.slug, f.title, f.description, f.retention_days) == ("talks", "New title", None, None)
    assert svc.get_feed("talks") == f
    g = svc.update_feed("talks", title="Newer", description=" About ", retention_days=14)
    assert (g.title, g.description, g.retention_days) == ("Newer", "About", 14)
    assert wakes == []


def test_update_feed_validation(svc):
    before = svc.create_feed("Talks", description="Keep", retention_days=7)
    with pytest.raises(Invalid, match="A feed needs a title."):
        svc.update_feed("talks", title=" ", description="x", retention_days=7)
    with pytest.raises(Invalid, match="Retention must be at least 1 day."):
        svc.update_feed("talks", title="T", description="x", retention_days=0)
    assert svc.get_feed("talks") == before


def test_set_default(svc):
    svc.create_feed("Talks")
    svc.create_feed("B")
    svc.set_default("b")
    assert [(s.feed.slug, s.feed.is_default) for s in svc.list_feeds()] == [
        ("talks", False),
        ("b", True),
    ]


def test_delete_feed_removes_dir(svc, db, storage):
    svc.create_feed("Talks")
    svc.create_feed("B")
    ep = publish(db, storage, svc.add_episode(URL), artwork=True)
    other = publish(db, storage, svc.add_episode(URL, feed="b"))
    svc.set_feed_artwork("talks", PNG)
    (storage.feed_dir("talks") / "stray.txt").write_bytes(b"x")
    svc.delete_feed("talks")
    assert not storage.feed_dir("talks").exists()
    assert db.get_episode(ep.id) is None and db.get_feed("talks") is None
    assert db.get_episode(other.id) is not None and files(storage, "b") == [f"{other.id}.mp3"]
    assert storage.tmp.is_dir()
    # The default moved to the feed that is left.
    assert svc.get_feed("b").is_default


def test_artwork_and_deleting_the_feed_do_not_race(svc, db, storage, monkeypatch):
    # The artwork call has found the feed and is about to write its cover when the delete
    # comes in. Without the lock the delete removes the feed and its folder first, and the
    # cover is then written into a folder nobody will ever clean up.
    svc.create_feed("Talks")
    real = db.get_feed
    found = threading.Event()

    def slow(slug):
        feed = real(slug)
        found.set()  # the artwork call holds its lock and has the feed
        time.sleep(0.15)
        return feed

    monkeypatch.setattr(db, "get_feed", slow)
    with ThreadPoolExecutor(2) as pool:
        artwork = pool.submit(svc.set_feed_artwork, "talks", PNG)
        assert found.wait(10)
        deleted = pool.submit(svc.delete_feed, "talks")
        artwork.result()
        deleted.result()
    assert db.get_feed("talks") is None
    assert not storage.feed_dir("talks").exists()


def test_delete_feed_without_a_folder(svc):
    svc.create_feed("Talks")
    svc.delete_feed("talks")
    assert svc.list_feeds() == []


def test_delete_feed_deletes_the_row_before_the_files(svc, db, storage, monkeypatch):
    svc.create_feed("Talks")
    svc.add_episode(URL)
    storage.feed_dir("talks").mkdir()
    seen = []
    real = storage.delete_feed

    def spy(slug):
        seen.append((db.get_feed(slug), db.list_episodes()))
        real(slug)

    monkeypatch.setattr(storage, "delete_feed", spy)
    svc.delete_feed("talks")
    assert seen == [(None, [])]


def test_delete_feed_that_fails_on_disk_has_still_deleted_the_row(svc, storage, monkeypatch):
    svc.create_feed("Talks")

    def boom(slug):
        raise OSError("disk on fire")

    monkeypatch.setattr(storage, "delete_feed", boom)
    with pytest.raises(OSError):
        svc.delete_feed("talks")
    assert svc.list_feeds() == []


# ------------------------------------------------------------------- artwork


def test_artwork(svc, storage):
    svc.create_feed("Talks")
    assert svc.set_feed_artwork("talks", PNG).artwork == "cover.png"
    assert (storage.feed_dir("talks") / "cover.png").read_bytes() == PNG
    with pytest.raises(Invalid, match="PNG or JPEG"):
        svc.set_feed_artwork("talks", b"GIF89a")
    assert svc.get_feed("talks").artwork == "cover.png"  # a refused upload changes nothing


def test_artwork_message_is_exact(svc):
    svc.create_feed("Talks")
    with pytest.raises(Invalid) as exc:
        svc.set_feed_artwork("talks", b"GIF89a")
    assert str(exc.value) == "Artwork must be a PNG or JPEG under 5 MB."


def test_artwork_jpeg_replaces_png_and_back(svc, storage):
    svc.create_feed("Talks")
    svc.set_feed_artwork("talks", PNG)
    f = svc.set_feed_artwork("talks", JPEG)
    assert f.artwork == "cover.jpg" and files(storage, "talks") == ["cover.jpg"]
    assert (storage.feed_dir("talks") / "cover.jpg").read_bytes() == JPEG
    g = svc.set_feed_artwork("talks", PNG)
    assert g.artwork == "cover.png" and files(storage, "talks") == ["cover.png"]
    h = svc.set_feed_artwork("talks", PNG + b"1")  # same type: replaced in place
    assert h.artwork == "cover.png"
    assert (storage.feed_dir("talks") / "cover.png").read_bytes() == PNG + b"1"
    assert files(storage, "talks") == ["cover.png"]


def test_artwork_goes_next_to_the_episodes(svc, db, storage):
    svc.create_feed("Talks")
    ep = publish(db, storage, svc.add_episode(URL))
    svc.set_feed_artwork("talks", PNG)
    assert files(storage, "talks") == sorted(["cover.png", f"{ep.id}.mp3"])


def test_artwork_size_limit(svc, storage):
    svc.create_feed("Talks")
    exact = PNG + b"0" * (FIVE_MIB - len(PNG))
    assert len(exact) == FIVE_MIB
    assert svc.set_feed_artwork("talks", exact).artwork == "cover.png"
    with pytest.raises(Invalid, match="under 5 MB"):
        svc.set_feed_artwork("talks", exact + b"0")
    with pytest.raises(Invalid, match="under 5 MB"):
        svc.set_feed_artwork("talks", JPEG + b"0" * FIVE_MIB)
    assert (storage.feed_dir("talks") / "cover.png").stat().st_size == FIVE_MIB
    assert files(storage, "talks") == ["cover.png"]


@pytest.mark.parametrize("data", [b"", b"\x89PNG", b"\xff\xd8", b"RIFF....WEBP", b"<svg/>"])
def test_artwork_must_start_like_a_png_or_jpeg(svc, data):
    svc.create_feed("Talks")
    with pytest.raises(Invalid, match="PNG or JPEG"):
        svc.set_feed_artwork("talks", data)


def test_artwork_of_an_unknown_feed_leaves_nothing_behind(svc, storage):
    with pytest.raises(NotFound) as exc:
        svc.set_feed_artwork("nope", PNG)
    assert str(exc.value) == "Unknown feed 'nope'. Call list_feeds for the slugs."
    assert not storage.feed_dir("nope").exists()
    with pytest.raises(NotFound):
        svc.set_feed_artwork("../media", PNG)


def test_artwork_write_that_fails_leaves_the_old_cover(svc, storage, monkeypatch):
    svc.create_feed("Talks")
    svc.set_feed_artwork("talks", PNG)

    def boom(src, dst):
        raise OSError("disk on fire")

    monkeypatch.setattr("podcast_feed.service.os.replace", boom)
    with pytest.raises(OSError):
        svc.set_feed_artwork("talks", JPEG)
    assert svc.get_feed("talks").artwork == "cover.png"
    assert files(storage, "talks") == ["cover.png"]  # and no temporary file


def test_artwork_from_several_threads_ends_consistent(svc, storage):
    svc.create_feed("Talks")
    calls = [PNG, JPEG] * 10

    def one(data):
        svc.set_feed_artwork("talks", data)

    with ThreadPoolExecutor(4) as pool:
        list(pool.map(one, calls))
    artwork = svc.get_feed("talks").artwork
    assert artwork in ("cover.png", "cover.jpg")
    assert files(storage, "talks") == [artwork]


# ------------------------------------------------------------------ episodes


def test_get_episode(svc):
    svc.create_feed("Talks")
    ep = svc.add_episode(URL)
    assert svc.get_episode(ep.id) == ep
    with pytest.raises(NotFound) as exc:
        svc.get_episode("nope")
    assert str(exc.value) == "No episode 'nope'."


def test_list_episodes(svc, db):
    svc.create_feed("Talks")
    svc.create_feed("B")
    a = svc.add_episode("https://x.example/1.mp3")
    b = svc.add_episode("https://x.example/2.mp3")
    c = svc.add_episode("https://x.example/3.mp3", feed="b")
    db.set_status(b.id, "failed", "timed out")
    assert [e.id for e in svc.list_episodes()] == [c.id, b.id, a.id]  # newest first
    assert [e.id for e in svc.list_episodes(feed="talks")] == [b.id, a.id]
    assert [e.id for e in svc.list_episodes(feed="b")] == [c.id]
    assert [e.id for e in svc.list_episodes(status="failed")] == [b.id]
    assert [e.id for e in svc.list_episodes(feed="talks", status="queued")] == [a.id]
    assert [e.id for e in svc.list_episodes(limit=2)] == [c.id, b.id]
    assert [e.id for e in svc.list_episodes(feed="  ")] == [c.id, b.id, a.id]
    assert [e.id for e in svc.list_episodes(status="")] == [c.id, b.id, a.id]  # blank: any
    assert [e.id for e in svc.list_episodes(status="  ", feed="talks")] == [b.id, a.id]
    assert [e.id for e in svc.list_episodes(status=" failed ")] == [b.id]


def test_list_episodes_default_limit_is_50_and_none_means_all(svc):
    svc.create_feed("Talks")
    for n in range(52):
        svc.add_episode(f"https://x.example/{n}.mp3")
    assert len(svc.list_episodes()) == 50
    assert len(svc.list_episodes(limit=None)) == 52


def test_list_episodes_of_an_unknown_feed(svc):
    with pytest.raises(NotFound) as exc:
        svc.list_episodes(feed="nope")
    assert str(exc.value) == "Unknown feed 'nope'. Call list_feeds for the slugs."


def test_list_episodes_refuses_what_would_silently_match_nothing_or_everything(svc):
    svc.create_feed("Talks")
    svc.add_episode(URL)
    with pytest.raises(Invalid, match="Unknown status 'done'"):
        svc.list_episodes(status="done")
    for limit in (0, -1):  # SQLite reads a negative LIMIT as "no limit"
        with pytest.raises(Invalid, match="at least 1"):
            svc.list_episodes(limit=limit)


def test_update_episode(svc, db, storage, wakes):
    svc.create_feed("Talks")
    ep = publish(db, storage, svc.add_episode(URL))
    wakes.clear()
    changed = svc.update_episode(ep.id, "  New title ", "  New text \n")
    assert (changed.id, changed.title, changed.description) == (ep.id, "New title", "New text")
    assert svc.get_episode(ep.id) == changed and changed.status == "ready"
    cleared = svc.update_episode(ep.id, " ", "")
    assert (cleared.title, cleared.description) == (None, None)
    assert wakes == []


def test_update_unknown_episode(svc):
    with pytest.raises(NotFound) as exc:
        svc.update_episode("nope", "T", "D")
    assert str(exc.value) == "No episode 'nope'."


def test_retry_only_failed(svc, db, storage, wakes):
    svc.create_feed("Talks")
    ep = svc.add_episode(URL)
    wakes.clear()
    for status in ("queued", "downloading", "converting", "ready"):
        if status == "ready":
            publish(db, storage, ep)
        else:
            db.set_status(ep.id, status)
        with pytest.raises(Invalid) as exc:
            svc.retry_episode(ep.id)
        assert str(exc.value) == "Only failed episodes can be retried."
        assert db.get_episode(ep.id).status == status
    assert wakes == []
    db.set_status(ep.id, "failed", "too large")
    again = svc.retry_episode(ep.id)
    assert (again.id, again.status, again.error) == (ep.id, "queued", None)
    assert db.get_episode(ep.id) == again
    assert len(wakes) == 1


def test_retry_unknown_episode(svc, wakes):
    with pytest.raises(NotFound) as exc:
        svc.retry_episode("nope")
    assert str(exc.value) == "No episode 'nope'." and wakes == []


def test_concurrent_retry_queues_and_wakes_once(svc, db, wakes, monkeypatch):
    svc.create_feed("Talks")
    ep = svc.add_episode(URL)
    db.set_status(ep.id, "failed", "timed out")
    wakes.clear()
    slow_requeue(db, monkeypatch)
    results = in_threads(2, lambda: svc.retry_episode(ep.id))
    queued = [r for r in results if isinstance(r, Episode)]
    refused = [r for r in results if isinstance(r, Invalid)]
    assert len(queued) == 1 and len(refused) == 1
    assert str(refused[0]) == "Only failed episodes can be retried."
    assert len(wakes) == 1


def test_remove_episode(svc, db, storage):
    svc.create_feed("Talks")
    keep = publish(db, storage, svc.add_episode("https://x.example/keep.mp3"))
    ep = publish(db, storage, svc.add_episode(URL), artwork=True)
    svc.set_feed_artwork("talks", PNG)
    assert files(storage, "talks") == sorted(
        ["cover.png", f"{keep.id}.mp3", f"{ep.id}.mp3", f"{ep.id}.png"]
    )
    svc.remove_episode(ep.id)
    assert db.get_episode(ep.id) is None
    assert files(storage, "talks") == sorted(["cover.png", f"{keep.id}.mp3"])
    assert svc.get_episode(keep.id).status == "ready"
    with pytest.raises(NotFound) as exc:
        svc.remove_episode(ep.id)
    assert str(exc.value) == f"No episode '{ep.id}'."


def test_remove_episode_that_is_not_ready(svc, db):
    svc.create_feed("Talks")
    ep = svc.add_episode(URL)
    svc.remove_episode(ep.id)
    assert svc.list_episodes() == []


def test_remove_episode_takes_files_the_row_does_not_name(svc, storage):
    # A worker's mark_ready can land between the read and the delete: the row we read has no
    # file names, but the files are in the folder by the time we delete.
    svc.create_feed("Talks")
    ep = svc.add_episode(URL)
    other = svc.add_episode("https://x.example/other.mp3")
    folder = storage.feed_dir("talks")
    folder.mkdir()
    for name in (f"{ep.id}.m4a", f"{ep.id}.jpg", f"{other.id}.m4a", "cover.png"):
        (folder / name).write_bytes(b"x")
    svc.remove_episode(ep.id)
    assert files(storage, "talks") == sorted([f"{other.id}.m4a", "cover.png"])


def test_remove_episode_deletes_the_row_before_the_files(svc, db, storage, monkeypatch):
    svc.create_feed("Talks")
    ep = svc.add_episode(URL)
    seen = []
    for name in ("delete_episode_files", "delete_episode_files_by_id"):
        real = getattr(storage, name)

        def spy(*args, _real=real, _name=name):
            seen.append((_name, db.get_episode(ep.id)))
            _real(*args)

        monkeypatch.setattr(storage, name, spy)
    svc.remove_episode(ep.id)
    assert seen and all(row is None for _, row in seen)
    assert {name for name, _ in seen} >= {"delete_episode_files_by_id"}


# ------------------------------------------------------------ the rest of the contract


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.get_feed("nope"),
        lambda s: s.update_feed("nope", title="T", description=None, retention_days=None),
        lambda s: s.set_feed_artwork("nope", PNG),
        lambda s: s.set_default("nope"),
        lambda s: s.delete_feed("nope"),
        lambda s: s.feed_url("nope"),
        lambda s: s.add_episode(URL, feed="nope"),
        lambda s: s.list_episodes(feed="nope"),
    ],
)
def test_unknown_feed_message_is_ours_everywhere(svc, call):
    svc.create_feed("Talks")
    with pytest.raises(NotFound) as exc:
        call(svc)
    assert str(exc.value) == "Unknown feed 'nope'. Call list_feeds for the slugs."
    assert exc.value.__suppress_context__ or exc.value.__context__ is None


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.get_episode("nope"),
        lambda s: s.update_episode("nope", "T", "D"),
        lambda s: s.retry_episode("nope"),
        lambda s: s.remove_episode("nope"),
    ],
)
def test_unknown_episode_message_is_ours_everywhere(svc, call):
    svc.create_feed("Talks")
    with pytest.raises(NotFound) as exc:
        call(svc)
    assert str(exc.value) == "No episode 'nope'."
    assert exc.value.__suppress_context__ or exc.value.__context__ is None


def test_feed_url(svc):
    svc.create_feed("Talks")
    assert svc.feed_url("talks") == "https://podcast-api.example.com/s3cr3t/feeds/talks.xml"


def test_the_secret_is_only_in_the_feed_url(svc):
    svc.create_feed("Talks")
    ep = svc.add_episode(URL)
    assert "s3cr3t" not in repr(svc)
    texts = [repr(svc.list_feeds()), repr(svc.get_feed("talks")), repr(ep)]
    texts += [repr(svc.list_episodes()), repr(svc.get_episode(ep.id))]
    assert not any("s3cr3t" in text for text in texts)
    for call in (
        lambda: svc.add_episode("ftp://x"),
        lambda: svc.add_episode(URL, feed="nope"),
        lambda: svc.create_feed("", slug="bad slug"),
        lambda: svc.get_episode("nope"),
    ):
        with pytest.raises(Exception) as exc:
            call()
        assert "s3cr3t" not in str(exc.value)


def test_wake_is_only_for_queued_episodes(svc, db, wakes):
    svc.create_feed("Talks")
    svc.create_feed("B")
    svc.update_feed("talks", title="T", description=None, retention_days=2)
    svc.set_default("b")
    svc.set_feed_artwork("b", PNG)
    svc.list_feeds()
    svc.list_episodes()
    assert wakes == []
    ep = svc.add_episode(URL)
    svc.get_episode(ep.id)
    svc.update_episode(ep.id, "T", "D")
    assert len(wakes) == 1
    svc.remove_episode(ep.id)
    svc.delete_feed("talks")
    assert len(wakes) == 1


# ------------------------------------------------------ what the public routes serve


def guids(body: bytes) -> list[str | None]:
    return [g.text for g in ET.fromstring(body).iterfind("channel/item/guid")]


def test_feed_document(svc, db, storage):
    svc.create_feed("Talks", description="About")
    ready = publish(db, storage, svc.add_episode(URL))
    svc.add_episode("https://x.example/b.mp3")  # still queued: not in the feed
    body, changed = svc.feed_document("talks")
    assert guids(body) == [ready.id]
    assert changed == svc.get_feed("talks").changed_at
    xml = ET.fromstring(body)
    assert xml.findtext("channel/description") == "About"
    assert xml.findtext("channel/link") == "https://podcast-api.example.com/s3cr3t/feeds/talks.xml"
    enclosure = xml.find("channel/item/enclosure")
    assert enclosure is not None
    assert (
        enclosure.get("url") == f"https://podcast-api.example.com/s3cr3t/media/talks/{ready.id}.mp3"
    )


def test_feed_document_of_a_feed_without_episodes(svc):
    svc.create_feed("Talks")
    body, _ = svc.feed_document("talks")
    assert guids(body) == []


def test_feed_document_lists_every_ready_episode(svc, db, storage):
    svc.create_feed("Talks")
    made = [publish(db, storage, svc.add_episode(f"https://x.example/{n}.mp3")) for n in range(55)]
    body, _ = svc.feed_document("talks")
    assert sorted(guids(body)) == sorted(e.id for e in made)  # not just the first 50


def test_feed_document_skips_an_episode_whose_file_is_gone(svc, db, storage):
    svc.create_feed("Talks")
    kept = publish(db, storage, svc.add_episode(URL))
    gone = publish(db, storage, svc.add_episode("https://x.example/b.mp3"))
    (storage.feed_dir("talks") / f"{gone.id}.mp3").unlink()
    body, _ = svc.feed_document("talks")
    assert guids(body) == [kept.id]


def test_feed_document_only_has_the_episodes_of_its_feed(svc, db, storage):
    svc.create_feed("Talks")
    svc.create_feed("B")
    mine = publish(db, storage, svc.add_episode(URL, feed="talks"))
    publish(db, storage, svc.add_episode(URL, feed="b"))
    assert guids(svc.feed_document("talks")[0]) == [mine.id]


def test_feed_document_follows_the_feed(svc, db, storage):
    svc.create_feed("Talks")
    first, changed = svc.feed_document("talks")
    publish(db, storage, svc.add_episode(URL))
    second, changed_again = svc.feed_document("talks")
    assert first != second and changed_again >= changed
    assert changed_again == svc.get_feed("talks").changed_at


def test_feed_document_of_an_unknown_feed(svc):
    with pytest.raises(NotFound) as exc:
        svc.feed_document("nope")
    assert str(exc.value) == "Unknown feed 'nope'. Call list_feeds for the slugs."


def test_media_file_allowlist(svc, db, storage):
    svc.create_feed("Talks")
    svc.set_feed_artwork("talks", PNG)
    ep = publish(db, storage, svc.add_episode(URL), artwork=True)
    folder = storage.feed_dir("talks")
    # A ready episode's audio and artwork, and the cover, resolve:
    assert svc.media_file("talks", f"{ep.id}.mp3") == (folder / f"{ep.id}.mp3", "audio/mpeg")
    assert svc.media_file("talks", f"{ep.id}.png") == (folder / f"{ep.id}.png", "image/png")
    assert svc.media_file("talks", "cover.png") == (folder / "cover.png", "image/png")

    # Everything else does not, whether or not it is in the folder:
    queued = svc.add_episode("https://x.example/q.mp3")
    for stray in (".cover.upload", "notes.txt", f"{queued.id}.mp3", f"{ep.id}.jpg"):
        (folder / stray).write_bytes(b"x")
    other = svc.create_feed("B")
    foreign = publish(db, storage, svc.add_episode(URL, feed="b"))
    (folder / f"{foreign.id}.mp3").write_bytes(b"x")  # another feed's id, in this folder
    for name in (
        "nope.mp3",
        "cover.jpg",
        ".cover.upload",
        "notes.txt",
        f"{queued.id}.mp3",  # not ready
        f"{ep.id}.jpg",  # not what the row names
        f"{ep.id}.mp3.bak",
        f"{ep.id}.",
        f"{foreign.id}.mp3",  # belongs to feed b
        f"../{other.slug}/{foreign.id}.mp3",
        "../talks/cover.png",
        "..",
        ".",
        "",
        "/etc/passwd",
        "a\x00b",
    ):
        with pytest.raises(NotFound):
            svc.media_file("talks", name)
    with pytest.raises(NotFound):
        svc.media_file("nope", f"{ep.id}.mp3")
    with pytest.raises(NotFound):
        svc.media_file("../media/talks", f"{ep.id}.mp3")
    assert svc.media_file("b", f"{foreign.id}.mp3")[1] == "audio/mpeg"


def test_media_file_types(svc, db, storage):
    svc.create_feed("Talks")
    svc.set_feed_artwork("talks", JPEG)
    ep = svc.add_episode(URL)
    folder = storage.feed_dir("talks")
    (folder / f"{ep.id}.m4a").write_bytes(b"audio")
    (folder / f"{ep.id}.jpg").write_bytes(b"art")
    db.mark_ready(
        ep.id,
        title="T",
        description=None,
        artwork=f"{ep.id}.jpg",
        file=f"{ep.id}.m4a",
        mime="audio/mp4",
        size_bytes=5,
        duration_s=None,
    )
    assert svc.media_file("talks", f"{ep.id}.m4a")[1] == "audio/mp4"
    assert svc.media_file("talks", f"{ep.id}.jpg")[1] == "image/jpeg"
    assert svc.media_file("talks", "cover.jpg")[1] == "image/jpeg"
    with pytest.raises(NotFound):
        svc.media_file("talks", "cover.png")  # the cover is a JPEG now


def test_media_file_of_a_file_that_is_gone(svc, db, storage):
    svc.create_feed("Talks")
    svc.set_feed_artwork("talks", PNG)
    ep = publish(db, storage, svc.add_episode(URL), artwork=True)
    folder = storage.feed_dir("talks")
    for name in (f"{ep.id}.mp3", f"{ep.id}.png", "cover.png"):
        assert svc.media_file("talks", name)[0] == folder / name
        (folder / name).unlink()
        with pytest.raises(NotFound):
            svc.media_file("talks", name)


def test_the_public_methods_never_leak_the_secret_in_errors(svc):
    svc.create_feed("Talks")
    for call in (lambda: svc.feed_document("nope"), lambda: svc.media_file("talks", "nope.mp3")):
        with pytest.raises(NotFound) as exc:
            call()
        assert "s3cr3t" not in str(exc.value)


# ------------------------------------------------------------------------------- uploads


def receive(svc, body=b"audio", filename="talk.mp3", feed=None, declared=None):
    """What the admin UI does with an upload: start, write the body, finish."""
    pending = svc.start_upload(feed, filename, len(body) if declared is None else declared)
    pending.part.write_bytes(body)
    return pending


def tmp_files(storage) -> list[str]:
    return sorted(p.name for p in storage.tmp.iterdir())


def test_an_upload_is_queued_under_its_own_id(svc, storage, wakes):
    svc.create_feed("Talks")
    pending = receive(svc, filename="C:\\Music\\Talk One.mp3")
    assert tmp_files(storage) == [f"{pending.id}.part"] and wakes == []  # not yet an episode
    e = svc.finish_upload(pending, "  Mine ", "About it")
    assert e.id == pending.id and (e.status, e.source_kind) == ("queued", "upload")
    assert e.source_ref == f"{pending.id}/Talk One.mp3"
    assert (e.title, e.description, e.feed_slug) == ("Mine", "About it", "talks")
    assert tmp_files(storage) == [f"{pending.id}.upload"] and wakes == [None]
    assert storage.upload_ready(pending.id).read_bytes() == b"audio"
    assert svc.get_episode(e.id) == e


def test_the_same_file_uploaded_twice_is_two_episodes(svc, wakes):
    svc.create_feed("Talks")
    a = svc.finish_upload(receive(svc))
    b = svc.finish_upload(receive(svc))
    assert a.id != b.id and len(svc.list_episodes()) == 2 and wakes == [None, None]


def test_an_upload_goes_to_the_named_feed_or_the_default(svc):
    svc.create_feed("Talks")
    svc.create_feed("B")
    assert svc.finish_upload(receive(svc)).feed_slug == "talks"
    assert svc.finish_upload(receive(svc, feed="b")).feed_slug == "b"


def test_an_upload_needs_a_feed(svc, storage):
    with pytest.raises(Invalid, match="No feeds yet"):
        svc.start_upload(None, "a.mp3", 5)
    svc.create_feed("Talks")
    with pytest.raises(NotFound, match="Unknown feed 'nope'"):
        svc.start_upload("nope", "a.mp3", 5)
    assert tmp_files(storage) == []


def test_a_declared_size_over_the_limit_is_refused_before_anything_is_written(svc, storage):
    svc.create_feed("Talks")
    with pytest.raises(TooLarge) as exc:
        svc.start_upload(None, "a.mp3", MAX_UPLOAD_BYTES + 1)
    assert str(exc.value) == "That file is over the 100 MB upload limit. Add it by link instead."
    assert tmp_files(storage) == []
    svc.start_upload(None, "a.mp3", MAX_UPLOAD_BYTES)  # exactly the limit fits
    svc.start_upload(None, "a.mp3", None)  # no Content-Length: counted while it arrives


def test_an_upload_is_refused_when_the_disk_would_fall_below_the_floor(svc, storage, monkeypatch):
    svc.create_feed("Talks")
    monkeypatch.setattr(storage, "free_bytes", lambda: 5 * GIB + 1000 - 1)
    with pytest.raises(Invalid) as exc:
        svc.start_upload(None, "a.mp3", 1000)
    assert (
        str(exc.value)
        == "The server is nearly out of disk space, so it cannot take the upload now."
    )
    monkeypatch.setattr(storage, "free_bytes", lambda: 5 * GIB + 1000)
    svc.start_upload(None, "a.mp3", 1000)


def test_an_empty_upload_is_refused_and_leaves_nothing(svc, storage, wakes):
    svc.create_feed("Talks")
    with pytest.raises(Invalid) as exc:
        svc.finish_upload(receive(svc, body=b""))
    assert str(exc.value) == "No file was sent."
    assert tmp_files(storage) == [] and svc.list_episodes() == [] and wakes == []


def test_an_upload_that_grew_past_the_limit_is_refused_and_leaves_nothing(svc, storage, wakes):
    svc.create_feed("Talks")
    pending = svc.start_upload(None, "a.mp3", None)
    with pending.part.open("wb") as part:
        part.truncate(MAX_UPLOAD_BYTES + 1)  # a sparse file: no 100 MB written
    with pytest.raises(TooLarge):
        svc.finish_upload(pending)
    assert tmp_files(storage) == [] and svc.list_episodes() == [] and wakes == []


def test_an_upload_whose_feed_was_deleted_meanwhile_leaves_nothing(svc, storage, wakes):
    svc.create_feed("Talks")
    svc.create_feed("B")
    pending = receive(svc, feed="b")
    svc.delete_feed("b")
    with pytest.raises(NotFound, match="Unknown feed 'b'"):
        svc.finish_upload(pending)
    assert tmp_files(storage) == [] and wakes == []


def test_an_aborted_upload_leaves_nothing(svc, storage):
    svc.create_feed("Talks")
    svc.abort_upload(receive(svc))
    assert tmp_files(storage) == []
    svc.abort_upload(receive(svc))  # nothing to clean up twice


def test_a_failed_upload_cannot_be_retried(svc, db, storage, wakes):
    svc.create_feed("Talks")
    e = svc.finish_upload(receive(svc))
    wakes.clear()
    with pytest.raises(Invalid, match="Only failed"):  # not failed yet
        svc.retry_episode(e.id)
    db.set_status(e.id, "failed", "upload lost")
    with pytest.raises(Invalid) as exc:
        svc.retry_episode(e.id)
    assert str(exc.value) == "An upload can't be retried; upload the file again."
    assert svc.get_episode(e.id).status == "failed" and wakes == []


def test_removing_an_upload_that_waits_takes_its_file(svc, storage):
    svc.create_feed("Talks")
    e = svc.finish_upload(receive(svc))
    svc.remove_episode(e.id)
    assert tmp_files(storage) == [] and svc.list_episodes() == []


def test_removing_an_episode_leaves_the_files_of_other_uploads(svc, storage):
    svc.create_feed("Talks")
    keep = svc.finish_upload(receive(svc))
    gone = svc.finish_upload(receive(svc))
    svc.remove_episode(gone.id)
    assert tmp_files(storage) == [f"{keep.id}.upload"]
