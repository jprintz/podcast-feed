# podcast-feed

Private podcast feeds from links to audio and video files. Add a direct link to a file, and
podcast-feed downloads it, makes AAC audio of it (an MP3 file stays as it is) and publishes
it as an episode in an RSS feed that any podcast app can subscribe to. An admin UI manages the
feeds and episodes, and an [MCP](https://modelcontextprotocol.io) server lets Claude add and
remove episodes.

- **Direct links only:** a link to a file, not to a web page or a YouTube video. Every
  connection, redirects included, goes only to public addresses ("blocked address" otherwise),
  with limits on size, duration and free disk space.
- **Several feeds**, created in the admin UI. One of them is the default for new episodes.
- **Retention:** an episode is deleted 30 days after it was added (per-feed override).
- **One process:** SQLite for the index, audio files on disk, ffmpeg for the conversion. A
  restart picks up the episodes it interrupted.

## Listeners and routes

| Port | Path | What |
|---|---|---|
| 8080 | `GET`/`HEAD` `/feeds/<slug>.xml` | A feed (RSS 2.0 with the iTunes tags) |
| 8080 | `GET`/`HEAD` `/media/<slug>/<file>` | An episode's audio or artwork, or the feed's cover, with ranges |
| 8080 | `/ui/` | The admin UI (`/` redirects there) |
| 8000 | `/mcp` | The MCP server (Streamable HTTP, stateless) |

The feed and media routes are the only public ones; everything else on 8080 is under `/ui/`.
Every request other than `GET`, `HEAD` or `OPTIONS` must come from the admin UI's own pages
(`Sec-Fetch-Site: same-origin`, or without that header an `Origin` whose host is the request's
`Host`), or it gets 403.

**MCP tools**, each with a title and all four hints: `list_feeds`, `add_episode` (returns at
once with status `queued`), `list_episodes`, `get_episode` (poll it until `ready` or `failed`)
and `remove_episode`. They never return a feed URL. Feeds are created, edited and deleted in
the admin UI only.

Neither listener has a login of its own. Put both behind something that authenticates every
request: the author runs the admin UI and the MCP server behind Cloudflare Access.

## Configuration

| Variable | Default | |
|---|---|---|
| `PODCAST_PUBLIC_BASE` | (required) | **A secret.** The public `https://` address of the feeds, including the secret path, e.g. `https://podcast-api.example.com/<secret>`; a trailing `/` is dropped. Feed and media links are built from it. |
| `PODCAST_DATA_DIR` | `/data` | The SQLite database (`podcast.sqlite`) |
| `PODCAST_MEDIA_DIR` | `/media` | Audio and artwork, one folder per feed; `.tmp/` for jobs in progress |
| `PODCAST_RETENTION_DAYS` | `30` | Days an episode is kept (1 to 3650), unless its feed says otherwise |
| `PODCAST_MAX_BYTES` | `1073741824` | Largest download (1 GiB) |
| `PODCAST_MAX_HOURS` | `6` | Longest episode |
| `PODCAST_MIN_FREE_GB` | `5` | Free space the media disk must keep; below it an episode fails with "disk nearly full" |
| `PODCAST_WEB_PORT` | `8080` | Feeds, media and the admin UI |
| `PODCAST_MCP_PORT` | `8000` | The MCP server |
| `PODCAST_HOST` | `0.0.0.0` | The address both listeners bind |

A missing or invalid setting stops the program at once with exit status 2; the message names
the variable, never its value. podcast-feed never logs `PODCAST_PUBLIC_BASE` or a feed URL:
the feed URLs appear only in the admin UI. `media/` gets a `CACHEDIR.TAG`, so backups that
skip caches (`restic --exclude-caches`) leave the audio out.

## The gateway and the secret

Podcast apps can't log in to Cloudflare Access, so the feeds and the audio reach them through
the device gateway of [vps-infra](https://github.com/jprintz/vps-infra): a Cloudflare Worker
on `podcast-api.<domain>` checks the podcast secret (the first path segment), strips it,
forwards only `/feeds/` and `/media/` and adds the service token Access expects, so Access
still stands in front of every request. The podcast gateway has its own secret, separate from
the device secret of the e-reader clients. `PODCAST_PUBLIC_BASE` is that gateway's address
plus the secret, which is why it is one. How the gateway works and how to rotate its secret:
[`docs/devices.md`](https://github.com/jprintz/vps-infra/blob/main/docs/devices.md) in
vps-infra.

## Deployment

The image is `ghcr.io/jprintz/podcast-feed:<version>` (linux/amd64). It runs as uid 65532 with
a read-only root: give it writable `/data` and `/media` volumes and a `/tmp` (Podman's
`ReadOnly=true` mounts one; with Docker use `--tmpfs /tmp`). It needs no capabilities.
SIGTERM stops it within seconds; an open download gets 3 s to finish. If a listener or the
worker stops on its own, the process exits with status 1, so that the service manager
restarts it.

```ini
[Container]
ContainerName=podcast
Image=ghcr.io/jprintz/podcast-feed:0.1.0
Secret=podcast_public_base,type=env,target=PODCAST_PUBLIC_BASE
Volume=/srv/apps/podcast/data:/data:U
Volume=/srv/apps/podcast/media:/media:U
UserNS=auto
ReadOnly=true
NoNewPrivileges=true
DropCapability=ALL
```

## Development

```bash
uv sync --all-extras
make test     # unit tests; the conversion tests need ffmpeg and ffprobe on PATH (skipped otherwise)
make lint     # ruff check and format check
make smoke    # builds the image and smoke-tests it with Docker, as CI does
```

`make smoke` runs `scripts/smoke.sh`: the container read-only with no capabilities, the
image's healthcheck, the MCP tools and their annotations (`scripts/smoke_mcp.py`), the admin UI,
a feed round trip, the public routes' answers, ffmpeg, and `docker stop` under 8 s with exit
status 0. CI runs the tests with `REQUIRE_FFMPEG=1` (the static ffmpeg of the image) and the
same smoke test, and pushes the image from `main` and from `v*` tags.

## License

MIT
