import email.utils
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

from podcast_feed.db import Episode, Feed
from podcast_feed.rss import render_feed

NS = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd", "atom": "http://www.w3.org/2005/Atom"}
BASE = "https://p.example/s"
CHANGED = datetime(2026, 10, 29, 12, 0, 0, tzinfo=UTC)
VERSION = int(CHANGED.timestamp())


def make_feed(**changes) -> Feed:
    fields = {
        "id": 1,
        "slug": "talks",
        "title": "Talks & <more>",
        "description": None,
        "artwork": None,
        "retention_days": None,
        "is_default": True,
        "created_at": CHANGED,
        "changed_at": CHANGED,
    }
    return Feed(**{**fields, **changes})


def make_episode(id: str, published: datetime | None, **changes) -> Episode:
    fields = {
        "id": id,
        "feed_id": 1,
        "feed_slug": "talks",
        "source_kind": "url",
        "source_ref": f"https://x.example/{id}.mp3",
        "title": f"Episode {id}",
        "description": None,
        "artwork": None,
        "file": f"{id}.mp3",
        "mime": "audio/mpeg",
        "size_bytes": 1234,
        "duration_s": 125.0,
        "status": "ready",
        "error": None,
        "added_at": published or CHANGED,
        "published_at": published,
    }
    return Episode(**{**fields, **changes})


def at(hour: int) -> datetime:
    return datetime(2026, 10, 29, hour, 0, 0, tzinfo=UTC)


def channel(feed: Feed, episodes: list[Episode]) -> ET.Element:
    channel = ET.fromstring(render_feed(feed, episodes, BASE)).find("channel")
    assert channel is not None
    return channel


def guids(ch: ET.Element) -> list[str | None]:
    return [item.findtext("guid") for item in ch.findall("item")]


def test_feed_document():
    ep_old = make_episode("old", at(8))
    ep_new = make_episode("new", at(10))
    xml = ET.fromstring(render_feed(make_feed(), [ep_new, ep_old], BASE))
    assert xml.tag == "rss" and xml.get("version") == "2.0"
    ch = xml.find("channel")
    assert ch is not None
    assert ch.findtext("title") == "Talks & <more>"  # escaped on output, parsed back
    assert ch.findtext("description") == "Talks & <more>"  # falls back to the title
    assert ch.findtext("link") == "https://p.example/s/feeds/talks.xml"
    assert ch.findtext("itunes:block", namespaces=NS) == "Yes"
    assert ch.findtext("itunes:explicit", namespaces=NS) == "false"
    atom = ch.find("atom:link", NS)
    assert atom is not None
    assert atom.get("href") == "https://p.example/s/feeds/talks.xml"
    assert (atom.get("rel"), atom.get("type")) == ("self", "application/rss+xml")
    assert ch.find("itunes:image", NS) is None  # the feed has no artwork
    items = ch.findall("item")
    assert [i.findtext("guid") for i in items] == [ep_new.id, ep_old.id]
    enc = items[0].find("enclosure")
    assert enc is not None
    assert enc.get("url") == f"https://p.example/s/media/talks/{ep_new.id}.mp3"
    assert (enc.get("length"), enc.get("type")) == (str(ep_new.size_bytes), "audio/mpeg")
    guid = items[0].find("guid")
    assert guid is not None and guid.get("isPermaLink") == "false"
    assert items[0].findtext("itunes:duration", namespaces=NS) == "125"
    assert items[0].findtext("title") == "Episode new"
    assert ep_new.published_at is not None
    assert email.utils.parsedate_to_datetime(items[0].findtext("pubDate")) == ep_new.published_at


def test_the_declaration_names_utf8_and_the_prefixes_are_the_usual_ones():
    out = render_feed(make_feed(), [make_episode("a", at(8))], BASE)
    assert out.startswith(b"<?xml version='1.0' encoding='utf-8'?>")
    assert b"xmlns:itunes=" in out and b"xmlns:atom=" in out
    assert b"<itunes:block>Yes</itunes:block>" in out


def test_the_same_input_gives_the_same_bytes():
    # The feed's ETag is a hash of these bytes.
    episodes = [make_episode("a", at(8)), make_episode("b", at(9), description="d")]
    assert render_feed(make_feed(), episodes, BASE) == render_feed(make_feed(), episodes, BASE)


def test_a_feed_without_episodes_is_a_valid_feed():
    ch = channel(make_feed(), [])
    assert ch.findall("item") == []
    assert ch.findtext("title") == "Talks & <more>"


def test_feed_description_and_artwork():
    ch = channel(make_feed(description="About talks", artwork="cover.png"), [])
    assert ch.findtext("description") == "About talks"
    image = ch.find("itunes:image", NS)
    assert image is not None
    # The version makes a podcast app fetch a replaced cover again (R15).
    assert image.get("href") == f"https://p.example/s/media/talks/cover.png?v={VERSION}"


