import re
import sqlite3
import threading
from datetime import UTC, datetime, timedelta, timezone

import pytest

from podcast_feed.db import Database, Episode, Feed, new_episode_id, utcnow
from podcast_feed.errors import Invalid, NotFound

T0 = datetime(2026, 10, 29, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    """A settable clock: an aware UTC datetime with microsecond 0, like utcnow()."""

    def __init__(self, now: datetime = T0):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def set(self, now: datetime) -> None:
        self.now = now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def db(clock):
    database = Database(":memory:", clock=clock)
    yield database
    database.close()


def ready(db, feed_id, ref, **fields):
    """Add an episode and mark it ready at the clock's current time."""
    episode, _ = db.add_or_get_episode(feed_id, "url", ref, None, None)
    args = dict(
        title="T",
        description=None,
        artwork=None,
        file="x.m4a",
        mime="audio/mp4",
        size_bytes=1,
        duration_s=1.0,
    )
    assert db.mark_ready(episode.id, **{**args, **fields})
    return episode


def test_utcnow_whole_seconds():
    now = utcnow()
    assert now.microsecond == 0
    assert now.utcoffset() == timedelta(0)


def test_new_episode_id_format_and_variety():
    ids = {new_episode_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(re.fullmatch("[a-z2-7]{12}", i) for i in ids)


# ---------------------------------------------------------------- feeds


def test_first_feed_is_default(db):
    a = db.create_feed("a", "A")
    b = db.create_feed("b", "B")
    assert a.is_default and not b.is_default and db.default_feed().slug == "a"


def test_no_default_feed_when_there_are_no_feeds(db):
    assert db.default_feed() is None
    assert db.list_feeds() == []


def test_create_feed_returns_the_stored_feed(db, clock):
    f = db.create_feed("talks", "Talks", description="Long ones", retention_days=14)
    assert f == Feed(
        id=f.id,
        slug="talks",
        title="Talks",
        description="Long ones",
        artwork=None,
        retention_days=14,
        is_default=True,
        created_at=T0,
        changed_at=T0,
    )
    assert db.get_feed("talks") == f and db.get_feed_by_id(f.id) == f
    assert f.created_at.tzinfo is not None


def test_get_missing_feed_is_none(db):
    assert db.get_feed("nope") is None
    assert db.get_feed_by_id(99) is None


def test_list_feeds_by_creation(db, clock):
    db.create_feed("b", "B")
    clock.advance(seconds=10)
    db.create_feed("a", "A")
    clock.set(T0 - timedelta(days=1))
    db.create_feed("c", "C")
    assert [f.slug for f in db.list_feeds()] == ["c", "b", "a"]


def test_slug_taken(db):
    db.create_feed("a", "A")
    with pytest.raises(Invalid, match="taken") as err:
        db.create_feed("a", "Again")
    assert str(err.value) == "The slug 'a' is taken."
    assert [f.title for f in db.list_feeds()] == ["A"]


def test_slug_taken_leaves_default_alone(db):
    db.create_feed("a", "A")
    with pytest.raises(Invalid):
        db.create_feed("a", "Again")
    # The failed create did not leave a transaction open.
    assert db.create_feed("b", "B").is_default is False


def test_delete_default_moves_flag_to_oldest(db, clock):
    db.create_feed("a", "A")
    clock.advance(seconds=10)
    db.create_feed("b", "B")
    clock.set(T0 + timedelta(seconds=5))
    db.create_feed("c", "C")
    db.delete_feed("a")
    assert db.default_feed().slug == "c"
    assert [f.is_default for f in db.list_feeds()] == [True, False]


def test_delete_default_breaks_created_at_ties_by_id(db):
    # The clock has whole-second resolution, so feeds made in one second share created_at.
    db.create_feed("a", "A")
    db.create_feed("b", "B")
    db.create_feed("c", "C")
    db.delete_feed("a")
    assert db.default_feed().slug == "b"


def test_delete_other_feed_keeps_default(db):
    db.create_feed("a", "A")
    db.create_feed("b", "B")
    db.delete_feed("b")
    assert db.default_feed().slug == "a"


def test_delete_last_feed_leaves_no_default(db):
    db.create_feed("a", "A")
    db.delete_feed("a")
    assert db.default_feed() is None
    assert db.create_feed("b", "B").is_default


def test_set_default(db):
    db.create_feed("a", "A")
    db.create_feed("b", "B")
    db.set_default("b")
    assert db.default_feed().slug == "b"
    assert [f.slug for f in db.list_feeds() if f.is_default] == ["b"]
    db.set_default("b")  # setting it again changes nothing
    assert db.default_feed().slug == "b"


def test_update_feed_bumps_changed_at(db, clock):
    f = db.create_feed("a", "A")
    clock.advance(minutes=5)
    updated = db.update_feed("a", title="New", description="About", retention_days=7)
    assert (updated.title, updated.description, updated.retention_days) == ("New", "About", 7)
    assert updated.changed_at == clock.now and updated.created_at == f.created_at
    assert db.get_feed("a") == updated
    cleared = db.update_feed("a", title="New", description=None, retention_days=None)
    assert (cleared.description, cleared.retention_days) == (None, None)


def test_set_feed_artwork_bumps_changed_at(db, clock):
    db.create_feed("a", "A")
    clock.advance(minutes=5)
    f = db.set_feed_artwork("a", "art.png")
    assert f.artwork == "art.png" and f.changed_at == clock.now
    clock.advance(minutes=5)
    f = db.set_feed_artwork("a", None)
    assert f.artwork is None and f.changed_at == clock.now


def test_feed_methods_raise_not_found_for_an_unknown_slug(db):
    with pytest.raises(NotFound):
        db.update_feed("nope", title="T", description=None, retention_days=None)
    with pytest.raises(NotFound):
        db.set_feed_artwork("nope", None)
    with pytest.raises(NotFound):
        db.set_default("nope")
    with pytest.raises(NotFound):
        db.delete_feed("nope")


def test_set_default_on_unknown_slug_keeps_the_default(db):
    db.create_feed("a", "A")
    with pytest.raises(NotFound):
        db.set_default("nope")
    assert db.default_feed().slug == "a"


def test_retention_must_be_at_least_one_day(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.create_feed("a", "A", retention_days=0)
    assert db.list_feeds() == []


# ------------------------------------------------------------- episodes


def test_add_or_get_episode_dedupes(db):
    f = db.create_feed("a", "A")
    e1, c1 = db.add_or_get_episode(f.id, "url", "https://x/y.mp3", None, None)
    e2, c2 = db.add_or_get_episode(f.id, "url", "https://x/y.mp3", "T", None)
    assert (c1, c2) == (True, False) and e1.id == e2.id and re.fullmatch("[a-z2-7]{12}", e1.id)
    # The second call returns the existing episode untouched.
    assert e2.title is None


def test_new_episode_fields(db):
    f = db.create_feed("a", "A")
    e, _ = db.add_or_get_episode(f.id, "url", "https://x/y.mp3", "Title", "About")
    assert e == Episode(
        id=e.id,
        feed_id=f.id,
        feed_slug="a",
        source_kind="url",
        source_ref="https://x/y.mp3",
        title="Title",
        description="About",
        artwork=None,
        file=None,
        mime=None,
        size_bytes=None,
        duration_s=None,
        status="queued",
        error=None,
        added_at=T0,
        published_at=None,
    )
    assert db.get_episode(e.id) == e


def test_same_source_in_two_feeds_is_two_episodes(db):
    a = db.create_feed("a", "A")
    b = db.create_feed("b", "B")
    e1, c1 = db.add_or_get_episode(a.id, "url", "https://x/y.mp3", None, None)
    e2, c2 = db.add_or_get_episode(b.id, "url", "https://x/y.mp3", None, None)
    assert (c1, c2) == (True, True) and e1.id != e2.id
    # The kind is part of the identity too.
    e3, c3 = db.add_or_get_episode(a.id, "upload", "https://x/y.mp3", None, None)
    assert c3 is True and e3.id != e1.id


def test_get_missing_episode_is_none(db):
    assert db.get_episode("aaaaaaaaaaaa") is None


def test_next_queued_oldest_first(db, clock):
    f = db.create_feed("a", "A")
    first, _ = db.add_or_get_episode(f.id, "url", "https://x/1", None, None)
    clock.advance(seconds=5)
    db.add_or_get_episode(f.id, "url", "https://x/2", None, None)
    assert db.next_queued().id == first.id


def test_next_queued_breaks_ties_by_insertion_order(db):
    # Two episodes with the same clock value (whole seconds): the first one added goes first.
    f = db.create_feed("a", "A")
    ids = [db.add_or_get_episode(f.id, "url", f"https://x/{n}", None, None)[0].id for n in range(5)]
    seen = []
    while (e := db.next_queued()) is not None:
        seen.append(e.id)
        db.set_status(e.id, "downloading")
    assert seen == ids


def test_next_queued_skips_other_statuses(db):
    f = db.create_feed("a", "A")
    assert db.next_queued() is None
    busy, _ = db.add_or_get_episode(f.id, "url", "https://x/1", None, None)
    failed, _ = db.add_or_get_episode(f.id, "url", "https://x/2", None, None)
    waiting, _ = db.add_or_get_episode(f.id, "url", "https://x/3", None, None)
    db.set_status(busy.id, "downloading")
    db.set_status(failed.id, "failed", "too large")
    assert db.next_queued().id == waiting.id


def test_list_episodes_newest_first(db, clock):
    f = db.create_feed("a", "A")
    old, _ = db.add_or_get_episode(f.id, "url", "https://x/old", None, None)
    clock.advance(hours=1)
    new, _ = db.add_or_get_episode(f.id, "url", "https://x/new", None, None)
    assert [e.id for e in db.list_episodes()] == [new.id, old.id]


def test_list_episodes_breaks_ties_by_insertion_order_newest_first(db):
    f = db.create_feed("a", "A")
    ids = [db.add_or_get_episode(f.id, "url", f"https://x/{n}", None, None)[0].id for n in range(4)]
    assert [e.id for e in db.list_episodes()] == ids[::-1]


def test_list_episodes_filters(db, clock):
    a = db.create_feed("a", "A")
    b = db.create_feed("b", "B")
    in_a, _ = db.add_or_get_episode(a.id, "url", "https://x/1", None, None)
    clock.advance(seconds=1)
    in_b, _ = db.add_or_get_episode(b.id, "url", "https://x/2", None, None)
    db.set_status(in_b.id, "failed", "timed out")
    assert [e.id for e in db.list_episodes(feed_id=a.id)] == [in_a.id]
    assert [e.id for e in db.list_episodes(status="failed")] == [in_b.id]
    assert db.list_episodes(feed_id=a.id, status="failed") == []
    assert {e.feed_slug for e in db.list_episodes()} == {"a", "b"}


def test_list_episodes_limit(db, clock):
    f = db.create_feed("a", "A")
    ids = []
    for n in range(60):
        ids.append(db.add_or_get_episode(f.id, "url", f"https://x/{n}", None, None)[0].id)
        clock.advance(seconds=1)
    assert len(db.list_episodes()) == 50  # the default limit
    assert [e.id for e in db.list_episodes(limit=3)] == ids[:-4:-1]
    assert len(db.list_episodes(limit=None)) == 60  # None means no limit
    assert [e.id for e in db.list_episodes(limit=None)] == ids[::-1]


def test_set_status_and_requeue(db):
    f = db.create_feed("a", "A")
    e, _ = db.add_or_get_episode(f.id, "url", "https://x/1", None, None)
    db.set_status(e.id, "downloading")
    assert (db.get_episode(e.id).status, db.get_episode(e.id).error) == ("downloading", None)
    db.set_status(e.id, "failed", "timed out")
    got = db.get_episode(e.id)
    assert (got.status, got.error) == ("failed", "timed out")
    db.requeue(e.id)
    got = db.get_episode(e.id)
    assert (got.status, got.error) == ("queued", None)
    # A row that has been deleted meanwhile is not an error (the worker can race a delete).
    db.delete_episode(e.id)
    db.set_status(e.id, "converting")
    db.requeue(e.id)
    assert db.get_episode(e.id) is None


def test_set_status_rejects_an_unknown_status(db):
    f = db.create_feed("a", "A")
    e, _ = db.add_or_get_episode(f.id, "url", "https://x/1", None, None)
    with pytest.raises(sqlite3.IntegrityError):
        db.set_status(e.id, "sleeping")


def test_reset_unfinished(db):
    f = db.create_feed("a", "A")
    downloading, _ = db.add_or_get_episode(f.id, "url", "https://x/1", None, None)
    converting, _ = db.add_or_get_episode(f.id, "url", "https://x/2", None, None)
    done = ready(db, f.id, "https://x/3")
    failed, _ = db.add_or_get_episode(f.id, "url", "https://x/4", None, None)
    db.set_status(downloading.id, "downloading")
    db.set_status(converting.id, "converting")
    db.set_status(failed.id, "failed", "timed out")
    assert db.reset_unfinished() == 2
    assert db.get_episode(downloading.id).status == "queued"
    assert db.get_episode(converting.id).status == "queued"
    assert db.get_episode(done.id).status == "ready"
    assert db.get_episode(failed.id).status == "failed"
    assert db.reset_unfinished() == 0


def test_mark_ready_sets_published_and_changed(db, clock):
    f = db.create_feed("a", "A")
    e, _ = db.add_or_get_episode(f.id, "url", "https://x/1", None, None)
    db.set_status(e.id, "converting")
    clock.advance(hours=2)
    assert db.mark_ready(
        e.id,
        title="Real title",
        description="About",
        artwork="a.jpg",
        file="abc.m4a",
        mime="audio/mp4",
        size_bytes=1234,
        duration_s=61.5,
    )
    got = db.get_episode(e.id)
    assert got.published_at == clock.now and got.added_at == T0
    assert got.status == "ready" and got.error is None
    assert (got.title, got.description, got.artwork) == ("Real title", "About", "a.jpg")
    assert (got.file, got.mime, got.size_bytes, got.duration_s) == (
        "abc.m4a",
        "audio/mp4",
        1234,
        61.5,
    )
    assert db.get_feed("a").changed_at == clock.now


def test_mark_ready_on_deleted_row(db):
    f = db.create_feed("a", "A")
    e, _ = db.add_or_get_episode(f.id, "url", "https://x/1", None, None)
    db.delete_episode(e.id)
    assert (
        db.mark_ready(
            e.id,
            title="T",
            description=None,
            artwork=None,
            file="x.m4a",
            mime="audio/mp4",
            size_bytes=1,
            duration_s=1.0,
        )
        is False
    )
    assert db.list_episodes() == []
    assert db.get_feed("a").changed_at == T0  # the feed did not change either


def test_edit_ready_episode_bumps_feed(db, clock):
    f = db.create_feed("a", "A")
    e = ready(db, f.id, "https://x/1")
    clock.advance(minutes=3)
    got = db.update_episode_text(e.id, "Better", "Notes")
    assert (got.title, got.description) == ("Better", "Notes")
    assert db.get_episode(e.id) == got
    assert db.get_feed("a").changed_at == clock.now


def test_edit_unfinished_episode_leaves_feed_alone(db, clock):
    f = db.create_feed("a", "A")
    e, _ = db.add_or_get_episode(f.id, "url", "https://x/1", None, None)
    clock.advance(minutes=3)
    got = db.update_episode_text(e.id, "Better", None)
    assert got.title == "Better" and got.description is None
    assert db.get_feed("a").changed_at == T0


def test_update_episode_text_on_unknown_id(db):
    with pytest.raises(NotFound):
        db.update_episode_text("aaaaaaaaaaaa", "T", None)


def test_delete_ready_episode_bumps_feed(db, clock):
    f = db.create_feed("a", "A")
    e = ready(db, f.id, "https://x/1")
    clock.advance(minutes=3)
    db.delete_episode(e.id)
    assert db.get_episode(e.id) is None
    assert db.get_feed("a").changed_at == clock.now


def test_delete_unfinished_episode_leaves_feed_alone(db, clock):
    f = db.create_feed("a", "A")
    e, _ = db.add_or_get_episode(f.id, "url", "https://x/1", None, None)
    clock.advance(minutes=3)
    db.delete_episode(e.id)
    assert db.get_episode(e.id) is None
    assert db.get_feed("a").changed_at == T0
    db.delete_episode(e.id)  # deleting it again is harmless


def test_taking_a_ready_episode_out_of_ready_bumps_feed(db, clock):
    f = db.create_feed("a", "A")
    e = ready(db, f.id, "https://x/1")
    clock.advance(minutes=3)
    db.requeue(e.id)
    assert db.get_feed("a").changed_at == clock.now
    clock.advance(minutes=3)
    db.set_status(e.id, "downloading")  # queued -> downloading is not a feed change
    assert db.get_feed("a").changed_at == T0 + timedelta(minutes=3)


def test_expired_uses_feed_retention_or_default(db, clock):
    a = db.create_feed("a", "A", retention_days=7)
    b = db.create_feed("b", "B")
    now = clock.now
    ids = {}
    for name, feed, days in [
        ("a_8", a, 8),
        ("a_6", a, 6),
        ("b_8", b, 8),
        ("b_31", b, 31),
        ("a_failed_8", a, 8),
        ("b_failed_8", b, 8),
        ("b_failed_31", b, 31),
    ]:
        clock.set(now - timedelta(days=days))
        if "failed" in name:
            e, _ = db.add_or_get_episode(feed.id, "url", f"https://x/{name}", None, None)
            db.set_status(e.id, "failed", "timed out")
        else:
            e = ready(db, feed.id, f"https://x/{name}")
        ids[name] = e.id
    # A queued one 40 days old never expires, and neither do downloading or converting ones.
    clock.set(now - timedelta(days=40))
    for name, status in [("queued", "queued"), ("busy", "downloading"), ("conv", "converting")]:
        e, _ = db.add_or_get_episode(b.id, "url", f"https://x/{name}", None, None)
        db.set_status(e.id, status)
    clock.set(now)
    got = db.expired(default_days=30)
    assert {e.id for e in got} == {ids["a_8"], ids["b_31"], ids["a_failed_8"], ids["b_failed_31"]}
    assert [e.added_at for e in got] == sorted(e.added_at for e in got)
    assert {e.id for e in db.expired(default_days=7)} >= {ids["b_8"], ids["b_failed_8"]}


def test_expired_is_strictly_older_than_the_retention(db, clock):
    f = db.create_feed("a", "A", retention_days=7)
    now = clock.now
    clock.set(now - timedelta(days=7))
    on_the_line = ready(db, f.id, "https://x/1")
    clock.set(now - timedelta(days=7, seconds=1))
    past = ready(db, f.id, "https://x/2")
    clock.set(now)
    assert [e.id for e in db.expired(default_days=30)] == [past.id]
    assert on_the_line.id not in {e.id for e in db.expired(default_days=30)}


def test_delete_feed_cascades(db):
    a = db.create_feed("a", "A")
    b = db.create_feed("b", "B")
    ready(db, a.id, "https://x/1")
    db.add_or_get_episode(a.id, "url", "https://x/2", None, None)
    keep = ready(db, b.id, "https://x/3")
    db.delete_feed("a")
    assert [e.id for e in db.list_episodes()] == [keep.id]
    assert db.get_feed("a") is None


def test_ready_counts(db):
    a = db.create_feed("a", "A")
    b = db.create_feed("b", "B")
    c = db.create_feed("c", "C")
    ready(db, a.id, "https://x/1")
    ready(db, a.id, "https://x/2")
    ready(db, b.id, "https://x/3")
    queued, _ = db.add_or_get_episode(a.id, "url", "https://x/4", None, None)
    failed, _ = db.add_or_get_episode(b.id, "url", "https://x/5", None, None)
    db.set_status(failed.id, "failed", "timed out")
    counts = db.ready_counts()
    assert counts[a.id] == 2 and counts[b.id] == 1
    # A feed without ready episodes counts 0 whether the caller uses [] or .get().
    assert counts[c.id] == 0 and counts.get(c.id, 0) == 0
    assert queued.id not in counts


# ------------------------------------------------------ storage details


def test_data_survives_reopening_and_uses_wal(tmp_path, clock):
    path = tmp_path / "podcast.db"
    first = Database(path, clock=clock)
    f = first.create_feed("a", "A")
    e = ready(first, f.id, "https://x/1")
    first.close()

    second = Database(str(path), clock=clock)
    try:
        assert second.get_feed("a") == f
        got = second.get_episode(e.id)
        assert got.status == "ready" and got.published_at == T0 and got.added_at == T0
        assert second._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert second._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert second._conn.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        second.close()


def test_a_failed_commit_surfaces_and_leaves_the_connection_usable(db):
    # A DEFERRABLE foreign key is checked at COMMIT, so the statement itself succeeds.
    db._conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
    db._conn.execute(
        "CREATE TABLE child (parent_id INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)"
    )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        with db._tx() as conn:
            conn.execute("INSERT INTO child VALUES (42)")
    assert not db._conn.in_transaction
    assert db._conn.execute("SELECT COUNT(*) FROM child").fetchone()[0] == 0  # rolled back
    # The connection is not stuck in the failed transaction: the next write works.
    assert db.create_feed("a", "A").slug == "a"
    assert [f.slug for f in db.list_feeds()] == ["a"]


def test_the_original_error_surfaces_when_sqlite_has_already_rolled_back(db):
    # ON CONFLICT ROLLBACK ends the transaction inside SQLite, so there is nothing to roll back.
    db._conn.execute("CREATE TABLE once (n INTEGER PRIMARY KEY)")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        with db._tx() as conn:
            conn.execute("INSERT INTO once VALUES (1)")
            conn.execute("INSERT OR ROLLBACK INTO once VALUES (1)")
    assert not db._conn.in_transaction
    assert db.create_feed("a", "A").slug == "a"


def test_a_body_that_ends_the_transaction_itself_surfaces_its_own_error(db):
    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with db._tx() as conn:
            conn.execute("ROLLBACK")
            raise Boom
    assert not db._conn.in_transaction
    assert db.create_feed("a", "A").slug == "a"


def test_timestamps_are_utc_text_that_sorts_chronologically(db, clock):
    f = db.create_feed("a", "A")
    clock.set(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
    db.add_or_get_episode(f.id, "url", "https://x/1", None, None)
    clock.set(datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC))
    db.add_or_get_episode(f.id, "url", "https://x/2", None, None)
    stored = [r[0] for r in db._conn.execute("SELECT added_at FROM episodes ORDER BY rowid")]
    assert stored == sorted(stored)
    assert all(isinstance(s, str) and s.endswith("+00:00") for s in stored)
    assert datetime.fromisoformat(stored[0]) == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_a_clock_in_another_zone_is_stored_as_utc():
    cet = timezone(timedelta(hours=1))
    database = Database(":memory:", clock=lambda: datetime(2026, 10, 29, 13, 0, 0, tzinfo=cet))
    try:
        f = database.create_feed("a", "A")
        assert f.created_at == T0 and f.created_at.utcoffset() == timedelta(0)
    finally:
        database.close()


def test_a_naive_clock_is_refused():
    database = Database(":memory:", clock=lambda: datetime(2026, 10, 29, 12, 0, 0))
    try:
        with pytest.raises(ValueError, match="aware"):
            database.create_feed("a", "A")
        assert database.list_feeds() == []
    finally:
        database.close()


def test_calls_from_many_threads_are_serialised(tmp_path):
    database = Database(tmp_path / "podcast.db")
    f = database.create_feed("a", "A")
    errors = []

    def work(worker: int) -> None:
        try:
            for n in range(40):
                database.add_or_get_episode(f.id, "url", f"https://x/{worker}/{n}", None, None)
                database.list_episodes(limit=None)
                database.next_queued()
        except BaseException as exc:  # the test reports whatever went wrong
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(w,)) for w in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert errors == []
        assert len(database.list_episodes(limit=None)) == 8 * 40
    finally:
        database.close()
