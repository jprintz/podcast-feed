import re

import pytest

import podcast_feed
from podcast_feed import errors


def test_version_is_a_version_string():
    assert re.fullmatch(r"\d+\.\d+\.\d+", podcast_feed.__version__)


@pytest.mark.parametrize("name", ["ConfigError", "Invalid", "NotFound", "EpisodeFailure"])
def test_errors_share_one_base(name):
    assert issubclass(getattr(errors, name), errors.PodcastError)
    assert issubclass(errors.PodcastError, Exception)


def test_episode_failure_str_is_the_reason():
    assert str(errors.EpisodeFailure("timed out")) == "timed out"
