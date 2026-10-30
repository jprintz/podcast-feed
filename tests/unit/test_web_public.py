import asyncio
import email.utils
import hashlib
import inspect
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from podcast_feed.config import Settings
from podcast_feed.db import Database, Episode
from podcast_feed.service import Service
from podcast_feed.storage import Storage
from podcast_feed.web_public import public_routes

BASE = "https://p.example/s"
# Every write happens in this second, as in a busy minute: changed_at cannot tell them apart.
NOW = datetime(2026, 10, 29, 12, 0, 0, tzinfo=UTC)
AUDIO = bytes(range(256)) * 4
PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 20
SIZE = len(AUDIO)
NS = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}
FEED_TYPE = "application/rss+xml; charset=utf-8"


@pytest.fixture
def db():
    database = Database(":memory:", clock=lambda: NOW)
    yield database
    database.close()


@pytest.fixture
def storage(tmp_path):
    store = Storage(tmp_path / "media")
    store.ensure_layout()
    return store


@pytest.fixture
def svc(db, storage, tmp_path):
    settings = Settings.from_env(
        {
            "PODCAST_PUBLIC_BASE": BASE,
            "PODCAST_DATA_DIR": str(tmp_path / "data"),
            "PODCAST_MEDIA_DIR": str(tmp_path / "media"),
        }
    )
    service = Service(db, storage, settings, lambda: None)
    service.create_feed("Talks")
    return service


def publish(
    svc: Service, db: Database, storage: Storage, url: str, *, artwork: bool = True
) -> Episode:
    """A ready episode with 1 KiB of audio and (unless artwork=False) a cover of its own."""
    episode = svc.add_episode(url)
    folder = storage.feed_dir("talks")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{episode.id}.mp3").write_bytes(AUDIO)
    if artwork:
        (folder / f"{episode.id}.png").write_bytes(PNG)
    assert db.mark_ready(
        episode.id,
        title=f"Title of {url}",
        description=None,
        artwork=f"{episode.id}.png" if artwork else None,
        file=f"{episode.id}.mp3",
        mime="audio/mpeg",
        size_bytes=SIZE,
        duration_s=61.0,
    )
    return svc.get_episode(episode.id)


@pytest.fixture
def episode(svc, db, storage) -> Episode:
    svc.set_feed_artwork("talks", PNG)
    return publish(svc, db, storage, "https://x.example/a.mp3")


@pytest.fixture
def client(svc, episode):
    return TestClient(Starlette(routes=public_routes(svc)))


def guids(response) -> list[str | None]:
    return [g.text for g in ET.fromstring(response.content).iterfind("channel/item/guid")]


# ------------------------------------------------------------------------- the feed


def test_feed(client, episode):
    r = client.get("/feeds/talks.xml")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/rss+xml; charset=utf-8"
    assert r.headers["etag"]
    assert guids(r) == [episode.id]
    assert ET.fromstring(r.content).findtext("channel/title") == "Talks"


def test_feed_headers(client, svc):
    r = client.get("/feeds/talks.xml")
    # The ETag is a hash of the bytes served (R7), the date is when the feed changed.
    assert r.headers["etag"] == f'"{hashlib.sha1(r.content).hexdigest()}"'
    assert r.headers["last-modified"] == email.utils.format_datetime(NOW, usegmt=True)
    assert email.utils.parsedate_to_datetime(r.headers["last-modified"]) == (
        svc.get_feed("talks").changed_at
    )
    assert int(r.headers["content-length"]) == len(r.content)


def test_the_etag_follows_the_content_within_one_second(client, svc):
    before = client.get("/feeds/talks.xml")
    svc.update_feed("talks", title="Other title", description=None, retention_days=None)
    after = client.get("/feeds/talks.xml")
    # Same second, same Last-Modified: only the content hash tells them apart.
    assert after.headers["last-modified"] == before.headers["last-modified"]
    assert after.headers["etag"] != before.headers["etag"]
    assert client.get("/feeds/talks.xml").headers["etag"] == after.headers["etag"]


def test_feed_304(client):
    etag = client.get("/feeds/talks.xml").headers["etag"]
    r = client.get("/feeds/talks.xml", headers={"If-None-Match": etag})
    assert r.status_code == 304
    assert r.content == b""
    assert r.headers["etag"] == etag


