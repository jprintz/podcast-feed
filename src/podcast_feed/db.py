"""The SQLite database shared by the admin UI, the MCP server and the worker.

One connection serves the event loop and the worker threads, so every method takes the
same lock, reads included. Changes of more than one statement run in one transaction.

Timestamps are stored as ISO 8601 UTC text with a fixed width, so they sort correctly as
text, and they come back as aware UTC datetimes. The clock has whole-second resolution,
which makes ties common: every ordering therefore falls back on the insertion order (the
rowid) or on the id.

Looking something up by a key that does not exist gives None. Changing something that does
not exist raises NotFound, except where the caller may be racing a delete (the worker's
set_status, requeue, mark_ready and delete_episode), which then do nothing.
"""

import secrets
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from podcast_feed.errors import Invalid, NotFound

_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
_ID_LENGTH = 12


def utcnow() -> datetime:
    """Now as an aware UTC datetime without microseconds (they would not survive RFC 2822)."""
    return datetime.now(UTC).replace(microsecond=0)


def new_episode_id() -> str:
    """12 random base32 characters: part of the public media URL, so unguessable."""
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(_ID_LENGTH))


@dataclass(frozen=True)
class Feed:
    id: int
    slug: str
    title: str
    description: str | None
    artwork: str | None
    retention_days: int | None
    is_default: bool
    created_at: datetime
    changed_at: datetime


@dataclass(frozen=True)
class Episode:
    id: str
    feed_id: int
    feed_slug: str
    source_kind: str
    source_ref: str
    title: str | None
    description: str | None
    artwork: str | None
    file: str | None
    mime: str | None
    size_bytes: int | None
    duration_s: float | None
    status: str
    error: str | None
    added_at: datetime
    published_at: datetime | None


MIGRATIONS = (
    """
    CREATE TABLE feeds (
      id INTEGER PRIMARY KEY,
      slug TEXT NOT NULL UNIQUE,
      title TEXT NOT NULL,
      description TEXT,
      artwork TEXT,
      retention_days INTEGER CHECK (retention_days IS NULL OR retention_days >= 1),
      is_default INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,
      changed_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX feeds_one_default ON feeds(is_default) WHERE is_default = 1;
    CREATE TABLE episodes (
      id TEXT PRIMARY KEY,
      feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
      source_kind TEXT NOT NULL,
      source_ref TEXT NOT NULL,
      title TEXT, description TEXT, artwork TEXT,
      file TEXT, mime TEXT, size_bytes INTEGER, duration_s REAL,
      status TEXT NOT NULL CHECK (status IN ('queued','downloading','converting','ready','failed')),
      error TEXT,
      added_at TEXT NOT NULL,
      published_at TEXT,
      UNIQUE (feed_id, source_kind, source_ref)
    );
    """,
)

_EPISODE = "SELECT e.*, f.slug AS feed_slug FROM episodes e JOIN feeds f ON f.id = e.feed_id"


