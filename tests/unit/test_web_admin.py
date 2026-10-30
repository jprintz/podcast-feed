import re
import threading
from datetime import UTC, datetime
from html.parser import HTMLParser

import pytest
from starlette.routing import Route
from starlette.testclient import TestClient

from podcast_feed.config import Settings
from podcast_feed.db import Database
from podcast_feed.service import MAX_ARTWORK_BYTES, Service
from podcast_feed.storage import Storage
from podcast_feed.web import create_web_app
from podcast_feed.web_admin import admin_routes

HOST = "https://podcast.example.com"
FEED_URL = "https://podcast-api.example.com/s3cr3t/feeds/talks.xml"
NOW = datetime(2026, 10, 29, 12, 0, 0, tzinfo=UTC)
PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 20
FILE_URL = "https://files.example/private/dir/talk.mp3?token=abc123"
BAD_ARTWORK = "Artwork must be a PNG or JPEG under 5 MB."


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
            "PODCAST_PUBLIC_BASE": "https://podcast-api.example.com/s3cr3t",
            "PODCAST_DATA_DIR": str(tmp_path / "data"),
            "PODCAST_MEDIA_DIR": str(tmp_path / "media"),
        }
    )
    return Service(db, storage, settings, lambda: None)


@pytest.fixture
def client(svc):
    """The whole web app with no feed in it, reached under the admin's host name."""
    with TestClient(create_web_app(svc), base_url=HOST) as test_client:
        yield test_client


@pytest.fixture
def talks(svc):
    return svc.create_feed("Talks")


def post(client, path, data=None, **kwargs):
    """A same-origin POST, as a browser on the admin page sends it; redirects are not followed."""
    headers = {"Sec-Fetch-Site": "same-origin", **kwargs.pop("headers", {})}
    return client.post(path, data=data, headers=headers, follow_redirects=False, **kwargs)


class _Alerts(HTMLParser):
    """The text of every element that has role="alert"."""

    def __init__(self):
        super().__init__()
        self.found: list[str] = []
        self._tag: str | None = None
        self._text = ""

    def handle_starttag(self, tag, attrs):
        if ("role", "alert") in attrs and self._tag is None:
            self._tag, self._text = tag, ""

    def handle_data(self, data):
        if self._tag is not None:
            self._text += data

    def handle_endtag(self, tag):
        if tag == self._tag:
            self.found.append(self._text.strip())
            self._tag = None


def alerts(response) -> list[str]:
    parser = _Alerts()
    parser.feed(response.text)
    return parser.found


def add(client, url="https://x.example/a.mp3", title=None):
    data = {"url": url} if title is None else {"url": url, "title": title}
    response = post(client, "/ui/feeds/talks/episodes", data)
    assert response.status_code == 303
    return response


def episode_of(svc, index=0):
    return svc.list_episodes("talks")[index]


def finish(db, episode, title="Done"):
    assert db.mark_ready(
        episode.id,
        title=title,
        description=None,
        artwork=None,
        file=f"{episode.id}.mp3",
        mime="audio/mpeg",
        size_bytes=10,
        duration_s=1.0,
    )


# ------------------------------------------------------------------------ routing and feeds


