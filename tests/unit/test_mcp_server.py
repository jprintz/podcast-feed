"""The MCP server, through an in-memory client: tools, results, errors and logging."""

import asyncio
import json
import logging
import re
import threading
from datetime import UTC, datetime

import pytest
from mcp import Client

from podcast_feed.config import GIB, Settings
from podcast_feed.db import Database
from podcast_feed.mcp_server import INSTRUCTIONS, _run, create_mcp
from podcast_feed.service import Service
from podcast_feed.storage import Storage

SECRET = "s3cr3t"
# A link whose host, path and query would all show if the server ever echoed it.
HOST = "leaky-cdn.example"
LINK = f"https://{HOST}/private/talk-0042.mp3?token=hunter2"
URL = "https://x.example/a.mp3"

# name: (title, read only, destructive, idempotent, open world), the brief's table.
TOOLS = {
    "list_feeds": ("List podcast feeds", True, False, True, False),
    "add_episode": ("Add an episode from a file link", False, False, True, True),
    "list_episodes": ("List episodes", True, False, True, False),
    "get_episode": ("Get an episode's status", True, False, True, False),
    "remove_episode": ("Remove an episode", False, True, True, False),
}
EPISODE_KEYS = {"id", "feed", "title", "description", "status", "error", "added_at", "duration_s"}
FEED_KEYS = {"slug", "title", "description", "episode_count", "retention_days", "default"}
# What the SDK puts in front of a failed tool's own message.
PREFIX = "Error executing tool {}: "
# Text an exception might carry from a link. It is a constant so that it is not a literal in the
# source line of a frame (which a traceback shows).
LEAK = "SECRETTOKEN link"
LOG_LINE = re.compile(r"tool=(\w+) target=(\S+) outcome=(\S+) ms=\d+")


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
    return []


@pytest.fixture
def svc(db, storage, tmp_path, wakes):
    settings = Settings(
        public_base=f"https://podcast-api.example.com/{SECRET}",
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


def data(result) -> dict:
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


def error_text(result) -> str:
    assert result.is_error
    return result.content[0].text


def publish(
    db: Database, storage: Storage, episode_id: str, slug: str, title: str = "Ready"
) -> None:
    """What the worker does when it is done: files in the feed's folder, then mark_ready."""
    folder = storage.feed_dir(slug)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{episode_id}.mp3").write_bytes(b"audio")
    (folder / f"{episode_id}.png").write_bytes(b"art")
    assert db.mark_ready(
        episode_id,
        title=title,
        description="Show notes",
        artwork=f"{episode_id}.png",
        file=f"{episode_id}.mp3",
        mime="audio/mpeg",
        size_bytes=5,
        duration_s=61.5,
    )


def mcp_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "podcast_feed.mcp"]


# ------------------------------------------------------------------------- the tools


async def test_tools_and_annotations(svc):
    async with Client(create_mcp(svc)) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert set(tools) == set(TOOLS)
        for name, expected in TOOLS.items():
            a = tools[name].annotations
            assert a is not None, name
            assert (
                a.title,
                a.read_only_hint,
                a.destructive_hint,
                a.idempotent_hint,
                a.open_world_hint,
            ) == expected, name
            assert tools[name].title == expected[0], name
        add = tools["add_episode"].annotations
        assert (add.read_only_hint, add.destructive_hint) == (False, False)
        assert (add.idempotent_hint, add.open_world_hint) == (True, True)
        assert tools["remove_episode"].annotations.destructive_hint is True
        assert all(
            None
            not in (
                t.annotations.title,
                t.annotations.read_only_hint,
                t.annotations.destructive_hint,
                t.annotations.idempotent_hint,
                t.annotations.open_world_hint,
            )
            for t in tools.values()
        )


async def test_instructions_are_the_briefs(svc):
    async with Client(create_mcp(svc)) as c:
        assert c.instructions == INSTRUCTIONS
        assert c.instructions.startswith(
            "Manages julius's private podcast feeds. add_episode takes a"
        )
        assert c.instructions.endswith("Feed URLs are never returned here.")
        assert (
            "'queued'" in c.instructions
            and "get_episode until it is 'ready' or 'failed'" in c.instructions
        )


