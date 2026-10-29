"""The media folder on disk:

    media/
      CACHEDIR.TAG     keeps media/ out of backups (restic --exclude-caches)
      .tmp/            downloads and conversions in progress
      <slug>/          a feed's audio, episode artwork and cover

.tmp/ is on the same file system as the feed folders, so a finished file moves into its feed
with an atomic rename. Every path is a feed slug plus a plain file name, so none can point
outside media/ (or at .tmp/ or the tag).
"""

import contextlib
import shutil
from pathlib import Path

from podcast_feed.db import Episode
from podcast_feed.errors import Invalid
from podcast_feed.slug import valid_slug

# https://bford.info/cachedir/: the signature line, then anything.
CACHEDIR_TAG = (
    "Signature: 8a477f597d28d172789f06886806bc55\n"
    "# This file is a cache directory tag created by podcast-feed. See https://bford.info/cachedir/\n"
)


def _plain_name(name: str) -> str:
    """name, if it names a file directly in a folder; Invalid otherwise."""
    if not name or name.startswith(".") or any(ch in name for ch in "/\\\0"):
        raise Invalid(f"{name!r} is not a plain file name.")
    return name


class Storage:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.tmp = root / ".tmp"

    def ensure_layout(self) -> None:
        """Create media/ and media/.tmp/, and media/CACHEDIR.TAG if it is missing."""
        self.tmp.mkdir(parents=True, exist_ok=True)
        tag = self.root / "CACHEDIR.TAG"
        if not tag.exists():
            tag.write_text(CACHEDIR_TAG)

    def clear_tmp(self) -> None:
        """Delete everything in .tmp/: the leftovers of jobs a restart interrupted."""
        if not self.tmp.is_dir():
            return
        for entry in self.tmp.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink(missing_ok=True)

    def feed_dir(self, slug: str) -> Path:
        """The feed's folder, which may not exist yet. Invalid unless slug is a feed slug."""
        if not valid_slug(slug):
            raise Invalid(f"{slug!r} is not a feed slug.")
        return self.root / slug

    def path(self, slug: str, name: str) -> Path:
        """The file `name` in the feed's folder. Invalid unless name is a plain file name."""
        return self.feed_dir(slug) / _plain_name(name)

    def exists(self, slug: str, name: str) -> bool:
        return self.path(slug, name).is_file()

    def delete_episode_files(self, slug: str, episode: Episode) -> None:
        """Delete the episode's audio and artwork from the feed's folder, those that exist."""
        for name in (episode.file, episode.artwork):
            if name:
                self.path(slug, name).unlink(missing_ok=True)

    def delete_feed(self, slug: str) -> None:
        """Delete the feed's folder and everything in it, if it exists."""
        folder = self.feed_dir(slug)
        with contextlib.suppress(FileNotFoundError):
            shutil.rmtree(folder)

    def free_bytes(self) -> int:
        """Free space on the media volume."""
        return shutil.disk_usage(self.root).free
