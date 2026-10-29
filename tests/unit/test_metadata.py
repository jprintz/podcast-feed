from datetime import date

import pytest

from podcast_feed.metadata import episode_description, episode_title

D = date(2026, 10, 29)


def test_title_order():
    assert episode_title(" Mine ", {"title": "Tag"}, "f.mp3", "x.example", D) == "Mine"
    assert episode_title(None, {"title": "Tag"}, "f.mp3", "x.example", D) == "Tag"
    assert episode_title(None, {}, "Episode 1.mp3", "x.example", D) == "Episode 1"
    assert episode_title(None, {}, None, "x.example", D) == "x.example 2026-10-29"
    assert len(episode_title("a" * 500, {}, None, "x", D)) == 300


def test_description_order():
    assert episode_description(None, {"description": "D", "comment": "C"}) == "D"
    assert episode_description(None, {"comment": "C"}) == "C"
    assert episode_description("  ", {}) is None


def test_blank_values_count_as_missing():
    assert episode_title("  ", {"title": "\n"}, " .mp3", "x.example", D) == "x.example 2026-10-29"
    assert episode_title("", {"title": " Tag "}, None, "x.example", D) == "Tag"
    assert episode_description("", {"description": "  ", "comment": " C "}) == "C"


def test_a_given_description_wins():
    assert episode_description(" Mine ", {"description": "D", "comment": "C"}) == "Mine"


@pytest.mark.parametrize(
    "filename,title",
    [
        ("Episode 1.mp3", "Episode 1"),
        ("my.talk.v2.m4a", "my.talk.v2"),  # only the last extension goes
        ("no extension", "no extension"),
        ("trailing dot.", "trailing dot"),
        ("  padded  .mp3", "padded"),
    ],
)
def test_title_from_the_file_name_drops_the_extension(filename, title):
    assert episode_title(None, {}, filename, "x.example", D) == title


@pytest.mark.parametrize("filename", [".mp3", ".", "", "  .mp3", None])
def test_a_file_name_without_a_stem_falls_through_to_host_and_date(filename):
    assert episode_title(None, {}, filename, "x.example", D) == "x.example 2026-10-29"


def test_every_source_of_a_title_is_cut_to_300_characters():
    assert episode_title(None, {"title": "t" * 400}, None, "x", D) == "t" * 300
    assert episode_title(None, {}, "f" * 400 + ".mp3", "x", D) == "f" * 300
    # The cut is followed by a strip, so a title never ends in a space.
    assert episode_title("a" * 299 + " b", {}, None, "x", D) == "a" * 299


def test_the_description_is_not_cut():
    assert episode_description("d" * 5000, {}) == "d" * 5000