def test_root_redirects(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/"


def test_create_feed(client, svc):
    response = post(client, "/ui/feeds", {"title": "Talks"})
    assert (response.status_code, response.headers["location"]) == (303, "/ui/feeds/talks")
    assert svc.get_feed("talks").title == "Talks"


def test_create_feed_with_every_field(client, svc):
    response = post(
        client,
        "/ui/feeds",
        {"title": "Talks", "slug": "my-talks", "description": "About talks", "retention_days": "7"},
    )
    assert response.headers["location"] == "/ui/feeds/my-talks"
    feed = svc.get_feed("my-talks")
    assert (feed.description, feed.retention_days) == ("About talks", 7)


def test_create_feed_with_artwork(client, svc):
    files = {"artwork": ("cover.png", PNG, "image/png")}
    response = post(client, "/ui/feeds", {"title": "Talks"}, files=files)
    assert (response.status_code, response.headers["location"]) == (303, "/ui/feeds/talks")
    assert svc.get_feed("talks").artwork == "cover.png"


def test_create_feed_without_choosing_a_file(client, svc):
    """A browser sends an empty file part when nothing was picked: that is no artwork, no error."""
    files = {"artwork": ("", b"", "application/octet-stream")}
    response = post(client, "/ui/feeds", {"title": "Talks"}, files=files)
    assert response.status_code == 303
    assert svc.get_feed("talks").artwork is None


def test_create_feed_with_bad_artwork_keeps_the_feed(client, svc):
    files = {"artwork": ("cover.png", b"not an image", "image/png")}
    response = post(client, "/ui/feeds", {"title": "Talks"}, files=files)
    assert response.status_code == 400
    assert BAD_ARTWORK in alerts(response)[0]
    # The feed exists and its page, which is what is shown, has the artwork form to try again.
    assert svc.get_feed("talks").artwork is None
    assert 'action="/ui/feeds/talks/artwork"' in response.text


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"title": "  "}, "A feed needs a title."),
        (
            {"title": "X", "slug": "Bad Slug"},
            "Slugs are 1 to 40 lowercase letters, digits and dashes.",
        ),
        ({"title": "X", "retention_days": "0"}, "Retention must be at least 1 day."),
        ({"title": "X", "retention_days": "-3"}, "Retention must be at least 1 day."),
        ({"title": "X", "retention_days": "soon"}, "Retention must be at least 1 day."),
        ({"title": "X", "retention_days": "1_0"}, "Retention must be at least 1 day."),
        ({"title": "X", "retention_days": "3651"}, "Retention can be at most 3650 days."),
        ({"title": "X", "retention_days": "9" * 5000}, "Retention can be at most 3650 days."),
    ],
)
def test_create_feed_errors_show_the_message(client, svc, data, message):
    response = post(client, "/ui/feeds", data)
    assert response.status_code == 400
    assert alerts(response) == [message]
    assert svc.list_feeds() == []


def test_create_feed_keeps_what_was_typed_after_an_error(client):
    response = post(
        client, "/ui/feeds", {"title": "My show", "slug": "Bad Slug", "description": "d"}
    )
    assert response.status_code == 400
    assert 'value="My show"' in response.text
    assert 'value="Bad Slug"' in response.text


def test_slug_taken(client, talks):
    response = post(client, "/ui/feeds", {"title": "Other", "slug": "talks"})
    assert response.status_code == 400
    assert alerts(response) == ["The slug 'talks' is taken."]


def test_a_file_in_a_text_field_is_no_title(client):
    response = post(client, "/ui/feeds", files={"title": ("t.txt", b"Talks", "text/plain")})
    assert response.status_code == 400
    assert alerts(response) == ["A feed needs a title."]


def test_feeds_page_lists_feeds(client, svc, talks):
    svc.create_feed("Other")
    page = client.get("/ui/")
    assert page.status_code == 200
    assert 'href="/ui/feeds/talks"' in page.text
    assert 'href="/ui/feeds/other"' in page.text
    assert 'action="/ui/feeds"' in page.text  # the create form
    assert "default" in page.text.lower()


def test_feeds_page_without_feeds(client):
    page = client.get("/ui/")
    assert page.status_code == 200
    assert "No feeds yet" in page.text
    assert 'action="/ui/feeds"' in page.text


def test_feed_url_only_on_the_feed_page(client, talks):
    add(client)
    assert "s3cr3t" not in client.get("/ui/").text
    assert "s3cr3t" not in client.get("/ui/feeds/talks/episodes").text
    assert "s3cr3t" in client.get("/ui/feeds/talks").text


def test_feed_page_shows_feed_url(client, talks):
    page = client.get("/ui/feeds/talks")
    assert page.status_code == 200
    assert FEED_URL in page.text
    # The copy button reads the address from a data attribute; the page has no inline script.
    assert f'data-copy="{FEED_URL}"' in page.text
    assert not re.search(r"<script(?![^>]*\bsrc=)", page.text)
    assert 'src="/ui/static/htmx.min.js"' in page.text


