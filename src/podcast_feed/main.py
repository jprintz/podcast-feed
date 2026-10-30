"""The program: the web app on :8080, the MCP server on :8000 and the worker, on one event loop.

`main` reads the settings and runs `serve` until SIGTERM (a container stop) or SIGINT sets the
stop event. `serve` then closes both listeners, cancels the worker and returns, so the process
exits within seconds instead of being killed at the end of Podman's stop timeout.

If a listener or a worker loop ends on its own, `serve` stops the rest and raises, and the
process exits with status 1 so that Podman restarts the container: a dead worker behind live
listeners would accept episodes that are never processed.

Nothing here logs the settings: PODCAST_PUBLIC_BASE is a secret.
"""

import asyncio
import contextlib
import logging
import signal
import sys
import traceback
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import uvicorn
from starlette.applications import Starlette

from podcast_feed import __version__
from podcast_feed.config import Settings
from podcast_feed.db import Database
from podcast_feed.errors import ConfigError
from podcast_feed.guard import guarded_client
from podcast_feed.mcp_server import create_mcp
from podcast_feed.service import Service
from podcast_feed.sources import HttpFileSource, UploadSource
from podcast_feed.storage import Storage
from podcast_feed.web import create_web_app
from podcast_feed.worker import Worker

log = logging.getLogger("podcast_feed")

# On a stop, how long a listener lets open responses (a podcast app's long download, say)
# finish before it cancels them. Podman kills the container 10 s after SIGTERM.
GRACEFUL_SHUTDOWN_S = 3


class Stopped(RuntimeError):
    """A listener or a worker loop ended before the stop event was set."""


class StartupFailed(Exception):
    """A listener could not start: its port was taken, or its app's startup failed."""


@dataclass
class App:
    """The two ASGI apps and the worker that `serve` runs, and what it closes at the end."""

    web: Starlette
    mcp: Starlette
    worker: Worker
    db: Database
    http: httpx.AsyncClient

    async def aclose(self) -> None:
        """Close the download client and the database. Call it once the worker has stopped."""
        await self.http.aclose()
        self.db.close()


def build(settings: Settings) -> App:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.data_dir / "podcast.sqlite")
    storage = Storage(settings.media_dir)
    http = guarded_client()
    sources = {"url": HttpFileSource(http, settings.max_bytes), "upload": UploadSource(storage)}
    worker = Worker(db, storage, settings, sources)
    service = Service(db, storage, settings, wake=worker.wake)
    # host="0.0.0.0" is not the address the MCP server binds (that is settings.host, see
    # serve). The SDK switches its DNS-rebinding Host check on only for a localhost host, and
    # behind the tunnel the Host header is the public name, which the check would refuse.
    mcp = create_mcp(service).streamable_http_app(stateless_http=True, host="0.0.0.0")
    return App(web=create_web_app(service), mcp=mcp, worker=worker, db=db, http=http)


class _Listener(uvicorn.Server):
    """A uvicorn server that leaves signals to `main` and is stopped through `should_exit`.

    uvicorn's own `capture_signals` replaces the SIGINT and SIGTERM handlers with signal.signal
    and raises the signal again after its shutdown. Two servers in one process would nest those
    handlers and put them back out of order, and neither knows about the worker. So `main`
    handles the signals, once, for the whole process.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield

    async def serve(self, sockets=None) -> None:
        try:
            await super().serve(sockets)
        except SystemExit as exc:
            # uvicorn calls sys.exit() when it cannot bind its port or the app's startup
            # fails (it has logged why). In a task, SystemExit would tear through the event
            # loop past serve()'s shutdown, so it becomes an ordinary error here.
            raise StartupFailed(f"exit status {exc.code}") from None


def _listener(app: Starlette, host: str, port: int) -> _Listener:
    return _Listener(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            lifespan="on",  # the MCP app's session manager runs in its lifespan
            log_config=None,  # main() configures logging
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
        )
    )


def _described(exc: BaseException) -> str:
    """The exception's type and its traceback's frames, never its message: that could quote a
    link or other text from outside."""
    frames = "".join(traceback.format_tb(exc.__traceback__)).rstrip()
    return f"{type(exc).__name__}\n{frames}" if frames else type(exc).__name__


def _how_it_ended(task: asyncio.Task) -> str:
    if task.cancelled():
        return "it was cancelled"
    exc = task.exception()
    return "it returned" if exc is None else _described(exc)


async def serve(settings: Settings, stop: asyncio.Event) -> None:
    """Run both listeners and the worker until `stop` is set, then stop them all and return.

    The worker recovers (queues interrupted episodes again) before either listener opens. If
    a listener or a worker loop ends before `stop` is set, the rest is stopped and Stopped (a
    RuntimeError) names it.
    """
    app = build(settings)
    try:
        app.worker.recover()
        await _run(app, settings, stop)
    finally:
        await app.aclose()


async def _run(app: App, settings: Settings, stop: asyncio.Event) -> None:
    log.info("podcast-feed %s starting", __version__)
    web = _listener(app.web, settings.host, settings.web_port)
    mcp = _listener(app.mcp, settings.host, settings.mcp_port)
    worker = asyncio.create_task(app.worker.run(), name="worker")
    sweep = asyncio.create_task(app.worker.sweep_loop(), name="sweep")
    parts = [
        asyncio.create_task(web.serve(), name="web"),
        asyncio.create_task(mcp.serve(), name="mcp"),
        worker,
        sweep,
    ]
    stopping = asyncio.create_task(stop.wait(), name="stop")
    ended: asyncio.Task | None = None
    try:
        await asyncio.wait([stopping, *parts], return_when=asyncio.FIRST_COMPLETED)
        if not stop.is_set():
            ended = next(part for part in parts if part.done())
            log.error(
                "%s stopped unexpectedly, stopping the rest: %s",
                ended.get_name(),
                _how_it_ended(ended),
            )
    finally:  # also when serve() itself is cancelled
        web.should_exit = mcp.should_exit = True
        for task in (worker, sweep, stopping):
            task.cancel()
        await asyncio.gather(*parts, stopping, return_exceptions=True)
        for part in parts:
            if part is not ended and part.done() and not part.cancelled() and part.exception():
                log.error("%s failed while stopping: %s", part.get_name(), _how_it_ended(part))
    if ended is not None:
        raise Stopped(f"{ended.get_name()} stopped unexpectedly")
    log.info("podcast-feed stopped")


async def _main(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal, sig, stop)
    await serve(settings, stop)


def _on_signal(sig: signal.Signals, stop: asyncio.Event) -> None:
    log.info("Got %s, stopping", sig.name)
    stop.set()


def configure_logging() -> None:
    """INFO to stderr, without the libraries that would log what a caller sent."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # The SDK logs every request at INFO, and a failed tool call with its message.
    logging.getLogger("mcp").setLevel(logging.WARNING)
    # httpx logs the URL of every request the worker makes, each redirect hop too, at INFO,
    # and httpcore the host at DEBUG. A link can carry a private token, and no link is logged.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> None:
    """Run podcast-feed until SIGTERM or SIGINT."""
    configure_logging()
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        # Its message names the setting and never quotes PODCAST_PUBLIC_BASE (a secret).
        print(f"podcast-feed: {exc}", file=sys.stderr)
        sys.exit(2)
    try:
        asyncio.run(_main(settings))
    except Stopped as exc:
        log.error("Exiting: %s", exc)
        sys.exit(1)
    except Exception as exc:
        log.error("Exiting after an unexpected error: %s", _described(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
