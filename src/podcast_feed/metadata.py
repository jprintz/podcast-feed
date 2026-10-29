"""Episode title and description: what the person gave, else what the file says, else a fallback.

Blank text counts as missing, so an empty form field or an empty tag falls through to the next
source.
"""

from datetime import date

TITLE_MAX = 300


def _text(value: str | None) -> str | None:
    """value without surrounding whitespace, or None if nothing is left."""
    value = (value or "").strip()
    return value or None


def _stem(filename: str | None) -> str | None:
    """The file name without its last extension; None if there is no name left (e.g. `.mp3`)."""
    if filename is None:
        return None
    stem, dot, _ = filename.rpartition(".")
    return stem if dot else filename


def episode_title(
    given: str | None, tags: dict[str, str], filename: str | None, host: str, today: date
) -> str:
    """The title: given, else the file's title tag, else its name without the extension, else
    `<host> <date>`. Never longer than 300 characters."""
    candidates = (given, tags.get("title"), _stem(filename), f"{host} {today.isoformat()}")
    title = next(t for t in map(_text, candidates) if t)
    return title[:TITLE_MAX].rstrip()


def episode_description(given: str | None, tags: dict[str, str]) -> str | None:
    """The description: given, else the file's description tag, else its comment tag, else None."""
    for candidate in (given, tags.get("description"), tags.get("comment")):
        if text := _text(candidate):
            return text
    return None
