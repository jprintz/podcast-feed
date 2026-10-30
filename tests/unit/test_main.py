import asyncio
import logging
import os
import signal
import socket
import sys

import pytest
from starlette.testclient import TestClient

from podcast_feed import main as main_module
from podcast_feed.config import Settings
from podcast_feed.db import Database
from podcast_feed.main import build, main, serve
from podcast_feed.worker import Worker
from tests.conftest import wait_until_listening

SECRET = "s3cr3t-path"
PUBLIC_BASE = f"https://p.example/{SECRET}"


def settings_for(tmp_path, **extra):
    return Settings.from_env(
        {
            "PODCAST_PUBLIC_BASE": PUBLIC_BASE,
            "PODCAST_DATA_DIR": str(tmp_path / "data"),
            "PODCAST_MEDIA_DIR": str(tmp_path / "media"),
            **extra,
        }
    )


def listening_settings(tmp_path, free_port):
    """Settings for serve(): both listeners on free ports of 127.0.0.1."""
    return settings_for(
        tmp_path,
        PODCAST_WEB_PORT=str(free_port()),
        PODCAST_MCP_PORT=str(free_port()),
        PODCAST_HOST="127.0.0.1",
    )


def is_listening(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), 1).close()
    except OSError:
        return False
    return True


def only_this_task_left() -> bool:
    return asyncio.all_tasks() == {asyncio.current_task()}


# --------------------------------------------------------------------------------- build


def test_build(tmp_path):
    app = build(settings_for(tmp_path))
    try:
        with TestClient(app.web) as c:
            assert c.get("/ui/").status_code == 200
        assert (tmp_path / "data/podcast.sqlite").exists()
    finally:
        asyncio.run(app.aclose())


def test_build_creates_the_data_folder_and_its_parents(tmp_path):
    app = build(settings_for(tmp_path, PODCAST_DATA_DIR=str(tmp_path / "a/b/data")))
    asyncio.run(app.aclose())
    assert (tmp_path / "a/b/data/podcast.sqlite").is_file()


def test_the_mcp_app_answers_under_the_public_host_name(tmp_path):
    """Behind the tunnel the Host header is the public name: the SDK's DNS-rebinding check,
    which would refuse it, must be off."""
    app = build(settings_for(tmp_path))
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    try:
        with TestClient(app.mcp, base_url="https://podcast-mcp.example.com") as c:
            r = c.post(
                "/mcp",
                json=initialize,
                headers={"Accept": "application/json, text/event-stream"},
            )
        assert r.status_code == 200, r.text
        assert "podcast_feed" in r.text
    finally:
        asyncio.run(app.aclose())


# --------------------------------------------------------------------------------- serve


async def test_serve_stops(tmp_path, free_port):
    web, mcp = free_port(), free_port()
    stop = asyncio.Event()
    task = asyncio.create_task(
        serve(
            settings_for(
                tmp_path,
                PODCAST_WEB_PORT=str(web),
                PODCAST_MCP_PORT=str(mcp),
                PODCAST_HOST="127.0.0.1",
            ),
            stop,
        )
    )
    await wait_until_listening(web)
    await wait_until_listening(mcp)
    stop.set()
    await asyncio.wait_for(task, 10)  # both listeners, the worker and the sweep loop end
    assert not is_listening(web)
    assert not is_listening(mcp)
    assert only_this_task_left()


async def test_serve_stops_within_seconds_while_the_worker_is_idle(tmp_path, free_port):
    settings = listening_settings(tmp_path, free_port)
    stop = asyncio.Event()
    task = asyncio.create_task(serve(settings, stop))
    await wait_until_listening(settings.web_port)
    await wait_until_listening(settings.mcp_port)
    loop = asyncio.get_running_loop()
    start = loop.time()
    stop.set()
    await asyncio.wait_for(task, 10)
    assert loop.time() - start < 2
    assert only_this_task_left()


