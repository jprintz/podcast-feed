from pathlib import Path

import pytest

from podcast_feed.config import Settings
from podcast_feed.errors import ConfigError

# The whitespace and the trailing "/" are stripped.
BASE = {"PODCAST_PUBLIC_BASE": " https://podcast-api.example.com/s3cr3t/\n"}


def test_defaults():
    s = Settings.from_env(BASE)
    assert s.public_base == "https://podcast-api.example.com/s3cr3t"
    assert (s.retention_days, s.max_bytes, s.max_hours) == (30, 1073741824, 6.0)
    assert s.min_free_bytes == 5 * 1024**3
    assert (s.web_port, s.mcp_port, s.host) == (8080, 8000, "0.0.0.0")
    assert (s.data_dir, s.media_dir) == (Path("/data"), Path("/media"))


def test_public_base_required():
    with pytest.raises(ConfigError, match="PODCAST_PUBLIC_BASE"):
        Settings.from_env({})


def test_public_base_must_be_https():
    with pytest.raises(ConfigError, match="https"):
        Settings.from_env({"PODCAST_PUBLIC_BASE": "http://x.example/s"})


def test_bad_number():
    with pytest.raises(ConfigError, match="PODCAST_RETENTION_DAYS"):
        Settings.from_env({**BASE, "PODCAST_RETENTION_DAYS": "thirty"})


def test_retention_at_least_one_day():
    with pytest.raises(ConfigError):
        Settings.from_env({**BASE, "PODCAST_RETENTION_DAYS": "0"})


def test_every_setting_can_be_overridden():
    s = Settings.from_env(
        {
            **BASE,
            "PODCAST_DATA_DIR": "/srv/data",
            "PODCAST_MEDIA_DIR": "/srv/media",
            "PODCAST_RETENTION_DAYS": "7",
            "PODCAST_MAX_BYTES": "2048",
            "PODCAST_MAX_HOURS": "1.5",
            "PODCAST_MIN_FREE_GB": "0.5",
            "PODCAST_WEB_PORT": "9090",
            "PODCAST_MCP_PORT": "9000",
            "PODCAST_HOST": "127.0.0.1",
        }
    )
    assert (s.data_dir, s.media_dir) == (Path("/srv/data"), Path("/srv/media"))
    assert (s.retention_days, s.max_bytes, s.max_hours) == (7, 2048, 1.5)
    assert s.min_free_bytes == 1024**3 // 2
    assert (s.web_port, s.mcp_port, s.host) == (9090, 9000, "127.0.0.1")


def test_blank_values_fall_back_to_the_defaults():
    s = Settings.from_env({**BASE, "PODCAST_RETENTION_DAYS": " ", "PODCAST_DATA_DIR": ""})
    assert (s.retention_days, s.data_dir) == (30, Path("/data"))


def test_public_base_is_never_echoed_in_an_error():
    with pytest.raises(ConfigError) as caught:
        Settings.from_env({"PODCAST_PUBLIC_BASE": "http://podcast-api.example.com/s3cr3t"})
    assert "s3cr3t" not in str(caught.value)


@pytest.mark.parametrize(
    "base", ["https://", "https:///s3cr3t", "ftp://x.example/s", "x.example/s"]
)
def test_public_base_needs_a_host_and_https(base):
    with pytest.raises(ConfigError, match="https"):
        Settings.from_env({"PODCAST_PUBLIC_BASE": base})


@pytest.mark.parametrize(
    "name, value",
    [
        ("PODCAST_RETENTION_DAYS", "1.5"),
        ("PODCAST_MAX_BYTES", "0"),
        ("PODCAST_MAX_HOURS", "0"),
        ("PODCAST_MAX_HOURS", "nan"),
        ("PODCAST_MAX_HOURS", "inf"),
        ("PODCAST_MIN_FREE_GB", "-1"),
        ("PODCAST_WEB_PORT", "0"),
        ("PODCAST_MCP_PORT", "70000"),
    ],
)
def test_out_of_range_numbers_are_rejected(name, value):
    with pytest.raises(ConfigError, match=name):
        Settings.from_env({**BASE, name: value})


def test_settings_are_frozen():
    s = Settings.from_env(BASE)
    with pytest.raises(AttributeError):
        s.host = "elsewhere"  # type: ignore[misc]
