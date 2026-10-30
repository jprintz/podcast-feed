from collections import namedtuple
from datetime import UTC, datetime

import pytest

from podcast_feed import storage as storage_module
from podcast_feed.db import Episode
from podcast_feed.errors import Invalid
from podcast_feed.storage import CACHEDIR_TAG, Storage

T0 = datetime(2026, 10, 29, 12, 0, 0, tzinfo=UTC)


def episode(file: str | None, artwork: str | None) -> Episode:
    return Episode(
        id="abcdefghijkl",
        feed_id=1,
        feed_slug="talks",
        source_kind="url",
        source_ref="https://x.example/a.mp3",
        title="T",
        description=None,
        artwork=artwork,
        file=file,
        mime="audio/mpeg",
        size_bytes=1,
        duration_s=1.0,
        status="ready",
        error=None,
        added_at=T0,
        published_at=T0,
    )


@pytest.fixture
def s(tmp_path):
    store = Storage(tmp_path / "media")
    store.ensure_layout()
    return store


def test_layout(tmp_path):
    s = Storage(tmp_path / "media")
    s.ensure_layout()
    assert (
        (tmp_path / "media/CACHEDIR.TAG")
        .read_text()
        .startswith("Signature: 8a477f597d28d172789f06886806bc55")
    )
    with pytest.raises(Invalid):
        s.path("talks", "../../data/podcast.sqlite")


def test_layout_makes_the_tmp_folder(tmp_path):
    s = Storage(tmp_path / "deep" / "media")
    s.ensure_layout()
    assert s.root == tmp_path / "deep" / "media"
    assert s.tmp == s.root / ".tmp" and s.tmp.is_dir()
    s.ensure_layout()  # twice is fine


def test_cachedir_tag_is_the_signature_then_a_comment():
    first, second = CACHEDIR_TAG.splitlines()
    assert CACHEDIR_TAG.startswith("Signature: 8a477f597d28d172789f06886806bc55\n")
    assert first == "Signature: 8a477f597d28d172789f06886806bc55"
    assert second.startswith("# ") and CACHEDIR_TAG.endswith("\n")