def test_item_description_artwork_and_duration():
    plain = make_episode("plain", at(8), duration_s=None)
    rich = make_episode(
        "rich", at(9), description="About it", artwork="rich.jpg", duration_s=125.9, size_bytes=7
    )
    items = channel(make_feed(), [rich, plain]).findall("item")
    rich_item, plain_item = items
    assert rich_item.findtext("description") == "About it"
    assert rich_item.findtext("itunes:duration", namespaces=NS) == "125"  # whole seconds
    image = rich_item.find("itunes:image", NS)
    assert image is not None
    assert image.get("href") == f"https://p.example/s/media/talks/rich.jpg?v={VERSION}"
    assert plain_item.find("description") is None
    assert plain_item.find("itunes:duration", NS) is None  # unknown: left out
    assert plain_item.find("itunes:image", NS) is None


def test_a_zero_duration_is_written():
    item = channel(make_feed(), [make_episode("a", at(8), duration_s=0.0)]).find("item")
    assert item is not None and item.findtext("itunes:duration", namespaces=NS) == "0"


def test_the_enclosure_has_no_version_but_a_replaced_cover_changes_the_artwork_url():
    later = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)
    episode = make_episode("a", at(8), artwork="a.png")
    before = ET.fromstring(render_feed(make_feed(artwork="cover.png"), [episode], BASE))
    after = ET.fromstring(
        render_feed(make_feed(artwork="cover.png", changed_at=later), [episode], BASE)
    )

    def hrefs(xml: ET.Element) -> list[str | None]:
        return [e.get("href") for e in xml.iterfind(".//itunes:image", NS)]

    assert hrefs(before) != hrefs(after)
    assert all(f"?v={int(later.timestamp())}" in h for h in hrefs(after))
    for xml in (before, after):
        enclosure = xml.find(".//enclosure")
        assert enclosure is not None and "?" not in enclosure.get("url", "")


def test_newest_published_first_ties_keep_the_given_order():
    # list_episodes gives newest added first, so for equal publish times that order stays.
    episodes = [
        make_episode("a", at(9)),
        make_episode("b", at(9)),
        make_episode("old", at(7)),
        make_episode("c", at(9)),
        make_episode("new", at(11)),
    ]
    assert guids(channel(make_feed(), episodes)) == ["new", "a", "b", "c", "old"]


def test_an_episode_without_a_publish_time_or_a_file_is_skipped():
    episodes = [
        make_episode("ok", at(8)),
        make_episode("unpublished", None),
        make_episode("fileless", at(9), file=None),
    ]
    assert guids(channel(make_feed(), episodes)) == ["ok"]


def test_an_episode_without_a_title_is_untitled():
    ch = channel(make_feed(), [make_episode("a", at(8), title=None)])
    assert ch.findtext("item/title") == "Untitled episode"


def test_xml_illegal_characters_are_stripped_from_every_text():
    nasty = "A\x00B\x1bC & <script>alert(1)</script> D\ud800E\ufffeF\uffffG\x08H\x0bI\x0cJ\x1fK"
    clean = "ABC & <script>alert(1)</script> DEFGHIJK"
    feed = make_feed(title=nasty, description=nasty, artwork="cover\x00.png")
    episode = make_episode(
        "a",
        at(8),
        title=nasty,
        description=nasty,
        artwork="a\x1b.png",
        file="a\x00.mp3",
        mime="x\x1b",
    )
    out = render_feed(feed, [episode], BASE)
    assert b"<script>" not in out  # escaped
    assert not any(ch in out.decode("utf-8") for ch in "\x00\x08\x0b\x0c\x1b\x1f\ufffe\uffff")
    ch = ET.fromstring(out).find("channel")  # still well-formed XML
    assert ch is not None
    assert ch.findtext("title") == clean
    assert ch.findtext("description") == clean
    item = ch.find("item")
    assert item is not None
    assert item.findtext("title") == clean
    assert item.findtext("description") == clean
    enclosure = item.find("enclosure")
    assert enclosure is not None
    assert enclosure.get("url") == "https://p.example/s/media/talks/a.mp3"
    assert enclosure.get("type") == "x"


def test_legal_unusual_characters_are_kept():
    text = "tab\there\nnew line, å ö é 日本語 😀 \x7f\x85"
    ch = channel(make_feed(), [make_episode("a", at(8), title=text)])
    assert ch.findtext("item/title") == text


def test_a_title_of_nothing_but_illegal_characters_is_untitled():
    ch = channel(make_feed(), [make_episode("a", at(8), title="\x00\x01")])
    assert ch.findtext("item/title") == "Untitled episode"


def test_file_names_are_quoted_in_urls():
    ch = channel(make_feed(), [make_episode("a", at(8), file="a b#.mp3")])
    enclosure = ch.find("item/enclosure")
    assert enclosure is not None
    assert enclosure.get("url") == "https://p.example/s/media/talks/a%20b%23.mp3"