@pytest.mark.parametrize(
    "header",
    ["{etag}", "W/{etag}", '"other", {etag}', '"other",W/{etag} , "more"', "*", " {etag} "],
)
def test_feed_304_for_every_way_to_name_the_etag(client, header):
    etag = client.get("/feeds/talks.xml").headers["etag"]
    r = client.get("/feeds/talks.xml", headers={"If-None-Match": header.format(etag=etag)})
    assert r.status_code == 304 and r.content == b""
    assert r.headers["etag"] == etag


@pytest.mark.parametrize("header", ['"stale"', 'W/"stale"', "", "stale", '"stale", "older"'])
def test_feed_200_for_another_etag(client, header):
    r = client.get("/feeds/talks.xml", headers={"If-None-Match": header})
    assert r.status_code == 200 and r.content


def test_feed_304_with_the_etag_of_an_older_feed(client, svc):
    old = client.get("/feeds/talks.xml").headers["etag"]
    svc.update_feed("talks", title="Changed", description=None, retention_days=None)
    r = client.get("/feeds/talks.xml", headers={"If-None-Match": old})
    assert r.status_code == 200 and r.headers["etag"] != old


def test_feed_head(client):
    got = client.get("/feeds/talks.xml")
    r = client.head("/feeds/talks.xml")
    assert r.status_code == 200 and r.content == b""
    assert r.headers["content-type"] == FEED_TYPE
    assert r.headers["etag"] == got.headers["etag"]
    assert r.headers["last-modified"] == got.headers["last-modified"]
    assert r.headers["content-length"] == str(len(got.content))
    again = client.head("/feeds/talks.xml", headers={"If-None-Match": got.headers["etag"]})
    assert again.status_code == 304 and again.content == b""


def test_the_feed_is_revalidated_on_every_use(client):
    # Without a Cache-Control a cache may guess a freshness lifetime from Last-Modified and
    # keep serving an old feed; the 304 repeats the header, as RFC 9111 has it update the entry.
    etag = client.get("/feeds/talks.xml").headers["etag"]
    conditional = {"If-None-Match": etag}
    responses = [
        (200, client.get("/feeds/talks.xml")),
        (304, client.get("/feeds/talks.xml", headers=conditional)),
        (200, client.head("/feeds/talks.xml")),
        (304, client.head("/feeds/talks.xml", headers=conditional)),
    ]
    for status, response in responses:
        assert response.status_code == status
        assert response.headers["cache-control"] == "private, no-cache", (status, response.request)


@pytest.mark.parametrize(
    "path",
    ["/feeds/nope.xml", "/feeds/Talks.xml", "/feeds/a.b.xml", "/feeds/%00.xml", "/feeds/-.xml"],
)
def test_unknown_feed(client, path):
    r = client.get(path)
    assert (r.status_code, r.text) == (404, "no such feed\n")
    assert r.headers["content-type"].startswith("text/plain")
    head = client.head(path)
    assert head.status_code == 404 and head.content == b""


def test_missing_file_skipped(client, svc, db, storage, episode):
    gone = publish(svc, db, storage, "https://x.example/gone.mp3")
    assert set(guids(client.get("/feeds/talks.xml"))) == {episode.id, gone.id}
    (storage.feed_dir("talks") / f"{gone.id}.mp3").unlink()
    assert guids(client.get("/feeds/talks.xml")) == [episode.id]


def test_only_ready_episodes_are_in_the_feed(client, svc, episode):
    svc.add_episode("https://x.example/queued.mp3")
    assert guids(client.get("/feeds/talks.xml")) == [episode.id]


def test_a_feed_of_another_slug_has_its_own_episodes(client, svc):
    svc.create_feed("Other")
    r = client.get("/feeds/other.xml")
    assert r.status_code == 200 and guids(r) == []


def test_every_url_in_the_feed_works(client, episode):
    xml = ET.fromstring(client.get("/feeds/talks.xml").content)
    urls = [e.get("url") for e in xml.iterfind(".//enclosure")]
    urls += [e.get("href") for e in xml.iterfind(".//itunes:image", NS)]
    assert len(urls) == 3  # the audio, the episode's cover and the feed's cover
    for url in urls:
        assert url is not None and url.startswith(BASE + "/media/talks/")
        assert client.get(url.removeprefix(BASE)).status_code == 200, url


def test_a_title_with_xml_trouble_still_gives_a_feed(client, svc, episode):
    svc.update_episode(episode.id, "Bad \x1b\x01 & <b>title</b>", "Desc \x0b")
    svc.update_feed("talks", title="Feed \x1b & <i>", description=None, retention_days=None)
    r = client.get("/feeds/talks.xml")
    assert r.status_code == 200
    ch = ET.fromstring(r.content).find("channel")
    assert ch is not None
    assert ch.findtext("title") == "Feed  & <i>"
    assert ch.findtext("item/title") == "Bad  & <b>title</b>"