async def test_tools_are_described(svc):
    async with Client(create_mcp(svc)) as c:
        for tool in (await c.list_tools()).tools:
            assert tool.description, tool.name
            for name, schema in tool.input_schema.get("properties", {}).items():
                assert schema.get("description"), f"{tool.name}.{name}"


async def test_input_schemas(svc):
    async with Client(create_mcp(svc)) as c:
        tools = {t.name: t.input_schema for t in (await c.list_tools()).tools}
        assert tools["list_feeds"].get("properties", {}) == {}
        add = tools["add_episode"]
        assert set(add["properties"]) == {"url", "feed", "title", "description"}
        assert add["required"] == ["url"]
        assert set(tools["list_episodes"]["properties"]) == {"feed", "status", "limit"}
        limit = tools["list_episodes"]["properties"]["limit"]
        assert (limit["default"], limit["minimum"], limit["maximum"]) == (50, 1, 500)
        assert tools["list_episodes"].get("required", []) == []
        for name in ("get_episode", "remove_episode"):
            assert set(tools[name]["properties"]) == {"episode_id"}
            assert tools[name]["required"] == ["episode_id"]


# ------------------------------------------------------------------------ feeds


async def test_list_feeds(svc, db, storage):
    async with Client(create_mcp(svc)) as c:
        svc.create_feed("Talks", description="Conference talks")
        svc.create_feed("Other", retention_days=7)
        ep = svc.add_episode(URL, "talks")
        svc.add_episode("https://x.example/b.mp3", "talks")  # still queued: not counted
        publish(db, storage, ep.id, "talks")
        result = await c.call_tool("list_feeds")
        assert data(result) == {
            "feeds": [
                {
                    "slug": "talks",
                    "title": "Talks",
                    "description": "Conference talks",
                    "episode_count": 1,
                    "retention_days": 30,  # the global default applies
                    "default": True,
                },
                {
                    "slug": "other",
                    "title": "Other",
                    "description": None,
                    "episode_count": 0,
                    "retention_days": 7,
                    "default": False,
                },
            ]
        }
        assert result.structured_content == data(result)


async def test_list_feeds_without_feeds_is_empty(svc):
    async with Client(create_mcp(svc)) as c:
        assert data(await c.call_tool("list_feeds")) == {"feeds": []}


# ---------------------------------------------------------------------- episodes


async def test_add_and_get(svc, wakes):
    async with Client(create_mcp(svc)) as c:
        svc.create_feed("Talks")
        added = data(await c.call_tool("add_episode", {"url": URL}))
        assert added["status"] == "queued"
        assert added["feed"] == "talks"
        assert wakes == [None]  # the service woke the worker
        assert data(await c.call_tool("get_episode", {"episode_id": added["id"]})) == added


async def test_episode_result_keys_and_values(svc, db):
    async with Client(create_mcp(svc)) as c:
        svc.create_feed("Talks")
        added = data(await c.call_tool("add_episode", {"url": URL}))
        assert set(added) == EPISODE_KEYS
        assert added["title"] is None and added["description"] is None
        assert added["error"] is None and added["duration_s"] is None
        stamp = datetime.fromisoformat(added["added_at"])
        assert stamp.tzinfo is not None and stamp.utcoffset().total_seconds() == 0
        assert abs((datetime.now(UTC) - stamp).total_seconds()) < 60
        assert added["added_at"] == svc.get_episode(added["id"]).added_at.isoformat()

        db.set_status(added["id"], "failed", "timed out")
        failed = data(await c.call_tool("get_episode", {"episode_id": added["id"]}))
        assert set(failed) == EPISODE_KEYS
        assert (failed["status"], failed["error"]) == ("failed", "timed out")


async def test_ready_episode_has_its_duration_and_no_files(svc, db, storage):
    async with Client(create_mcp(svc)) as c:
        svc.create_feed("Talks")
        added = data(await c.call_tool("add_episode", {"url": URL}))
        publish(db, storage, added["id"], "talks", title="Ready one")
        got = data(await c.call_tool("get_episode", {"episode_id": added["id"]}))
        assert set(got) == EPISODE_KEYS
        assert got["status"] == "ready" and got["title"] == "Ready one"
        assert got["description"] == "Show notes" and got["duration_s"] == 61.5
        dump = json.dumps(got)
        for leak in (".mp3", ".png", "audio/mpeg", URL, "x.example"):
            assert leak not in dump, leak


