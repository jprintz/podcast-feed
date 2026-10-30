import pytest

from podcast_feed.slug import slugify, valid_slug


@pytest.mark.parametrize(
    "title,slug",
    [
        ("Långa poddar!", "langa-poddar"),
        ("  NotebookLM  overviews ", "notebooklm-overviews"),
        ("???", "feed"),
        ("", "feed"),
        ("a" * 60, "a" * 40),
        # Cutting at 40 must not leave a trailing "-".
        ("a" * 39 + " b", "a" * 39),
        ("Already-a-slug_42", "already-a-slug-42"),
        ("日本語", "feed"),
    ],
)
def test_slugify(title, slug):
    assert slugify(title) == slug


@pytest.mark.parametrize("title", ["Långa poddar!", "???", "a" * 60, "a" * 39 + " b", "-x-", "Ünï"])
def test_slugify_always_gives_a_valid_slug(title):
    assert valid_slug(slugify(title))


@pytest.mark.parametrize(
    "s,ok",
    [
        ("talks", True),
        ("a-b-1", True),
        ("a", True),
        ("a" * 40, True),
        ("-a", False),
        ("a-", False),
        ("A", False),
        ("a" * 41, False),
        ("", False),
        ("a b", False),
        ("a_b", False),
        ("a\n", False),
    ],
)
def test_valid_slug(s, ok):
    assert valid_slug(s) is ok