# ------------------------------------------------------------------------- the media


def test_media_full_and_range(client, episode):
    path = f"/media/talks/{episode.id}.mp3"
    r = client.get(path)
    assert r.status_code == 200 and r.content == AUDIO
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-length"] == str(SIZE)
    r = client.get(path, headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == AUDIO[:10]
    assert r.headers["content-range"] == f"bytes 0-9/{SIZE}"
    assert r.headers["content-length"] == "10"
    assert r.headers["accept-ranges"] == "bytes"
    r = client.get(path, headers={"Range": "bytes=-10"})
    assert r.status_code == 206 and r.content == AUDIO[-10:]
    assert r.headers["content-range"] == f"bytes {SIZE - 10}-{SIZE - 1}/{SIZE}"


def test_media_range_open_end(client, episode):
    r = client.get(f"/media/talks/{episode.id}.mp3", headers={"Range": "bytes=0-"})
    assert r.status_code == 206 and r.content == AUDIO
    assert r.headers["content-range"] == f"bytes 0-{SIZE - 1}/{SIZE}"
    r = client.get(f"/media/talks/{episode.id}.mp3", headers={"Range": "bytes=1000-"})
    assert r.status_code == 206 and r.content == AUDIO[1000:]


def test_media_range_past_end(client, episode):
    path = f"/media/talks/{episode.id}.mp3"
    for header in (f"bytes={SIZE + 10}-", f"bytes={SIZE}-", f"bytes={SIZE + 10}-{SIZE + 20}"):
        r = client.get(path, headers={"Range": header})
        assert r.status_code == 416, header
        assert r.headers["content-range"] == f"bytes */{SIZE}"


def test_media_range_to_past_the_end_is_cut_short(client, episode):
    r = client.get(f"/media/talks/{episode.id}.mp3", headers={"Range": f"bytes=1020-{SIZE + 50}"})
    assert r.status_code == 206 and r.content == AUDIO[1020:]
    assert r.headers["content-range"] == f"bytes 1020-{SIZE - 1}/{SIZE}"


@pytest.mark.parametrize(
    ("header", "status"),
    [
        ("bytes=0-", 206),
        ("bytes=0-0", 206),
        (f"bytes={SIZE - 1}-", 206),
        (f"bytes={SIZE}-", 416),
        (f"bytes={SIZE + 10}-", 416),
        ("bytes=-0", 416),
        ("bytes=0-1,5-6", 206),  # two ranges: a multipart answer
        ("bytes=0-3, 10-12", 206),
        # Not valid ranges: a server ignores the header and sends the whole file.
        ("bytes=abc", 200),
        ("bytes=", 200),
        ("bytes=-", 200),
        ("bytes=5-2", 200),
        ("bytes=0-1,5-2", 200),
        ("bytes=0-5-9", 200),
        ("bytes=a-b", 200),
        ("bytes==0-5", 200),
        ("items=0-5", 200),
        ("garbage", 200),
        ("", 200),
        ("bytes=" + "9" * 30 + "-", 200),
        ("bytes=" + ",".join(["0-1"] * 150), 200),
    ],
)
def test_media_odd_range_headers_never_fail(client, episode, header, status):
    path = f"/media/talks/{episode.id}.mp3"
    r = client.get(path, headers={"Range": header})
    assert r.status_code == status, header
    if status == 200:
        assert r.content == AUDIO
        assert "content-range" not in r.headers
    elif status == 416:
        assert r.headers["content-range"] == f"bytes */{SIZE}"
    head = client.head(path, headers={"Range": header})
    assert head.status_code == status and head.content == b""


def test_media_multiple_ranges(client, episode):
    r = client.get(f"/media/talks/{episode.id}.mp3", headers={"Range": "bytes=0-1,5-6"})
    assert r.status_code == 206
    assert r.headers["content-type"].startswith("multipart/byteranges")
    assert AUDIO[0:2] in r.content and AUDIO[5:7] in r.content


def test_media_head(client, episode):
    path = f"/media/talks/{episode.id}.mp3"
    got = client.get(path)
    r = client.head(path)
    assert r.status_code == 200 and r.content == b""
    for name in ("content-type", "content-length", "accept-ranges", "cache-control", "etag"):
        assert r.headers[name] == got.headers[name], name
    r = client.head(path, headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == b""
    assert r.headers["content-range"] == f"bytes 0-9/{SIZE}"


def test_media_content_types(client, episode):
    for name, mime in (
        (f"{episode.id}.mp3", "audio/mpeg"),
        (f"{episode.id}.png", "image/png"),
        ("cover.png", "image/png"),
    ):
        r = client.get(f"/media/talks/{name}")
        assert r.status_code == 200, name
        assert r.headers["content-type"] == mime, name


def test_media_m4a_and_jpeg(client, svc, db, storage):
    folder = storage.feed_dir("talks")
    svc.set_feed_artwork("talks", b"\xff\xd8\xff\xe0" + b"j" * 10)
    ep = svc.add_episode("https://x.example/b.mp3")
    (folder / f"{ep.id}.m4a").write_bytes(AUDIO)
    (folder / f"{ep.id}.jpg").write_bytes(b"jpg")
    db.mark_ready(
        ep.id,
        title="T",
        description=None,
        artwork=f"{ep.id}.jpg",
        file=f"{ep.id}.m4a",
        mime="audio/mp4",
        size_bytes=SIZE,
        duration_s=None,
    )
    for name, mime in (
        (f"{ep.id}.m4a", "audio/mp4"),
        (f"{ep.id}.jpg", "image/jpeg"),
        ("cover.jpg", "image/jpeg"),
    ):
        r = client.get(f"/media/talks/{name}")
        assert (r.status_code, r.headers["content-type"]) == (200, mime), name


def test_media_cache_header(client, episode):
    for name in (f"{episode.id}.mp3", f"{episode.id}.png", "cover.png"):
        whole = client.get(f"/media/talks/{name}")
        part = client.get(f"/media/talks/{name}", headers={"Range": "bytes=0-1"})
        assert whole.headers["cache-control"] == "private, max-age=86400", name
        assert part.headers["cache-control"] == "private, max-age=86400", name


def test_media_ignores_the_query_string(client, episode):
    r = client.get("/media/talks/cover.png?v=123")
    assert r.status_code == 200 and r.content == PNG
    assert client.get(f"/media/talks/{episode.id}.png?v=1&x=%2e%2e").status_code == 200


def test_media_unknown_and_traversal(client):
    assert client.get("/media/talks/nope.mp3").status_code == 404
    assert client.get("/media/talks/..%2F..%2Fdata%2Fpodcast.sqlite").status_code == 404


def test_media_404_body(client, episode):
    for path in (
        "/media/talks/nope.mp3",
        "/media/nope/cover.png",
        f"/media/talks/{episode.id}.jpg",
        "/media/talks/%2e%2e",
        "/media/talks/..%5Ctalks%5Ccover.png",
        "/media/talks/cover.png%00",
    ):
        r = client.get(path)
        assert (r.status_code, r.text) == (404, "not found\n"), path
        assert r.headers["content-type"].startswith("text/plain"), path
        head = client.head(path)
        assert head.status_code == 404 and head.content == b"", path


@pytest.mark.parametrize(
    "path",
    [
        "/media/talks/..%2F..%2Fdata%2Fpodcast.sqlite",
        "/media/talks/%2e%2e%2f%2e%2e%2fdata%2fpodcast.sqlite",
        "/media/talks/..%2fCACHEDIR.TAG",
        "/media/..%2Ftalks%2Fcover.png",
        "/media/talks/..%5C..%5Cpodcast.sqlite",
        "/media/talks/podcast.sqlite",
        "/media/talks/podcast.sqlite-wal",
        "/media/talks/.cover.upload",
        "/media/talks/notes.txt",
        "/media/talks/CACHEDIR.TAG",
        "/media/.tmp/anything",
        "/media/CACHEDIR.TAG",
        "/media/talks/%2e",
        "/media/talks/%2e%2e",
        "/media/talks//etc/passwd",
        "/media/talks/%2Fetc%2Fpasswd",
        "/media/%2e%2e/talks/cover.png",
    ],
)
def test_media_exposes_nothing_else(client, storage, path):
    folder = storage.feed_dir("talks")
    (folder / "notes.txt").write_text("notes")  # in the folder, named by no row
    (folder / ".cover.upload").write_bytes(b"half a cover")
    for where in (storage.root, folder):  # the database is not in the media folder; if it were
        (where / "podcast.sqlite").write_bytes(b"SQLite format 3")
        (where / "podcast.sqlite-wal").write_bytes(b"SQLite format 3")
    r = client.get(path)
    assert r.status_code == 404, path
    assert b"SQLite" not in r.content and b"notes" not in r.content


def test_media_serves_only_what_the_database_names(client, svc, db, storage, episode):
    folder = storage.feed_dir("talks")
    queued = svc.add_episode("https://x.example/queued.mp3")
    (folder / f"{queued.id}.mp3").write_bytes(AUDIO)  # a file, but the episode is not ready
    (folder / f"{episode.id}.jpg").write_bytes(b"x")  # next to a real episode, but not its file
    assert client.get(f"/media/talks/{queued.id}.mp3").status_code == 404
    assert client.get(f"/media/talks/{episode.id}.jpg").status_code == 404
    assert client.get(f"/media/talks/{episode.id}.mp3").status_code == 200


def test_media_of_another_feed_is_not_served_under_this_slug(client, svc, db, storage):
    svc.create_feed("Other")
    ep = svc.add_episode("https://x.example/o.mp3", feed="other")
    other = storage.feed_dir("other")
    other.mkdir(parents=True)
    (other / f"{ep.id}.mp3").write_bytes(AUDIO)
    db.mark_ready(
        ep.id,
        title="T",
        description=None,
        artwork=None,
        file=f"{ep.id}.mp3",
        mime="audio/mpeg",
        size_bytes=SIZE,
        duration_s=None,
    )
    assert client.get(f"/media/other/{ep.id}.mp3").status_code == 200
    assert client.get(f"/media/talks/{ep.id}.mp3").status_code == 404


def test_media_file_deleted_after_publishing(client, storage, episode):
    (storage.feed_dir("talks") / f"{episode.id}.mp3").unlink()
    r = client.get(f"/media/talks/{episode.id}.mp3")
    assert (r.status_code, r.text) == (404, "not found\n")


def test_media_removed_episode_and_feed(client, svc, episode):
    svc.remove_episode(episode.id)
    assert client.get(f"/media/talks/{episode.id}.mp3").status_code == 404
    svc.delete_feed("talks")
    assert client.get("/media/talks/cover.png").status_code == 404
    assert client.get("/feeds/talks.xml").status_code == 404


# ---------------------------------------------------------------- nothing else is public


@pytest.mark.parametrize("path", ["/feeds/talks.xml", "/media/talks/cover.png"])
@pytest.mark.parametrize("method", ["post", "put", "delete", "patch", "options"])
def test_only_get_and_head(client, path, method):
    r = getattr(client, method)(path)
    assert r.status_code == 405
    assert set(r.headers["allow"].replace(" ", "").split(",")) == {"GET", "HEAD"}


def test_only_get(client):
    assert client.post("/feeds/talks.xml").status_code == 405
    assert client.get("/feeds/").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/feeds",
        "/feeds/",
        "/feeds/talks",
        "/feeds/talks.xml/more",
        "/feeds/talks.XML",
        "/feeds/.xml",
        "/media",
        "/media/",
        "/media/talks",
        "/media/talks/",
        "/ui/",
        "/ui/feeds",
        "/admin",
        "/health",
        "/data/podcast.sqlite",
        "/podcast.sqlite",
    ],
)
def test_no_other_route(client, path):
    assert client.get(path).status_code == 404
    assert client.post(path).status_code == 404


def test_the_public_routes_are_exactly_these_two(svc):
    routes = public_routes(svc)
    assert sorted((r.path, sorted(r.methods)) for r in routes) == [
        ("/feeds/{slug}.xml", ["GET", "HEAD"]),
        ("/media/{slug}/{name}", ["GET", "HEAD"]),
    ]


def test_the_handlers_are_sync_and_never_run_on_the_event_loop(client, svc, monkeypatch, episode):
    assert not any(inspect.iscoroutinefunction(r.endpoint) for r in public_routes(svc))
    seen = []

    def off_the_loop(method):
        def wrapper(*args):
            with pytest.raises(RuntimeError):  # there is no running loop in this thread
                asyncio.get_running_loop()
            seen.append(method.__name__)
            return method(*args)

        return wrapper

    monkeypatch.setattr(svc, "feed_document", off_the_loop(svc.feed_document))
    monkeypatch.setattr(svc, "media_file", off_the_loop(svc.media_file))
    assert client.get("/feeds/talks.xml").status_code == 200
    assert client.get(f"/media/talks/{episode.id}.mp3").status_code == 200
    assert seen == ["feed_document", "media_file"]