def _ts(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("The clock must return an aware datetime.")
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _when(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)


def _feed(row: sqlite3.Row) -> Feed:
    return Feed(
        id=row["id"],
        slug=row["slug"],
        title=row["title"],
        description=row["description"],
        artwork=row["artwork"],
        retention_days=row["retention_days"],
        is_default=bool(row["is_default"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        changed_at=datetime.fromisoformat(row["changed_at"]),
    )


def _episode(row: sqlite3.Row) -> Episode:
    return Episode(
        id=row["id"],
        feed_id=row["feed_id"],
        feed_slug=row["feed_slug"],
        source_kind=row["source_kind"],
        source_ref=row["source_ref"],
        title=row["title"],
        description=row["description"],
        artwork=row["artwork"],
        file=row["file"],
        mime=row["mime"],
        size_bytes=row["size_bytes"],
        duration_s=row["duration_s"],
        status=row["status"],
        error=row["error"],
        added_at=datetime.fromisoformat(row["added_at"]),
        published_at=_when(row["published_at"]),
    )


def _no_feed(slug: str) -> NotFound:
    return NotFound(f"There is no feed '{slug}'.")


class Database:
    def __init__(self, path: Path | str, clock: Callable[[], datetime] = utcnow):
        self._clock = clock
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        try:
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys=ON")
            if str(path) != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._migrate()
        except BaseException:
            self._conn.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- plumbing

    def _migrate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        for target, script in enumerate(MIGRATIONS, start=1):
            if target <= version:
                continue
            try:
                # user_version is part of the transaction, so a failed step leaves no trace.
                self._conn.executescript(
                    f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {target};\nCOMMIT;"
                )
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    @contextmanager
    def _locked(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                # COMMIT can fail (a deferred foreign key is checked here) and then leaves the
                # transaction open, so it belongs in the try: the connection must not stay in it.
                self._conn.execute("COMMIT")
            except BaseException:
                # SQLite may have ended the transaction itself (ON CONFLICT ROLLBACK, a full disk),
                # and a ROLLBACK then would raise and hide the error that matters.
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    def _touch(self, conn: sqlite3.Connection, feed_id: int, now: str) -> None:
        conn.execute("UPDATE feeds SET changed_at = ? WHERE id = ?", (now, feed_id))

    # ------------------------------------------------------------------- feeds

    def create_feed(
        self,
        slug: str,
        title: str,
        description: str | None = None,
        retention_days: int | None = None,
    ) -> Feed:
        now = _ts(self._clock())
        with self._tx() as conn:
            if conn.execute("SELECT 1 FROM feeds WHERE slug = ?", (slug,)).fetchone():
                raise Invalid(f"The slug '{slug}' is taken.")
            first = conn.execute("SELECT 1 FROM feeds WHERE is_default = 1").fetchone() is None
            cursor = conn.execute(
                "INSERT INTO feeds (slug, title, description, retention_days, is_default,"
                " created_at, changed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (slug, title, description, retention_days, int(first), now, now),
            )
            row = conn.execute("SELECT * FROM feeds WHERE id = ?", (cursor.lastrowid,)).fetchone()
            return _feed(row)

    def get_feed(self, slug: str) -> Feed | None:
        with self._locked() as conn:
            row = conn.execute("SELECT * FROM feeds WHERE slug = ?", (slug,)).fetchone()
        return None if row is None else _feed(row)

    def get_feed_by_id(self, id: int) -> Feed | None:
        with self._locked() as conn:
            row = conn.execute("SELECT * FROM feeds WHERE id = ?", (id,)).fetchone()
        return None if row is None else _feed(row)

    def list_feeds(self) -> list[Feed]:
        with self._locked() as conn:
            rows = conn.execute("SELECT * FROM feeds ORDER BY created_at, id").fetchall()
        return [_feed(r) for r in rows]

    def default_feed(self) -> Feed | None:
        with self._locked() as conn:
            row = conn.execute("SELECT * FROM feeds WHERE is_default = 1").fetchone()
        return None if row is None else _feed(row)

    def update_feed(
        self, slug: str, *, title: str, description: str | None, retention_days: int | None
    ) -> Feed:
        now = _ts(self._clock())
        with self._tx() as conn:
            cursor = conn.execute(
                "UPDATE feeds SET title = ?, description = ?, retention_days = ?, changed_at = ?"
                " WHERE slug = ?",
                (title, description, retention_days, now, slug),
            )
            if cursor.rowcount == 0:
                raise _no_feed(slug)
            return _feed(conn.execute("SELECT * FROM feeds WHERE slug = ?", (slug,)).fetchone())

    def set_feed_artwork(self, slug: str, artwork: str | None) -> Feed:
        now = _ts(self._clock())
        with self._tx() as conn:
            cursor = conn.execute(
                "UPDATE feeds SET artwork = ?, changed_at = ? WHERE slug = ?", (artwork, now, slug)
            )
            if cursor.rowcount == 0:
                raise _no_feed(slug)
            return _feed(conn.execute("SELECT * FROM feeds WHERE slug = ?", (slug,)).fetchone())

    def set_default(self, slug: str) -> None:
        with self._tx() as conn:
            row = conn.execute("SELECT id FROM feeds WHERE slug = ?", (slug,)).fetchone()
            if row is None:
                raise _no_feed(slug)
            # Clear first: the unique index allows only one default at any moment.
            conn.execute("UPDATE feeds SET is_default = 0 WHERE is_default = 1")
            conn.execute("UPDATE feeds SET is_default = 1 WHERE id = ?", (row["id"],))

    def delete_feed(self, slug: str) -> None:
        """Delete the feed and, by cascade, its episodes. The files are the caller's to remove."""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT id, is_default FROM feeds WHERE slug = ?", (slug,)
            ).fetchone()
            if row is None:
                raise _no_feed(slug)
            conn.execute("DELETE FROM feeds WHERE id = ?", (row["id"],))
            if row["is_default"]:
                conn.execute(
                    "UPDATE feeds SET is_default = 1"
                    " WHERE id = (SELECT id FROM feeds ORDER BY created_at, id LIMIT 1)"
                )

    def ready_counts(self) -> dict[int, int]:
        """Ready episodes per feed id; a feed without any counts 0."""
        with self._locked() as conn:
            rows = conn.execute(
                "SELECT f.id, COUNT(e.id) FROM feeds f"
                " LEFT JOIN episodes e ON e.feed_id = f.id AND e.status = 'ready'"
                " GROUP BY f.id"
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    # ---------------------------------------------------------------- episodes

    def add_or_get_episode(
        self,
        feed_id: int,
        source_kind: str,
        source_ref: str,
        title: str | None,
        description: str | None,
    ) -> tuple[Episode, bool]:
        """The episode for this source in this feed, and whether this call created it."""
        now = _ts(self._clock())
        with self._tx() as conn:
            cursor = conn.execute(
                "INSERT INTO episodes (id, feed_id, source_kind, source_ref, title, description,"
                " status, added_at) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)"
                " ON CONFLICT (feed_id, source_kind, source_ref) DO NOTHING",
                (new_episode_id(), feed_id, source_kind, source_ref, title, description, now),
            )
            row = conn.execute(
                f"{_EPISODE} WHERE e.feed_id = ? AND e.source_kind = ? AND e.source_ref = ?",
                (feed_id, source_kind, source_ref),
            ).fetchone()
            return _episode(row), cursor.rowcount == 1

    def get_episode(self, id: str) -> Episode | None:
        with self._locked() as conn:
            row = conn.execute(f"{_EPISODE} WHERE e.id = ?", (id,)).fetchone()
        return None if row is None else _episode(row)

    def list_episodes(
        self, feed_id: int | None = None, status: str | None = None, limit: int | None = 50
    ) -> list[Episode]:
        """Newest first; limit=None returns them all."""
        where: list[str] = []
        params: list[object] = []
        if feed_id is not None:
            where.append("e.feed_id = ?")
            params.append(feed_id)
        if status is not None:
            where.append("e.status = ?")
            params.append(status)
        sql = _EPISODE
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY e.added_at DESC, e.rowid DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._locked() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_episode(r) for r in rows]

    def next_queued(self) -> Episode | None:
        with self._locked() as conn:
            row = conn.execute(
                f"{_EPISODE} WHERE e.status = 'queued' ORDER BY e.added_at, e.rowid LIMIT 1"
            ).fetchone()
        return None if row is None else _episode(row)

    def set_status(self, id: str, status: str, error: str | None = None) -> None:
        now = _ts(self._clock())
        with self._tx() as conn:
            row = conn.execute(
                "SELECT feed_id, status FROM episodes WHERE id = ?", (id,)
            ).fetchone()
            if row is None:
                return
            conn.execute(
                "UPDATE episodes SET status = ?, error = ? WHERE id = ?", (status, error, id)
            )
            # A ready episode is in the feed's XML; one that enters or leaves "ready" changes it.
            if (row["status"] == "ready") != (status == "ready"):
                self._touch(conn, row["feed_id"], now)

    def requeue(self, id: str) -> None:
        self.set_status(id, "queued")

    def update_episode_text(self, id: str, title: str | None, description: str | None) -> Episode:
        now = _ts(self._clock())
        with self._tx() as conn:
            row = conn.execute(
                "SELECT feed_id, status FROM episodes WHERE id = ?", (id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"There is no episode '{id}'.")
            conn.execute(
                "UPDATE episodes SET title = ?, description = ? WHERE id = ?",
                (title, description, id),
            )
            if row["status"] == "ready":
                self._touch(conn, row["feed_id"], now)
            return _episode(conn.execute(f"{_EPISODE} WHERE e.id = ?", (id,)).fetchone())

    def mark_ready(
        self,
        id: str,
        *,
        title: str | None,
        description: str | None,
        artwork: str | None,
        file: str,
        mime: str,
        size_bytes: int,
        duration_s: float | None,
    ) -> bool:
        """Publish the episode now. False when it was deleted while it was being converted."""
        now = _ts(self._clock())
        with self._tx() as conn:
            row = conn.execute("SELECT feed_id FROM episodes WHERE id = ?", (id,)).fetchone()
            if row is None:
                return False
            conn.execute(
                "UPDATE episodes SET title = ?, description = ?, artwork = ?, file = ?, mime = ?,"
                " size_bytes = ?, duration_s = ?, status = 'ready', error = NULL, published_at = ?"
                " WHERE id = ?",
                (title, description, artwork, file, mime, size_bytes, duration_s, now, id),
            )
            self._touch(conn, row["feed_id"], now)
            return True

    def delete_episode(self, id: str) -> None:
        """Delete the row. The files are the caller's to remove."""
        now = _ts(self._clock())
        with self._tx() as conn:
            row = conn.execute(
                "SELECT feed_id, status FROM episodes WHERE id = ?", (id,)
            ).fetchone()
            if row is None:
                return
            conn.execute("DELETE FROM episodes WHERE id = ?", (id,))
            if row["status"] == "ready":
                self._touch(conn, row["feed_id"], now)

    def reset_unfinished(self) -> int:
        """Queue again what a restart interrupted. Returns how many episodes that was."""
        with self._locked() as conn:
            cursor = conn.execute(
                "UPDATE episodes SET status = 'queued', error = NULL"
                " WHERE status IN ('downloading', 'converting')"
            )
            return cursor.rowcount

    def expired(self, default_days: int) -> list[Episode]:
        """Ready and failed episodes older than their feed's retention (default_days if none).

        Age counts from added_at. Queued and running episodes never expire. Oldest first.
        """
        now = self._clock()
        with self._locked() as conn:
            rows = conn.execute(
                "SELECT e.*, f.slug AS feed_slug, f.retention_days AS retention"
                " FROM episodes e JOIN feeds f ON f.id = e.feed_id"
                " WHERE e.status IN ('ready', 'failed') ORDER BY e.added_at, e.rowid"
            ).fetchall()
        return [
            _episode(r)
            for r in rows
            if r["added_at"]
            < _ts(now - timedelta(days=default_days if r["retention"] is None else r["retention"]))
        ]
