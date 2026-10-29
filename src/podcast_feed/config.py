"""Settings from environment variables."""

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from podcast_feed.errors import ConfigError

GIB = 1024**3


@dataclass(frozen=True)
class Settings:
    # The public origin plus the secret path prefix, no trailing "/". It is a secret:
    # never log it and never put it in an error message.
    public_base: str
    data_dir: Path
    media_dir: Path
    retention_days: int
    max_bytes: int
    max_hours: float
    min_free_bytes: int
    web_port: int
    mcp_port: int
    host: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        return cls(
            public_base=_public_base(env),
            data_dir=Path(_text(env, "PODCAST_DATA_DIR", "/data")),
            media_dir=Path(_text(env, "PODCAST_MEDIA_DIR", "/media")),
            retention_days=_number(env, "PODCAST_RETENTION_DAYS", 30, int, minimum=1),
            max_bytes=_number(env, "PODCAST_MAX_BYTES", GIB, int, minimum=1),
            max_hours=_number(env, "PODCAST_MAX_HOURS", 6.0, float, minimum=0, exclusive=True),
            min_free_bytes=int(_number(env, "PODCAST_MIN_FREE_GB", 5, float, minimum=0) * GIB),
            web_port=_number(env, "PODCAST_WEB_PORT", 8080, int, minimum=1, maximum=65535),
            mcp_port=_number(env, "PODCAST_MCP_PORT", 8000, int, minimum=1, maximum=65535),
            host=_text(env, "PODCAST_HOST", "0.0.0.0"),
        )


def _public_base(env: Mapping[str, str]) -> str:
    raw = env.get("PODCAST_PUBLIC_BASE", "").strip().rstrip("/")
    if not raw:
        raise ConfigError(
            "PODCAST_PUBLIC_BASE is not set: give podcast-feed the public https:// address "
            "of its feeds, including the secret path"
        )
    parts = urlsplit(raw)
    if parts.scheme.lower() != "https" or not parts.hostname:
        # The value carries a secret, so it is not repeated here.
        raise ConfigError("PODCAST_PUBLIC_BASE must be an https:// address with a host name")
    return raw


def _text(env: Mapping[str, str], name: str, default: str) -> str:
    return env.get(name, "").strip() or default


def _number(
    env: Mapping[str, str], name: str, default, kind, *, minimum, maximum=None, exclusive=False
):
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = kind(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    too_small = value <= minimum if exclusive else value < minimum
    # "not finite" also rejects nan, which compares false with everything.
    if not math.isfinite(value) or too_small or (maximum is not None and value > maximum):
        raise ConfigError(f"{name} is out of range, got {raw!r}")
    return value