async def test_add_passes_feed_title_and_description_on(svc):
    async with Client(create_mcp(svc)) as c:
        svc.create_feed("Talks")
        svc.create_feed("Other")
        added = data(
            await c.call_tool(
                "add_episode",
                {"url": URL, "feed": "other", "title": "  A talk ", "description": "Notes"},
            )
        )
        assert (added["feed"], added["title"], added["description"]) == ("other", "A talk", "Notes")
        stored = svc.get_episode(added["id"])
        assert (stored.feed_slug, stored.title, stored.description) == ("other", "A talk", "Notes")
        assert stored.source_ref == URL  # the link is stored, never returned


async def test_add_the_same_link_again_gives_the_same_episode(svc, wakes):
    async with Client(create_mcp(svc)) as c:
        svc.create_feed("Talks")
        first = data(await c.call_tool("add_episode", {"url": URL}))
        second = data(await c.call_tool("add_episode", {"url": URL, "title": "Ignored"}))
        assert second["id"] == first["id"] and second["title"] is None
        assert wakes == [None]


async def test_list_episodes_filters_and_orders(svc, db, storage):
    async with Client(create_mcp(svc)) as c:
        svc.create_feed("Talks")
        svc.create_feed("Other")
        first = svc.add_episode("https://x.example/1.mp3", "talks")
        second = svc.add_episode("https://x.example/2.mp3", "talks")
        third = svc.add_episode("https://x.example/3.mp3", "other")
        publish(db, storage, first.id, "talks")

        everything = data(await c.call_tool("list_episodes"))["episodes"]
        assert [e["id"] for e in everything] == [third.id, second.id, first.id]  # newest first
        assert all(set(e) == EPISODE_KEYS for e in everything)

        talks = data(await c.call_tool("list_episodes", {"feed": "talks"}))["episodes"]
        assert [e["id"] for e in talks] == [second.id, first.id]
        assert {e["feed"] for e in talks} == {"talks"}

        ready = data(await c.call_tool("list_episodes", {"status": "ready"}))["episodes"]
        assert [e["id"] for e in ready] == [first.id]
        queued = data(await c.call_tool("list_episodes", {"feed": "talks", "status": "queued"}))
        assert [e["id"] for e in queued["episodes"]] == [second.id]
        assert data(await c.call_tool("list_episodes", {"status": "failed"})) == {"episodes": []}


async def test_list_episodes_limit(svc):
    async with Client(create_mcp(svc)) as c:
        svc.create_feed("Talks")
        ids = [svc.add_episode(f"https://x.example/{n}.mp3").id for n in range(60)]
        default = data(await c.call_tool("list_episodes"))["episodes"]
        assert len(default) == 50  # the default limit
        assert [e["id"] for e in default] == ids[::-1][:50]
        three = data(await c.call_tool("list_episodes", {"limit": 3}))["episodes"]
        assert [e["id"] for e in three] == ids[::-1][:3]
        everyone = data(await c.call_tool("list_episodes", {"limit": 100}))["episodes"]
        assert len(everyone) == 60


@pytest.mark.parametrize("limit", [0, -1, 501, 10**20])
async def test_a_limit_outside_1_to_500_is_rejected(svc, limit):
    async with Client(create_mcp(svc)) as c:
        svc.create_feed("Talks")
        svc.add_episode(URL)
        text = error_text(await c.call_tool("list_episodes", {"limit": limit}))
        assert "validation error" in text and "limit" in text  # the argument check, not a crash


async def test_the_largest_limit_works(svc):
    async with Client(create_mcp(svc)) as c:
        svc.create_feed("Talks")
        svc.add_episode(URL)
        assert len(data(await c.call_tool("list_episodes", {"limit": 500}))["episodes"]) == 1
        assert len(data(await c.call_tool("list_episodes", {"limit": 1}))["episodes"]) == 1


async def test_list_episodes_result_is_structured(svc):
    async with Client(create_mcp(svc)) as c:
        svc.create_feed("Talks")
        svc.add_episode(URL)
        result = await c.call_tool("list_episodes")
        assert result.structured_content == data(result)


