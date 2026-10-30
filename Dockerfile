# podcast-feed: the web app on :8080 (admin UI under /ui/, public /feeds/ and /media/) and the
# MCP server (Streamable HTTP on :8000/mcp) in one process. Dependencies come from uv.lock;
# ffmpeg and ffprobe are the static binaries from mwader/static-ffmpeg.
FROM docker.io/library/python:3.14-slim-trixie

COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv
COPY --from=docker.io/mwader/static-ffmpeg:9.0.2 /ffmpeg /ffprobe /usr/local/bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PODCAST_DATA_DIR=/data \
    PODCAST_MEDIA_DIR=/media

WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable && rm -rf /root/.cache \
    && mkdir -p /data /media && chown 65532:65532 /data /media

# /data (SQLite) and /media (audio, and .tmp/ for jobs in progress) are volumes in production.
# A read-only container also needs a writable /tmp for form uploads (Podman's ReadOnly=true
# mounts one; with Docker use --tmpfs /tmp).
USER 65532:65532
EXPOSE 8000 8080
HEALTHCHECK --interval=30s --timeout=5s \
  CMD ["/opt/venv/bin/python", "-c", "import socket; [socket.create_connection(('127.0.0.1', p), 2).close() for p in (8000, 8080)]"]
CMD ["podcast-feed"]