def test_feed_page_forms(client, talks):
    page = client.get("/ui/feeds/talks").text
    for action in ("episodes", "settings", "artwork", "delete"):
        assert f'action="/ui/feeds/talks/{action}"' in page
    assert 'enctype="multipart/form-data"' in page
    # Talks is the default feed already, so there is nothing to make default.
    assert "/ui/feeds/talks/default" not in page


def test_make_default(client, svc, talks):
    svc.create_feed("Other")
    assert 'action="/ui/feeds/other/default"' in client.get("/ui/feeds/other").text
    response = post(client, "/ui/feeds/other/default")
    assert (response.status_code, response.headers["location"]) == (303, "/ui/feeds/other")
    assert svc.get_feed("other").is_default
    assert not svc.get_feed("talks").is_default


def test_settings(client, svc, talks):
    response = post(
        client,
        "/ui/feeds/talks/settings",
        {"title": "New title", "description": "New text", "retention_days": "14"},
    )
    assert (response.status_code, response.headers["location"]) == (303, "/ui/feeds/talks")
    feed = svc.get_feed("talks")
    assert (feed.title, feed.description, feed.retention_days) == ("New title", "New text", 14)
    # An empty retention goes back to the default.
    post(client, "/ui/feeds/talks/settings", {"title": "New title", "retention_days": ""})
    assert svc.get_feed("talks").retention_days is None


def test_settings_errors_keep_the_typed_values(client, svc, talks):
    response = post(
        client,
        "/ui/feeds/talks/settings",
        {"title": " ", "description": "Typed text", "retention_days": "99999"},
    )
    assert response.status_code == 400
    assert alerts(response) == ["A feed needs a title."]
    assert "Typed text" in response.text
    assert svc.get_feed("talks").title == "Talks"


def test_artwork(client, svc, talks):
    files = {"artwork": ("c.png", PNG, "image/png")}
    response = post(client, "/ui/feeds/talks/artwork", files=files)
    assert (response.status_code, response.headers["location"]) == (303, "/ui/feeds/talks")
    assert svc.get_feed("talks").artwork == "cover.png"


@pytest.mark.parametrize(
    "files",
    [
        {"artwork": ("c.gif", b"GIF89a....", "image/gif")},
        {"artwork": ("", b"", "application/octet-stream")},  # no file chosen
        {"other": ("c.png", PNG, "image/png")},  # no artwork part at all
    ],
)
def test_artwork_errors(client, svc, talks, files):
    response = post(client, "/ui/feeds/talks/artwork", files=files)
    assert response.status_code == 400
    assert alerts(response) == [BAD_ARTWORK]
    assert svc.get_feed("talks").artwork is None


def _spy_on_artwork(monkeypatch, svc):
    sizes: list[int] = []
    real = svc.set_feed_artwork

    def spy(slug, data):
        sizes.append(len(data))
        return real(slug, data)

    monkeypatch.setattr(svc, "set_feed_artwork", spy)
    return sizes


def test_artwork_upload_is_read_only_up_to_the_limit_plus_one(client, svc, talks, monkeypatch):
    sizes = _spy_on_artwork(monkeypatch, svc)
    big = PNG + b"x" * (6 * 1024 * 1024)
    response = post(
        client, "/ui/feeds/talks/artwork", files={"artwork": ("c.png", big, "image/png")}
    )
    assert response.status_code == 400
    assert alerts(response) == [BAD_ARTWORK]  # the service's own message
    assert sizes == [MAX_ARTWORK_BYTES + 1]
    assert svc.get_feed("talks").artwork is None