async def test_serve_leaves_the_signal_handlers_alone(tmp_path, free_port):
    """main() owns SIGTERM and SIGINT. uvicorn would install its own handlers for each server
    (nested, and re-raised after its shutdown)."""
    signals = (signal.SIGTERM, signal.SIGINT)
    before = {sig: signal.getsignal(sig) for sig in signals}
    settings = listening_settings(tmp_path, free_port)
    stop = asyncio.Event()
    task = asyncio.create_task(serve(settings, stop))
    await wait_until_listening(settings.web_port)
    await wait_until_listening(settings.mcp_port)
    during = {sig: signal.getsignal(sig) for sig in signals}
    stop.set()
    await asyncio.wait_for(task, 10)
    assert during == before
    assert {sig: signal.getsignal(sig) for sig in signals} == before


async def test_serve_recovers_before_the_listeners_open(tmp_path, free_port, monkeypatch):
    settings = listening_settings(tmp_path, free_port)
    seen = []
    original = Worker.recover

    def recover(self):
        seen.append(is_listening(settings.web_port) or is_listening(settings.mcp_port))
        original(self)

    monkeypatch.setattr(Worker, "recover", recover)
    stop = asyncio.Event()
    task = asyncio.create_task(serve(settings, stop))
    await wait_until_listening(settings.web_port)
    stop.set()
    await asyncio.wait_for(task, 10)
    assert seen == [False]
    assert (tmp_path / "media/.tmp").is_dir()  # recover() laid out media/


async def test_a_failing_worker_stops_everything(tmp_path, free_port, monkeypatch, caplog):
    settings = listening_settings(tmp_path, free_port)

    async def run(self):
        await wait_until_listening(settings.web_port)
        await wait_until_listening(settings.mcp_port)
        raise OSError(f"text that must not be logged {SECRET}")

    monkeypatch.setattr(Worker, "run", run)
    with pytest.raises(RuntimeError, match="^worker stopped unexpectedly$"):
        await asyncio.wait_for(serve(settings, asyncio.Event()), 10)
    assert not is_listening(settings.web_port)
    assert not is_listening(settings.mcp_port)
    assert only_this_task_left()
    assert "worker" in caplog.text and "OSError" in caplog.text
    assert SECRET not in caplog.text


async def test_a_worker_loop_that_returns_is_fatal_too(tmp_path, free_port, monkeypatch):
    settings = listening_settings(tmp_path, free_port)

    async def sweep_loop(self, interval_s=3600):
        await wait_until_listening(settings.web_port)

    monkeypatch.setattr(Worker, "sweep_loop", sweep_loop)
    with pytest.raises(RuntimeError, match="^sweep stopped unexpectedly$"):
        await asyncio.wait_for(serve(settings, asyncio.Event()), 10)
    assert not is_listening(settings.web_port)
    assert only_this_task_left()


async def test_a_listener_that_cannot_start_stops_everything(tmp_path, free_port, caplog):
    settings = listening_settings(tmp_path, free_port)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", settings.web_port))
        taken.listen()
        with pytest.raises(RuntimeError, match="^web stopped unexpectedly$"):
            await asyncio.wait_for(serve(settings, asyncio.Event()), 10)
    assert not is_listening(settings.mcp_port)
    assert only_this_task_left()
    assert "web" in caplog.text