def test_layout_keeps_an_existing_tag(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    (root / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n# mine\n")
    Storage(root).ensure_layout()
    assert (root / "CACHEDIR.TAG").read_text().endswith("# mine\n")


def test_path_is_in_the_feed_folder(s):
    assert s.feed_dir("talks") == s.root / "talks"
    assert s.path("talks", "abcdefghijkl.mp3") == s.root / "talks" / "abcdefghijkl.mp3"
    assert s.path("talks", "cover.png") == s.root / "talks" / "cover.png"


@pytest.mark.parametrize(
    "name", ["", ".", "..", ".hidden", "../x.mp3", "a/b.mp3", "/etc/passwd", "a\\b.mp3", "a\0b"]
)
def test_path_refuses_anything_but_a_plain_name(s, name):
    with pytest.raises(Invalid):
        s.path("talks", name)
    with pytest.raises(Invalid):
        s.exists("talks", name)


@pytest.mark.parametrize("slug", ["", ".", "..", ".tmp", "a/b", "../media", "CACHEDIR.TAG"])
def test_feed_dir_refuses_anything_but_a_slug(s, slug):
    with pytest.raises(Invalid):
        s.feed_dir(slug)
    with pytest.raises(Invalid):
        s.delete_feed(slug)
    assert s.tmp.is_dir() and (s.root / "CACHEDIR.TAG").is_file()


def test_exists(s):
    assert not s.exists("talks", "a.mp3")  # no folder yet
    s.feed_dir("talks").mkdir()
    (s.feed_dir("talks") / "a.mp3").write_bytes(b"x")
    (s.feed_dir("talks") / "folder.mp3").mkdir()
    assert s.exists("talks", "a.mp3")
    assert not s.exists("talks", "b.mp3")
    assert not s.exists("talks", "folder.mp3")  # only files count


def test_clear_tmp(s):
    (s.tmp / "abcdefghijkl.download").write_bytes(b"x")
    (s.tmp / "sub").mkdir()
    (s.tmp / "sub" / "file").write_bytes(b"x")
    s.clear_tmp()
    assert s.tmp.is_dir() and not any(s.tmp.iterdir())


def test_clear_tmp_without_a_tmp_folder(tmp_path):
    Storage(tmp_path / "nothing").clear_tmp()
    assert not (tmp_path / "nothing").exists()


def test_delete_episode_files(s):
    folder = s.feed_dir("talks")
    folder.mkdir()
    for name in ("e.mp3", "e.png", "other.mp3", "cover.png"):
        (folder / name).write_bytes(b"x")
    s.delete_episode_files("talks", episode("e.mp3", "e.png"))
    assert sorted(p.name for p in folder.iterdir()) == ["cover.png", "other.mp3"]
    # Missing files, no artwork and no folder at all are all fine.
    s.delete_episode_files("talks", episode("e.mp3", "e.png"))
    s.delete_episode_files("talks", episode("other.mp3", None))
    s.delete_episode_files("talks", episode(None, None))
    s.delete_episode_files("gone", episode("e.mp3", "e.png"))
    assert [p.name for p in folder.iterdir()] == ["cover.png"]


def test_delete_episode_files_refuses_a_path(s):
    outside = s.root / "secret.txt"
    outside.write_text("x")
    with pytest.raises(Invalid):
        s.delete_episode_files("talks", episode("../secret.txt", None))
    assert outside.exists()


def test_delete_episode_files_by_id(s):
    folder = s.feed_dir("talks")
    folder.mkdir()
    mine = ["abcdefghijkl.mp3", "abcdefghijkl.m4a", "abcdefghijkl.png", "abcdefghijkl.jpg"]
    others = [
        "abcdefghijklm.mp3",  # another episode whose id starts with this id
        "zbcdefghijkl.mp3",
        "abcdefghijkl",  # no dot after the id
        "xabcdefghijkl.mp3",
        "cover.png",
    ]
    for name in mine + others:
        (folder / name).write_bytes(b"x")
    (s.root / "other").mkdir()
    (s.root / "other" / "abcdefghijkl.mp3").write_bytes(b"x")
    s.delete_episode_files_by_id("talks", "abcdefghijkl")
    assert sorted(p.name for p in folder.iterdir()) == sorted(others)
    assert (s.root / "other" / "abcdefghijkl.mp3").exists()
    s.delete_episode_files_by_id("talks", "abcdefghijkl")  # nothing left: fine


def test_delete_episode_files_by_id_keeps_folders(s):
    folder = s.feed_dir("talks")
    (folder / "abcdefghijkl.d").mkdir(parents=True)
    (folder / "abcdefghijkl.d" / "inner").write_bytes(b"x")
    s.delete_episode_files_by_id("talks", "abcdefghijkl")
    assert (folder / "abcdefghijkl.d" / "inner").exists()


def test_delete_episode_files_by_id_without_a_folder(s):
    s.delete_episode_files_by_id("gone", "abcdefghijkl")
    assert not s.feed_dir("gone").exists()


@pytest.mark.parametrize("episode_id", ["", ".", "..", ".hidden", "../x", "a/b", "a\\b", "a\0b"])
def test_delete_episode_files_by_id_refuses_anything_but_a_plain_id(s, episode_id):
    folder = s.feed_dir("talks")
    folder.mkdir()
    (folder / "a.mp3").write_bytes(b"x")
    (folder / "..mp3").write_bytes(b"x")
    with pytest.raises(Invalid):
        s.delete_episode_files_by_id("talks", episode_id)
    assert sorted(p.name for p in folder.iterdir()) == ["..mp3", "a.mp3"]


def test_delete_episode_files_by_id_refuses_a_bad_slug(s):
    with pytest.raises(Invalid):
        s.delete_episode_files_by_id("../media", "abcdefghijkl")


def test_delete_feed(s):
    folder = s.feed_dir("talks")
    (folder / "sub").mkdir(parents=True)
    (folder / "a.mp3").write_bytes(b"x")
    (folder / "sub" / "b").write_bytes(b"x")
    (s.root / "other").mkdir()
    s.delete_feed("talks")
    assert not folder.exists() and (s.root / "other").is_dir() and s.tmp.is_dir()
    s.delete_feed("talks")  # already gone: fine


def test_free_bytes(s, monkeypatch):
    usage = namedtuple("usage", "total used free")
    seen = []

    def disk_usage(path):
        seen.append(path)
        return usage(10, 3, 7)

    monkeypatch.setattr(storage_module.shutil, "disk_usage", disk_usage)
    assert s.free_bytes() == 7 and seen == [s.root]


def test_free_bytes_of_a_real_disk(s):
    assert s.free_bytes() > 0


def test_upload_files_live_in_tmp_and_the_id_must_be_a_plain_name(s):
    assert s.upload_part("abcdefghijkl") == s.tmp / "abcdefghijkl.part"
    assert s.upload_ready("abcdefghijkl") == s.tmp / "abcdefghijkl.upload"
    for bad in ("../x", "a/b", ".hidden", ""):
        with pytest.raises(Invalid):
            s.upload_part(bad)
        with pytest.raises(Invalid):
            s.upload_ready(bad)


def test_delete_upload_takes_both_files_and_nothing_else(s):
    (s.tmp / "abcdefghijkl.part").write_bytes(b"x")
    (s.tmp / "abcdefghijkl.upload").write_bytes(b"x")
    (s.tmp / "abcdefghijkl.download").write_bytes(b"x")  # the worker's, not an upload's
    (s.tmp / "zzzzzzzzzzzz.upload").write_bytes(b"x")
    s.delete_upload("abcdefghijkl")
    assert sorted(path.name for path in s.tmp.iterdir()) == [
        "abcdefghijkl.download",
        "zzzzzzzzzzzz.upload",
    ]
    s.delete_upload("abcdefghijkl")  # already gone: fine
