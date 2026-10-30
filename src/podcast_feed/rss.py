"""The RSS 2.0 document of a feed, with the iTunes tags podcast apps read.

Everything in it is text from outside: titles and descriptions come from the tags of a
downloaded file and from the admin UI. ElementTree escapes `&`, `<` and `>`, but it writes a
control character as it is, which no XML parser accepts, and it fails on a lone surrogate. So
every string goes through `_clean` first.

The output is a function of the arguments alone (no clock, no random values): the same feed and
episodes give the same bytes, which is what the feed's ETag is a hash of.
"""

import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from email.utils import format_datetime
from urllib.parse import quote

from podcast_feed.db import Episode, Feed

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM = "http://www.w3.org/2005/Atom"
ET.register_namespace("itunes", ITUNES)
ET.register_namespace("atom", ATOM)

UNTITLED = "Untitled episode"

# What XML 1.0 does not allow in a document: the C0 controls except tab, line feed and carriage
# return, the surrogates (a Python str holds a surrogate only when it is unpaired) and the two
# non-characters U+FFFE and U+FFFF.
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def _clean(text: str) -> str:
    return _ILLEGAL.sub("", text)


def _add(parent: ET.Element, tag: str, text: str | None = None, **attributes: str) -> ET.Element:
    """A child of `parent`, with text and attributes cleaned of what XML cannot hold."""
    element = ET.SubElement(
        parent, tag, {name: _clean(value) for name, value in attributes.items()}
    )
    if text is not None:
        element.text = _clean(text)
    return element


def _media_url(base: str, slug: str, name: str, version: int | None = None) -> str:
    """The public address of a file in the feed's folder. A version makes a podcast app fetch
    the file again when it is replaced under the same name."""
    # quote() would fail on a lone surrogate, hence the cleaning first.
    url = f"{base}/media/{slug}/{quote(_clean(name), safe='')}"
    return url if version is None else f"{url}?v={version}"


def render_feed(feed: Feed, episodes: Sequence[Episode], public_base: str) -> bytes:
    """The feed as an RSS 2.0 document in UTF-8.

    `episodes` are the feed's ready episodes, newest added first (the order of
    `Database.list_episodes`); they come out newest published first, and episodes published in
    the same second keep the order they came in. An episode that has no publish time or no audio
    file cannot be an item and is left out. `public_base` is the origin plus the secret path
    prefix, without a trailing slash; the feed's own address and every file's are built on it.
    """
    # The feed's changing is the one thing that replaces a cover under its old name.
    version = int(feed.changed_at.timestamp())
    feed_url = f"{public_base}/feeds/{feed.slug}.xml"  # a slug is letters, digits and dashes
    title = feed.title

    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    _add(channel, "title", title)
    _add(channel, "link", feed_url)
    _add(channel, "description", _clean(feed.description or "") or title)
    _add(channel, f"{{{ATOM}}}link", href=feed_url, rel="self", type="application/rss+xml")
    # A private feed: keep it out of the podcast directories.
    _add(channel, f"{{{ITUNES}}}block", "Yes")
    _add(channel, f"{{{ITUNES}}}explicit", "false")
    if feed.artwork:
        _add(
            channel,
            f"{{{ITUNES}}}image",
            href=_media_url(public_base, feed.slug, feed.artwork, version),
        )

    published = [e for e in episodes if e.published_at is not None and e.file]
    # sorted() is stable, also with reverse=True: equal times keep the given order.
    for episode in sorted(published, key=lambda e: e.published_at, reverse=True):
        _add_item(channel, feed, episode, public_base, version)

    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


def _add_item(
    channel: ET.Element, feed: Feed, episode: Episode, public_base: str, version: int
) -> None:
    """The item for an episode that has a publish time and a file."""
    item = ET.SubElement(channel, "item")
    _add(item, "title", _clean(episode.title or "") or UNTITLED)
    if episode.description and (description := _clean(episode.description)):
        _add(item, "description", description)
    _add(item, "guid", episode.id, isPermaLink="false")
    _add(item, "pubDate", format_datetime(episode.published_at))
    _add(
        item,
        "enclosure",
        url=_media_url(public_base, feed.slug, episode.file),
        length=str(episode.size_bytes or 0),
        type=episode.mime or "application/octet-stream",
    )
    if episode.duration_s is not None:
        _add(item, f"{{{ITUNES}}}duration", str(int(episode.duration_s)))
    if episode.artwork:
        _add(
            item,
            f"{{{ITUNES}}}image",
            href=_media_url(public_base, feed.slug, episode.artwork, version),
        )