async def test_remove(svc, db, storage):
    async with Client(create_mcp(svc)) as c:
        svc.create_feed("Talks")
        added = data(await c.call_tool("add_episode", {"url": URL}))
        publish(db, storage, added["id"], "talks")
        assert (storage.feed_dir("talks") / f"{added['id']}.mp3").exists()
        assert data(await c.call_tool("remove_episode", {"episode_id": added["id"]})) == {
            "removed": added["id"]
        }
        assert db.get_episode(added["id"]) is None
        assert not (storage.feed_dir("talks") / f"{added['id']}.mp3").exists()
        again = await c.call_tool("remove_episode", {"episode_id": added["id"]})
        assert error_text(again) == PREFIX.format("remove_episode") + f"No episode '{added['id']}'."


# ------------------------------------------------------------------------ errors


async def test_errors_are_tool_errors(svc):
    async with Client(create_mcp(svc)) as c:
        result = await c.call_tool("add_episode", {"url": URL})
        text = error_text(result)
        assert text == PREFIX.format("add_episode") + (
            "No feeds yet: create one in the podcast admin UI first."
        )
        assert "No feeds yet" in text


async def test_error_messages_are_the_services(svc):
    async with Client(create_mcp(svc)) as c:
        svc.create_feed("Talks")
        unknown_feed = "Unknown feed 'nope'. Call list_feeds for the slugs."
        no_episode = "No episode 'zzzzzzzzzzzz'."
        cases = [
            ("add_episode", {"url": "ftp://x.example/a.mp3"}, "Only http and https links"),
            ("add_episode", {"url": "https://u:p@x.example/a"}, "Links with a username or"),
            ("add_episode", {"url": URL, "feed": "nope"}, unknown_feed),
            ("list_episodes", {"feed": "nope"}, unknown_feed),
            ("get_episode", {"episode_id": "zzzzzzzzzzzz"}, no_episode),
            ("remove_episode", {"episode_id": "zzzzzzzzzzzz"}, no_episode),
        ]
        for name, args, message in cases:
            text = error_text(await c.call_tool(name, args))
            assert text.startswith(PREFIX.format(name) + message), (name, text)


async def test_a_status_that_does_not_exist_is_rejected(svc):
    async with Client(create_mcp(svc)) as c:
        for status in ("done", "Ready", "https://x.example/a"):
            assert (await c.call_tool("list_episodes", {"status": status})).is_error, status
        listed = {t.name: t for t in (await c.list_tools()).tools}["list_episodes"]
        shown = json.dumps(listed.input_schema["properties"]["status"])
        for status in ("queued", "downloading", "converting", "ready", "failed"):
            assert status in shown


async def test_blank_and_padded_slugs(svc):
    async with Client(create_mcp(svc)) as c:
        svc.create_feed("Talks")
        added = data(await c.call_tool("add_episode", {"url": URL, "feed": ""}))
        assert added["feed"] == "talks"  # blank: the default feed
        padded = data(await c.call_tool("list_episodes", {"feed": " talks ", "status": "queued"}))
        assert [e["id"] for e in padded["episodes"]] == [added["id"]]
        blank = data(await c.call_tool("list_episodes", {"feed": ""}))
        assert [e["id"] for e in blank["episodes"]] == [added["id"]]
        got = data(await c.call_tool("get_episode", {"episode_id": f" {added['id']} "}))
        assert got == added


async def test_blank_and_padded_status_mean_any_or_that_status(svc):
    async with Client(create_mcp(svc)) as c:
        svc.create_feed("Talks")
        added = data(await c.call_tool("add_episode", {"url": URL}))
        for status in ("", "   ", None):
            got = data(await c.call_tool("list_episodes", {"status": status}))["episodes"]
            assert [e["id"] for e in got] == [added["id"]], status
        got = data(await c.call_tool("list_episodes", {"status": " queued "}))["episodes"]
        assert [e["id"] for e in got] == [added["id"]]
        got = data(await c.call_tool("list_episodes", {"status": " failed "}))["episodes"]
        assert got == []


async def test_an_unexpected_error_is_just_an_internal_error_to_the_client(svc, monkeypatch):
    async with Client(create_mcp(svc)) as c:

        def boom():
            raise RuntimeError(LEAK)

        monkeypatch.setattr(svc, "list_feeds", boom)
        text = error_text(await c.call_tool("list_feeds"))
        assert text == PREFIX.format("list_feeds") + "internal error"


# ------------------------------------------------------------------- no secret anywhere