def test_artwork_of_exactly_the_limit_is_accepted(client, svc, talks, monkeypatch):
    sizes = _spy_on_artwork(monkeypatch, svc)
    exact = PNG + b"x" * (MAX_ARTWORK_BYTES - len(PNG))
    response = post(
        client, "/ui/feeds/talks/artwork", files={"artwork": ("c.png", exact, "image/png")}
    )
    assert response.status_code == 303
    assert sizes == [MAX_ARTWORK_BYTES]


def test_new_feed_artwork_is_capped_too(client, svc, monkeypatch):
    sizes = _spy_on_artwork(monkeypatch, svc)
    big = PNG + b"x" * (6 * 1024 * 1024)
    response = post(client, "/ui/feeds", {"title": "Talks"}, files={"artwork": ("c.png", big)})
    assert response.status_code == 400
    assert alerts(response)[0].endswith(BAD_ARTWORK)
    assert sizes == [MAX_ARTWORK_BYTES + 1]


# --------------------------------------------------------------------------------- episodes


def test_add_episode(client, svc, talks):
    response = add(client, "https://x.example/a.mp3", "My talk")
    assert response.headers["location"] == "/ui/feeds/talks"
    fragment = client.get("/ui/feeds/talks/episodes")
    assert fragment.status_code == 200
    assert "My talk" in fragment.text
    assert "Queued" in fragment.text
    assert [e.status for e in svc.list_episodes("talks")] == ["queued"]


def test_fragment_is_a_fragment_the_page_includes(client, talks):
    add(client)
    fragment = client.get("/ui/feeds/talks/episodes").text
    page = client.get("/ui/feeds/talks").text
    assert "<html" not in fragment
    assert 'id="episodes"' in fragment
    assert 'id="episodes"' in page
    assert "x.example" in fragment and "x.example" in page


def test_episode_without_title_shows_the_source_host_and_never_the_link(client, talks):
    add(client, FILE_URL)
    for text in (client.get("/ui/feeds/talks").text, client.get("/ui/feeds/talks/episodes").text):
        assert "files.example" in text
        for part in ("private/dir", "talk.mp3", "token=abc123", "abc123", FILE_URL):
            assert part not in text
        assert not re.search(r'href="https?:', text)  # a source link is never a link


def test_episode_with_title_shows_the_title(client, talks):
    add(client, FILE_URL, "Great talk")
    assert "Great talk" in client.get("/ui/feeds/talks/episodes").text


def test_add_episode_to_unknown_feed(client):
    response = post(client, "/ui/feeds/nope/episodes", {"url": "https://x.example/a.mp3"})
    assert response.status_code == 404
    assert alerts(response) == ["Unknown feed 'nope'. Call list_feeds for the slugs."]


def test_bad_url_message(client, svc, talks):
    response = post(client, "/ui/feeds/talks/episodes", {"url": "ftp://x.example/a.mp3"})
    assert response.status_code == 400
    assert "Only http and https links" in response.text
    assert alerts(response) == ["Only http and https links to a file work."]
    assert 'value="ftp://x.example/a.mp3"' in response.text  # so a typo can be fixed
    assert svc.list_episodes("talks") == []


@pytest.mark.parametrize("status", ["queued", "downloading", "converting"])
def test_table_polls_while_unfinished(client, svc, db, talks, status):
    add(client)
    db.set_status(episode_of(svc).id, status)
    fragment = client.get("/ui/feeds/talks/episodes").text
    assert 'hx-trigger="every 3s"' in fragment
    assert 'hx-get="/ui/feeds/talks/episodes"' in fragment
    assert 'hx-swap="outerHTML"' in fragment
    assert 'hx-trigger="every 3s"' in client.get("/ui/feeds/talks").text


def test_table_polls_only_while_unfinished(client, svc, db, talks):
    add(client, "https://x.example/a.mp3")
    add(client, "https://x.example/b.mp3")
    assert 'hx-trigger="every 3s"' in client.get("/ui/feeds/talks/episodes").text
    finish(db, episode_of(svc, 0))
    db.set_status(episode_of(svc, 1).id, "failed", "timed out")  # failed is finished too
    assert "hx-trigger" not in client.get("/ui/feeds/talks/episodes").text
    assert "hx-trigger" not in client.get("/ui/feeds/talks").text


