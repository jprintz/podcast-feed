"""Feed slugs: the part of a feed's URL and media directory that names it."""

import re
import unicodedata

MAX_LENGTH = 40
_VALID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?")


def slugify(title: str) -> str:
    """A valid slug from a title: ASCII letters and digits joined by single dashes."""
    ascii_only = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    dashed = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")
    return dashed[:MAX_LENGTH].strip("-") or "feed"


def valid_slug(slug: str) -> bool:
    return _VALID.fullmatch(slug) is not None