async def test_no_secret_anywhere(svc, db, storage, caplog):
    caplog.set_level(logging.DEBUG)
    svc.create_feed("Talks")
    svc.create_feed("Other", description="Second feed")
    seen = []

    async with Client(create_mcp(svc)) as c:

        async def call(name, args=None):
            result = await c.call_tool(name, args)
            seen.append(result.model_dump_json())
            return result

        added = data(await call("add_episode", {"url": LINK, "title": "Private talk"}))
        publish(db, storage, added["id"], "talks")
        await call("list_feeds")
        await call("list_episodes")
        await call("list_episodes", {"feed": "talks", "status": "ready"})
        await call("get_episode", {"episode_id": added["id"]})
        # Failures must not echo what was passed in either.
        await call("add_episode", {"url": LINK, "feed": "nope"})
        await call("add_episode", {"url": f"ftp://{HOST}/a?token=hunter2"})
        await call("add_episode", {"url": f"https://user:hunter2@{HOST}/a"})
        await call("remove_episode", {"episode_id": added["id"]})
        await call("remove_episode", {"episode_id": added["id"]})

    assert len(seen) == 10
    for needle in (SECRET, "podcast-api.example.com", HOST, "hunter2", "talk-0042", "/feeds/"):
        assert all(needle not in text for text in seen), needle
        assert needle not in caplog.text, needle
    assert "Private talk" not in caplog.text


async def test_a_link_in_the_status_stays_out_of_the_log(svc, caplog):
    caplog.set_level(logging.DEBUG)
    svc.create_feed("Talks")
    status_url = f"https://{HOST}/{SECRET}/feeds/talks.xml?token=hunter2"
    async with Client(create_mcp(svc)) as c:
        assert (await c.call_tool("list_episodes", {"status": status_url})).is_error
        assert (await c.call_tool("list_episodes", {"status": f" {status_url} "})).is_error
    for leak in (HOST, SECRET, "hunter2", "feeds/talks"):
        assert leak not in caplog.text, leak


async def test_a_link_or_a_sentence_where_an_id_or_slug_belongs_stays_out_of_the_log(svc, caplog):
    """The service quotes an unknown ID or slug in its message and the SDK logs that message;
    a link passed in the wrong place must be rejected before it can get there."""
    caplog.set_level(logging.DEBUG)
    svc.create_feed("Talks")
    feed_url = f"https://{HOST}/{SECRET}/feeds/talks.xml?token=hunter2"
    async with Client(create_mcp(svc)) as c:
        for name, args in (
            ("list_episodes", {"feed": feed_url}),
            ("add_episode", {"url": URL, "feed": feed_url}),
            ("get_episode", {"episode_id": feed_url}),
            ("remove_episode", {"episode_id": "Some Title With Spaces"}),
        ):
            assert (await c.call_tool(name, args)).is_error, name
        assert svc.list_episodes() == []  # nothing was added
    for leak in (HOST, SECRET, "hunter2", "Some Title"):
        assert leak not in caplog.text, leak


# ------------------------------------------------------------------------ logging


async def test_log_lines(svc, caplog):
    caplog.set_level(logging.DEBUG)
    svc.create_feed("Talks")
    async with Client(create_mcp(svc)) as c:
        await c.call_tool("list_feeds")
        added = data(
            await c.call_tool(
                "add_episode",
                {"url": LINK, "title": "Private title", "description": "Private text"},
            )
        )
        eid = added["id"]
        await c.call_tool("list_episodes", {"feed": "talks", "limit": 5, "status": "queued"})
        await c.call_tool("list_episodes")
        await c.call_tool("get_episode", {"episode_id": eid})
        await c.call_tool("remove_episode", {"episode_id": eid})
        await c.call_tool("get_episode", {"episode_id": eid})
        await c.call_tool("add_episode", {"url": "ftp://x.example/a"})
        await c.call_tool("add_episode", {"url": URL, "feed": "nope"})
        # Rejected by the argument check, before the tool runs: no line from us.
        assert (await c.call_tool("list_episodes", {"limit": -1})).is_error

    lines = mcp_lines(caplog)
    parsed = [LOG_LINE.fullmatch(line) for line in lines]
    assert all(parsed), lines  # exactly this shape and nothing else
    assert [m.groups() for m in parsed] == [
        ("list_feeds", "-", "ok"),
        ("add_episode", eid, "ok"),  # the episode's ID, not the link
        ("list_episodes", "talks", "ok"),
        ("list_episodes", "-", "ok"),
        ("get_episode", eid, "ok"),
        ("remove_episode", eid, "ok"),
        ("get_episode", eid, "error:NotFound"),
        ("add_episode", "-", "error:Invalid"),
        ("add_episode", "nope", "error:NotFound"),
    ]
    joined = "\n".join(lines)
    for leak in (HOST, "hunter2", "Private", "queued", SECRET, "x.example"):
        assert leak not in joined, leak