def test_failed_episode_shows_why_and_offers_retry(client, svc, db, talks):
    add(client)
    db.set_status(episode_of(svc).id, "failed", "too large")
    fragment = client.get("/ui/feeds/talks/episodes").text
    assert "Failed" in fragment and "too large" in fragment
    assert f'action="/ui/episodes/{episode_of(svc).id}/retry"' in fragment


def test_only_failed_episodes_offer_retry(client, svc, talks):
    add(client)
    fragment = client.get("/ui/feeds/talks/episodes").text
    assert "/retry" not in fragment
    assert f'action="/ui/episodes/{episode_of(svc).id}/edit"' in fragment
    assert f'action="/ui/episodes/{episode_of(svc).id}/remove"' in fragment


def test_titles_escaped(client, svc, talks):
    add(client, "https://x.example/a.mp3", "<script>alert(1)</script>")
    svc.update_episode(episode_of(svc).id, "<script>alert(1)</script>", "</textarea><b>boo</b>")
    for text in (client.get("/ui/feeds/talks").text, client.get("/ui/feeds/talks/episodes").text):
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
        assert "<script>alert(1)" not in text
        assert "<b>boo" not in text
        assert "&lt;/textarea&gt;&lt;b&gt;boo&lt;/b&gt;" in text


def test_feed_titles_and_unknown_slugs_escaped(client, svc):
    svc.create_feed("<img src=x onerror=alert(1)>", slug="evil")
    assert "<img src=x" not in client.get("/ui/").text
    assert "&lt;img src=x onerror=alert(1)&gt;" in client.get("/ui/").text
    assert "<img src=x" not in client.get("/ui/feeds/evil").text
    missing = client.get("/ui/feeds/%3Cb%3Ehi")  # a slug of <b>hi
    assert missing.status_code == 404
    assert "<b>hi" not in missing.text
    assert "&lt;b&gt;hi" in missing.text


def test_edit_episode(client, svc, talks):
    add(client, "https://x.example/a.mp3", "Old")
    episode = episode_of(svc)
    response = post(client, f"/ui/episodes/{episode.id}/edit", {"title": "New", "description": "D"})
    assert (response.status_code, response.headers["location"]) == (303, "/ui/feeds/talks")
    updated = svc.get_episode(episode.id)
    assert (updated.title, updated.description) == ("New", "D")
    # A blank title means no title: the service takes it, and the table falls back to the host.
    post(client, f"/ui/episodes/{episode.id}/edit", {"title": "  ", "description": ""})
    assert svc.get_episode(episode.id).title is None
    assert "x.example" in client.get("/ui/feeds/talks/episodes").text


def test_retry_episode(client, svc, db, talks):
    add(client)
    episode = episode_of(svc)
    db.set_status(episode.id, "failed", "timed out")
    response = post(client, f"/ui/episodes/{episode.id}/retry")
    assert (response.status_code, response.headers["location"]) == (303, "/ui/feeds/talks")
    assert svc.get_episode(episode.id).status == "queued"


def test_retry_of_an_episode_that_did_not_fail(client, svc, talks):
    add(client)
    episode = episode_of(svc)
    response = post(client, f"/ui/episodes/{episode.id}/retry")
    assert response.status_code == 400
    assert alerts(response) == ["Only failed episodes can be retried."]
    assert 'id="episodes"' in response.text  # the feed page of that episode
    assert svc.get_episode(episode.id).status == "queued"


def test_remove_episode(client, svc, talks):
    add(client)
    episode = episode_of(svc)
    response = post(client, f"/ui/episodes/{episode.id}/remove")
    assert (response.status_code, response.headers["location"]) == (303, "/ui/feeds/talks")
    assert svc.list_episodes("talks") == []


@pytest.mark.parametrize("action", ["edit", "retry", "remove"])
def test_unknown_episode(client, action):
    response = post(client, f"/ui/episodes/nope/{action}", {"title": "T"})
    assert response.status_code == 404
    assert alerts(response) == ["No episode 'nope'."]
    assert 'href="/ui/"' in response.text