async def test_an_open_download_does_not_hold_up_the_stop(tmp_path, free_port, monkeypatch):
    """A podcast app in the middle of a long download: serve() gives it a moment, then cuts it
    off, so a container stop is not killed by Podman's timeout."""
    monkeypatch.setattr(main_module, "GRACEFUL_SHUTDOWN_S", 1)
    settings = listening_settings(tmp_path, free_port)
    settings.data_dir.mkdir(parents=True)
    db = Database(settings.data_dir / "podcast.sqlite")
    feed = db.create_feed("talks", "Talks")
    episode, _ = db.add_or_get_episode(feed.id, "url", "https://x.example/a.mp3", None, None)
    name = f"{episode.id}.mp3"
    size = 256 * 1024 * 1024
    db.mark_ready(
        episode.id,
        title="A",
        description=None,
        artwork=None,
        file=name,
        mime="audio/mpeg",
        size_bytes=size,
        duration_s=60.0,
    )
    db.close()
    (settings.media_dir / "talks").mkdir(parents=True)
    with open(settings.media_dir / "talks" / name, "wb") as f:
        f.truncate(size)  # sparse: big enough that the response cannot all be buffered

    stop = asyncio.Event()
    task = asyncio.create_task(serve(settings, stop))
    await wait_until_listening(settings.web_port)
    reader, writer = await asyncio.open_connection("127.0.0.1", settings.web_port)
    try:
        writer.write(f"GET /media/talks/{name} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        assert (await reader.readline()).startswith(b"HTTP/1.1 200")
        # ... and read no more, like a stalled download.
        loop = asyncio.get_running_loop()
        start = loop.time()
        stop.set()
        await asyncio.wait_for(task, 10)
        assert loop.time() - start < 4
    finally:
        writer.close()
    assert not is_listening(settings.web_port)


# ---------------------------------------------------------------------------------- main


@pytest.fixture
def main_env(tmp_path, monkeypatch):
    """The environment main() reads, and the mcp logger's level put back afterwards."""
    for name, value in {
        "PODCAST_PUBLIC_BASE": PUBLIC_BASE,
        "PODCAST_DATA_DIR": str(tmp_path / "data"),
        "PODCAST_MEDIA_DIR": str(tmp_path / "media"),
    }.items():
        monkeypatch.setenv(name, value)
    mcp_logger = logging.getLogger("mcp")
    level = mcp_logger.level
    yield
    mcp_logger.setLevel(level)


def test_main_refuses_a_bad_setting_without_quoting_it(main_env, monkeypatch, capsys):
    monkeypatch.setenv("PODCAST_PUBLIC_BASE", f"http://p.example/{SECRET}")
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == 2
    err = capsys.readouterr().err
    assert "PODCAST_PUBLIC_BASE" in err
    assert SECRET not in err


def test_main_exits_1_when_serve_fails(main_env, monkeypatch, caplog):
    async def failing_serve(settings, stop):
        raise ValueError(f"text that must not be logged {SECRET}")

    monkeypatch.setattr(main_module, "serve", failing_serve)
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == 1
    assert "ValueError" in caplog.text
    assert SECRET not in caplog.text


def test_main_exits_1_when_a_part_stops_unexpectedly(main_env, monkeypatch, caplog):
    async def failing_serve(settings, stop):
        raise main_module.Stopped("worker stopped unexpectedly")

    monkeypatch.setattr(main_module, "serve", failing_serve)
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == 1
    assert "worker stopped unexpectedly" in caplog.text


def test_main_quiets_the_mcp_sdk_and_returns_after_a_clean_stop(main_env, monkeypatch):
    seen = []

    async def quick_serve(settings, stop):
        seen.append(settings.public_base)

    monkeypatch.setattr(main_module, "serve", quick_serve)
    main()  # no SystemExit: exit status 0
    assert seen == [PUBLIC_BASE]
    assert logging.getLogger("mcp").level == logging.WARNING


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_main_sets_stop_on_a_signal(main_env, monkeypatch, sig):
    async def serve_until_stopped(settings, stop):
        os.kill(os.getpid(), sig)
        await asyncio.wait_for(stop.wait(), 5)

    monkeypatch.setattr(main_module, "serve", serve_until_stopped)
    main()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
async def test_the_process_exits_cleanly_on_a_signal(tmp_path, free_port, sig):
    """The whole program, as the container runs it: a signal stops it within seconds, with
    exit status 0, and its log never shows the public base."""
    web, mcp = free_port(), free_port()
    env = {
        **os.environ,
        "PODCAST_PUBLIC_BASE": PUBLIC_BASE,
        "PODCAST_DATA_DIR": str(tmp_path / "data"),
        "PODCAST_MEDIA_DIR": str(tmp_path / "media"),
        "PODCAST_WEB_PORT": str(web),
        "PODCAST_MCP_PORT": str(mcp),
        "PODCAST_HOST": "127.0.0.1",
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "from podcast_feed.main import main; main()",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        await wait_until_listening(web)
        await wait_until_listening(mcp)
        proc.send_signal(sig)
        output, _ = await asyncio.wait_for(proc.communicate(), 5)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    text = output.decode()
    assert proc.returncode == 0, text
    assert SECRET not in text
    assert "Application startup complete" in text