async def test_an_unexpected_error_is_logged_with_its_type_and_frames_only(
    svc, caplog, monkeypatch
):
    caplog.set_level(logging.DEBUG)

    def boom():
        raise RuntimeError(LEAK)

    monkeypatch.setattr(svc, "list_feeds", boom)
    async with Client(create_mcp(svc)) as c:
        assert (await c.call_tool("list_feeds")).is_error
    lines = mcp_lines(caplog)
    assert len(lines) == 1  # one record for the call, whatever the SDK logs besides
    first, _, frames = lines[0].partition("\n")
    assert LOG_LINE.fullmatch(first).groups() == ("list_feeds", "-", "error:RuntimeError")
    assert 'File "' in frames and "in boom" in frames and "raise RuntimeError(LEAK)" in frames
    # Nothing in the exception's message anywhere, from any logger, in any form.
    assert "SECRETTOKEN" not in caplog.text
    assert all("SECRETTOKEN" not in r.getMessage() for r in caplog.records)
    assert all(
        r.exc_info is None or "SECRETTOKEN" not in str(r.exc_info[1]) for r in caplog.records
    )


async def test_a_cancelled_call_is_logged_as_an_error(caplog):
    caplog.set_level(logging.DEBUG)
    running = threading.Event()
    release = threading.Event()

    def slow() -> dict:
        running.set()
        release.wait(10)
        return {}

    task = asyncio.create_task(_run("get_episode", "abc123", slow))
    try:
        await asyncio.to_thread(running.wait, 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    assert [LOG_LINE.fullmatch(line).groups() for line in mcp_lines(caplog)] == [
        ("get_episode", "abc123", "error:CancelledError")
    ]


@pytest.mark.parametrize(
    "link",
    [
        "http://xn--a-ecp.ru/private-path?token=SECRETTOKEN",  # idna: decodes to a⒈, not allowed
        "http://xn--/private-path?token=SECRETTOKEN",
        "http://xn--9999999999.example/private-path?token=SECRETTOKEN",
        "http://x.example/private-path?token=SECRETTOKEN\ud800",  # a lone surrogate
    ],
)
async def test_a_link_idna_cannot_decode_is_refused_and_never_logged(svc, caplog, link):
    caplog.set_level(logging.DEBUG)
    svc.create_feed("Talks")
    async with Client(create_mcp(svc)) as c:
        text = error_text(await c.call_tool("add_episode", {"url": link}))
    assert text == PREFIX.format("add_episode") + "Only http and https links to a file work."
    lines = mcp_lines(caplog)
    assert [LOG_LINE.fullmatch(line).groups() for line in lines] == [
        ("add_episode", "-", "error:Invalid")
    ]
    everything = caplog.text + "\n".join(r.getMessage() for r in caplog.records)
    for leak in ("a\u2488", "xn--", "SECRETTOKEN", "private-path", "9999999999"):
        assert leak not in everything, leak
    assert svc.list_episodes() == []


# -------------------------------------------------------------------- threading


async def test_service_calls_run_off_the_event_loop(svc, monkeypatch):
    svc.create_feed("Talks")
    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}

    def spy(name: str) -> None:
        real = getattr(svc, name)

        def wrapper(*args, **kwargs):
            seen[name] = threading.get_ident()
            return real(*args, **kwargs)

        monkeypatch.setattr(svc, name, wrapper)

    for name in TOOLS:
        spy(name)
    async with Client(create_mcp(svc)) as c:
        added = data(await c.call_tool("add_episode", {"url": URL}))
        await c.call_tool("list_feeds")
        await c.call_tool("list_episodes")
        await c.call_tool("get_episode", {"episode_id": added["id"]})
        await c.call_tool("remove_episode", {"episode_id": added["id"]})
    assert set(seen) == set(TOOLS)
    assert all(thread != loop_thread for thread in seen.values()), seen