@pytest.mark.parametrize("action", ["edit", "retry", "remove"])
def test_episode_actions_redirect_to_the_episodes_feed_whatever_the_form_says(
    client, svc, db, talks, action
):
    add(client)
    episode = episode_of(svc)
    db.set_status(episode.id, "failed", "timed out")
    form = {"title": "T", "next": "https://evil.example/", "redirect": "//evil.example"}
    response = post(client, f"/ui/episodes/{episode.id}/{action}", form)
    assert response.headers["location"] == "/ui/feeds/talks"


# ----------------------------------------------------------------------------- deleting a feed


def test_delete_needs_typed_slug(client, svc, talks):
    add(client)
    for confirm in ("x", "Talks", "talks ", ""):
        response = post(client, "/ui/feeds/talks/delete", {"confirm": confirm})
        assert response.status_code == 400
        assert alerts(response) and "talks" in alerts(response)[0]
        assert 'id="episodes"' in response.text  # the feed page again
        assert svc.get_feed("talks")
        assert len(svc.list_episodes("talks")) == 1
    response = post(client, "/ui/feeds/talks/delete")  # no field at all
    assert response.status_code == 400
    response = post(client, "/ui/feeds/talks/delete", {"confirm": "talks"})
    assert (response.status_code, response.headers["location"]) == (303, "/ui/")
    assert svc.list_feeds() == []


def test_delete_unknown_feed(client):
    response = post(client, "/ui/feeds/nope/delete", {"confirm": "nope"})
    assert response.status_code == 404
    response = post(client, "/ui/feeds/nope/delete", {"confirm": "wrong"})
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/ui/feeds/nope"),
        ("get", "/ui/feeds/nope/episodes"),
        ("post", "/ui/feeds/nope/settings"),
        ("post", "/ui/feeds/nope/artwork"),
        ("post", "/ui/feeds/nope/default"),
        ("post", "/ui/feeds/nope/delete"),
    ],
)
def test_unknown_feed_is_a_small_not_found_page(client, method, path):
    if method == "get":
        response = client.get(path)
    else:
        response = post(client, path, {"title": "T", "confirm": "nope"})
    assert response.status_code == 404
    assert alerts(response) == ["Unknown feed 'nope'. Call list_feeds for the slugs."]
    if "episodes" not in path:  # the fragment has no page around it
        assert 'href="/ui/"' in response.text


# ---------------------------------------------------------------------------- same-origin rule


@pytest.mark.parametrize(
    "headers", [{}, {"Sec-Fetch-Site": "cross-site"}, {"Origin": "https://evil.example"}]
)
def test_cross_site_posts_refused(client, svc, headers):
    response = client.post("/ui/feeds", data={"title": "X"}, headers=headers)
    assert response.status_code == 403
    assert response.text == "cross-site request refused\n"
    assert svc.list_feeds() == []


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "same-site"},
        {"Sec-Fetch-Site": "none"},
        {"Sec-Fetch-Site": "cross-site", "Origin": HOST},  # the browser's word comes first
        {"Origin": "https://podcast.example.com.evil.example"},
        {"Origin": "https://evil@podcast.example.com"},
        {"Origin": "https://evil.example/podcast.example.com"},
        {"Origin": "null"},
        {"Origin": ""},
        {"Origin": "https://["},  # not even a URL
    ],
)
def test_other_ways_to_say_it_is_not_same_origin(client, svc, headers):
    response = client.post("/ui/feeds", data={"title": "X"}, headers=headers)
    assert response.status_code == 403
    assert svc.list_feeds() == []


def test_origin_without_fetch_metadata(client):
    response = client.post(
        "/ui/feeds",
        data={"title": "X"},
        headers={"Origin": "https://podcast.example.com"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_origin_is_compared_by_host_not_scheme(client):
    """Behind the tunnel the app sees http while the browser's Origin is https."""
    response = client.post(
        "/ui/feeds",
        data={"title": "X"},
        headers={"Origin": "http://PODCAST.example.com"},
        follow_redirects=False,
    )
    assert response.status_code == 303


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_every_unsafe_method_is_checked(client, talks, method):
    assert client.request(method, "/ui/feeds/talks").status_code == 403
    same_origin = {"Sec-Fetch-Site": "same-origin"}
    assert client.request(method, "/ui/feeds/talks", headers=same_origin).status_code == 405


def test_reading_is_not_restricted(client, talks):
    assert client.get("/ui/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200
    assert client.head("/ui/", headers={"Origin": "https://evil.example"}).status_code == 200
    assert client.options("/ui/", headers={"Origin": "https://evil.example"}).status_code != 403


# --------------------------------------------------------------------------- the public routes


def test_public_prefixes_in_full_app(client, talks):
    assert client.post("/feeds/talks.xml").status_code in (403, 405)
    assert client.post("/feeds/talks.xml").status_code == 403  # no browser says it is same-origin
    assert post(client, "/feeds/talks.xml", {}).status_code == 405
    assert post(client, "/media/talks/cover.png", {}).status_code == 405
    assert client.get("/feeds/").status_code == 404


def test_public_routes_are_served(client, svc, db, talks):
    add(client)
    finish(db, episode_of(svc))
    feed = client.get("/feeds/talks.xml")
    assert feed.status_code == 200
    assert feed.headers["content-type"].startswith("application/rss+xml")


def test_no_redirect_for_a_trailing_slash(client, talks):
    """A redirect from the app would point at its internal host name, not the public one."""
    for path in ("/feeds/talks.xml/", "/media/talks/x.mp3/", "/ui", "/ui/feeds/talks/"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 404, path
        assert "location" not in response.headers


def test_nothing_but_reading_outside_the_admin_prefix(svc):
    app = create_web_app(svc)
    outside = [r for r in app.routes if isinstance(r, Route) and not r.path.startswith("/ui")]
    assert {r.path for r in outside} == {"/", "/feeds/{slug}.xml", "/media/{slug}/{name}"}
    for route in outside:
        assert route.methods <= {"GET", "HEAD"}, route.path
    assert all(r.path.startswith("/ui/") for r in admin_routes(svc))


# ----------------------------------------------------------------------------- headers and files


def test_admin_html_is_never_cached(client, svc, db, talks):
    add(client)
    db.set_status(episode_of(svc).id, "failed", "timed out")
    episode_id = episode_of(svc).id
    responses = [
        client.get("/ui/"),
        client.get("/ui/feeds/talks"),
        client.get("/ui/feeds/talks/episodes"),
        client.get("/ui/feeds/nope"),  # 404 page
        client.get("/ui/feeds/nope/episodes"),
        post(client, "/ui/feeds", {"title": " "}),  # 400 page
        post(client, "/ui/feeds", {"title": "Other"}),  # 303
        post(client, f"/ui/episodes/{episode_id}/retry"),  # 303
        post(client, f"/ui/episodes/{episode_id}/retry"),  # 400 (it is queued now)
        post(client, "/ui/episodes/nope/remove"),  # 404
    ]
    assert [r.status_code for r in responses] == [200, 200, 200, 404, 404, 400, 303, 303, 400, 404]
    for response in responses:
        assert response.headers["cache-control"] == "no-store", response.request.url


def test_admin_pages_and_redirects_forbid_framing(client, svc, talks):
    # The pages show the secret feed address, so no other site may frame them (clickjacking).
    episode = svc.add_episode("https://x.example/a.mp3", feed="talks")
    responses = [
        client.get("/ui/"),
        client.get("/ui/feeds/talks"),
        client.get("/ui/feeds/talks/episodes"),
        client.get("/ui/feeds/nope"),  # 404 page
        post(client, "/ui/feeds", {"title": " "}),  # 400 page
        post(client, "/ui/feeds", {"title": "Other"}),  # 303
        post(client, f"/ui/episodes/{episode.id}/remove"),  # 303
        post(client, "/ui/episodes/nope/remove"),  # 404 page
    ]
    assert [r.status_code for r in responses] == [200, 200, 200, 404, 400, 303, 303, 404]
    for response in responses:
        where = (response.status_code, response.request.url)
        assert response.headers["x-frame-options"] == "DENY", where
        assert response.headers["content-security-policy"] == "frame-ancestors 'none'", where
        assert response.headers["x-content-type-options"] == "nosniff", where
        assert response.headers["cache-control"] == "no-store", where  # still there


def test_static_files(client):
    htmx = client.get("/ui/static/htmx.min.js")
    assert htmx.status_code == 200
    first_line = htmx.text.splitlines()[0]
    assert first_line.startswith("/*") and "htmx 2.0." in first_line and "0BSD" in first_line
    assert client.get("/ui/static/app.js").status_code == 200
    assert "Zero-Clause BSD" in client.get("/ui/static/htmx-LICENSE.txt").text
    assert client.get("/ui/static/nope.js").status_code == 404
    # Not HTML pages: they may be cached.
    assert "no-store" not in htmx.headers.get("cache-control", "")


def test_pages_load_the_scripts_from_the_static_mount(client):
    page = client.get("/ui/").text
    assert 'src="/ui/static/htmx.min.js"' in page
    assert 'src="/ui/static/app.js"' in page


# -------------------------------------------------------------------- the event loop stays free


class _Spy:
    """The service, noting the thread each call runs in."""

    def __init__(self, service):
        self._service = service
        self.threads: set[int] = set()
        self.calls: set[str] = set()

    def __getattr__(self, name):
        attribute = getattr(self._service, name)
        if not callable(attribute):
            return attribute

        def call(*args, **kwargs):
            self.threads.add(threading.get_ident())
            self.calls.add(name)
            return attribute(*args, **kwargs)

        return call


def test_service_calls_never_run_on_the_event_loop(svc, db):
    spy = _Spy(svc)
    loop_threads: set[int] = set()
    app = create_web_app(spy)

    async def watched(scope, receive, send):
        loop_threads.add(threading.get_ident())
        await app(scope, receive, send)

    with TestClient(watched, base_url=HOST) as client:
        post(
            client, "/ui/feeds", {"title": "Talks"}, files={"artwork": ("c.png", PNG, "image/png")}
        )
        post(client, "/ui/feeds", {"title": "Other"})
        client.get("/ui/")
        client.get("/ui/feeds/talks")
        client.get("/ui/feeds/talks/episodes")
        add(client, "https://x.example/a.mp3")
        add(client, "https://x.example/b.mp3")
        first, second = (e.id for e in svc.list_episodes("talks"))
        db.set_status(first, "failed", "timed out")
        post(client, f"/ui/episodes/{first}/edit", {"title": "T"})
        post(client, f"/ui/episodes/{first}/retry")
        post(client, f"/ui/episodes/{second}/remove")
        post(client, "/ui/feeds/talks/settings", {"title": "Talks"})
        post(client, "/ui/feeds/talks/artwork", files={"artwork": ("c.png", PNG, "image/png")})
        post(client, "/ui/feeds/other/default")
        post(client, "/ui/feeds/talks/delete", {"confirm": "x"})  # shows the page again
        post(client, "/ui/feeds/talks/delete", {"confirm": "talks"})
        post(client, "/ui/feeds/nope/default")  # the failures too
        post(client, "/ui/episodes/nope/retry")

    # Every service method the admin uses was exercised, and none of them ran on the loop.
    assert spy.calls >= {
        "list_feeds", "create_feed", "get_feed", "set_feed_artwork", "feed_url", "list_episodes",
        "add_episode", "get_episode", "update_episode", "retry_episode", "remove_episode",
        "update_feed", "set_default", "delete_feed",
    }  # fmt: skip
    assert loop_threads and spy.threads
    assert spy.threads.isdisjoint(loop_threads)
