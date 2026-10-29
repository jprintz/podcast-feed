import asyncio
import dataclasses
import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from podcast_feed import audio
from podcast_feed.audio import Probe, convert, extract_cover, plan_output, probe
from podcast_feed.errors import EpisodeFailure

NOT_MEDIA = "not an audio or video file"


@pytest.fixture
def fake_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Put a shell script named like a tool first on PATH, so a test controls what it does."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    def install(name: str, script: str) -> None:
        path = bin_dir / name
        path.write_text(f"#!/bin/sh\n{script}\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    return install


def streams_of(path: Path) -> list[dict]:
    """What ffprobe says about each stream of path (the test's own view, not audio.probe)."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out)["streams"]


async def test_probe_mp3(media):
    p = await probe(media("mp3"))
    assert p.audio_codec == "mp3" and abs(p.duration_s - 2) < 0.2 and p.tags["title"] == "Tone"


async def test_probe_text(media):
    with pytest.raises(EpisodeFailure, match="not an audio or video file"):
        await probe(media("text"))


async def test_probe_silent_video(media):
    with pytest.raises(EpisodeFailure, match="no audio stream"):
        await probe(media("video_silent"))


async def test_probe_missing_file(ffmpeg_tools, tmp_path):
    with pytest.raises(EpisodeFailure, match=NOT_MEDIA):
        await probe(tmp_path / "nothing.mp3")


async def test_probe_reports_the_container_and_the_cover(media):
    plain, covered = await probe(media("mp3")), await probe(media("mp3_cover"))
    assert plain.format_name == covered.format_name == "mp3"
    assert plain.cover_codec is None and covered.cover_codec == "png"
    assert covered.audio_codec == "mp3"
    assert (await probe(media("mp3_cover_jpg"))).cover_codec == "mjpeg"


async def test_probe_tags_come_from_the_stream_too_and_are_lower_case(media):
    # Ogg keeps its tags on the audio stream; Matroska writes ENCODER and DURATION in capitals.
    assert (await probe(media("opus"))).tags["title"] == "Tone"
    tags = (await probe(media("video_mp3"))).tags
    assert tags["title"] == "Tone" and "encoder" in tags
    assert all(key == key.lower() for key in tags)


async def test_probe_does_not_follow_urls_in_a_playlist(ffmpeg_tools, tmp_path, local_http_server):
    # A downloaded playlist must not make ffmpeg fetch a URL: that would bypass the URL guard.
    url = f"http://127.0.0.1:{local_http_server.port}/segment.ts"
    playlist = tmp_path / "list.m3u8"
    playlist.write_text(f"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\n{url}\n#EXT-X-ENDLIST\n")
    concat = tmp_path / "list.ffconcat"
    concat.write_text(f"ffconcat version 1.0\nfile {url}\n")
    for path in (playlist, concat):
        with pytest.raises(EpisodeFailure, match=NOT_MEDIA):
            await probe(path)
    assert local_http_server.last_host is None


@pytest.mark.parametrize(
    "output",
    [
        "printf 'not json at all'",
        "printf '[]'",
        "printf '{}'",
        'printf \'{"format": {}, "streams": []}\'',
        'printf \'{"streams": [{"codec_type": "audio"}]\'',  # cut off
        "exit 1",
    ],
)
async def test_probe_rejects_ffprobe_output_it_cannot_use(fake_tool, tmp_path, output):
    fake_tool("ffprobe", output)
    with pytest.raises(EpisodeFailure, match=NOT_MEDIA):
        await probe(tmp_path / "anything")


@pytest.mark.parametrize(
    "format_json,expected",
    [
        ('{"format_name": "mp3", "duration": "12.5"}', 12.5),
        ('{"format_name": "mp3", "duration": "N/A"}', None),
        ('{"format_name": "mp3"}', None),
        ('{"format_name": "mp3", "duration": "inf"}', None),
        ('{"format_name": "mp3", "duration": "garbage"}', None),
    ],
)
async def test_probe_duration_is_none_when_unknown(fake_tool, tmp_path, format_json, expected):
    streams = '[{"codec_type": "audio", "codec_name": "mp3", "disposition": {"attached_pic": 0}}]'
    fake_tool("ffprobe", f"""printf '%s' '{{"format": {format_json}, "streams": {streams}}}'""")
    assert (await probe(tmp_path / "anything")).duration_s == expected


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("mp3", ("keep", ".mp3", "audio/mpeg")),
        ("m4a", ("copy", ".m4a", "audio/mp4")),
        ("video", ("copy", ".m4a", "audio/mp4")),
        ("opus", ("transcode", ".m4a", "audio/mp4")),
        ("video_mp3", ("transcode", ".m4a", "audio/mp4")),  # MP3 audio, but not an MP3 file
    ],
)
async def test_plan(media, kind, expected):
    assert plan_output(await probe(media(kind))) == expected


async def test_keep_moves_the_file(media, tmp_path):
    src = media("mp3")
    await convert(src, tmp_path / "out.mp3", "keep")
    assert (tmp_path / "out.mp3").exists() and not src.exists()


@pytest.mark.parametrize("kind", ["video", "opus", "video_mp3"])
async def test_convert_to_m4a(media, tmp_path, kind):
    src = media(kind)
    plan, ext, _ = plan_output(await probe(src))
    await convert(src, tmp_path / f"out{ext}", plan)
    out = await probe(tmp_path / f"out{ext}")
    assert out.audio_codec == "aac" and out.cover_codec is None
    assert [s["codec_type"] for s in streams_of(tmp_path / f"out{ext}")] == ["audio"]
    assert src.exists()  # only "keep" moves the file


async def test_transcode_makes_aac_lc_at_128_kbps(media, tmp_path):
    await convert(media("opus"), tmp_path / "out.m4a", "transcode")
    (stream,) = streams_of(tmp_path / "out.m4a")
    assert stream["codec_name"] == "aac" and stream["profile"] == "LC"
    assert 110_000 < int(stream["bit_rate"]) < 150_000


async def test_copy_does_not_re_encode(media, tmp_path):
    src = media("m4a")
    (src_stream,) = streams_of(src)
    await convert(src, tmp_path / "out.m4a", "copy")
    (out_stream,) = streams_of(tmp_path / "out.m4a")
    assert out_stream["codec_name"] == "aac" and out_stream["bit_rate"] == src_stream["bit_rate"]


async def test_convert_failure_is_not_media_and_leaves_nothing(media, tmp_path):
    dest = tmp_path / "out.m4a"
    with pytest.raises(EpisodeFailure, match=NOT_MEDIA):
        await convert(media("text"), dest, "transcode")
    assert not dest.exists()


async def test_convert_timeout_on_a_real_conversion(media, tmp_path):
    dest = tmp_path / "out.m4a"
    with pytest.raises(EpisodeFailure, match="^conversion timed out$"):
        await convert(media("opus"), dest, "transcode", timeout_s=0.001)
    assert not dest.exists()


async def wait_for_file(path: Path) -> None:
    for _ in range(500):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{path} never appeared")


def assert_gone(pid: int) -> None:
    """The process no longer exists, not even as a zombie that was never waited for."""
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


FAST = 10  # seconds: far less than the 60 the fake tools hang for, far more than a kill takes

# Writes a partial output, tells the test which process this is, then hangs.
HANGING_FFMPEG = 'for out; do :; done; echo partial > "$out"; echo $$ > "$PIDFILE"; exec sleep 60'


async def test_timeout_kills_ffmpeg_reaps_it_and_deletes_the_partial_output(
    fake_tool, tmp_path, monkeypatch
):
    monkeypatch.setenv("PIDFILE", str(tmp_path / "pid"))
    fake_tool("ffmpeg", HANGING_FFMPEG)
    dest = tmp_path / "out.m4a"
    started = time.monotonic()
    with pytest.raises(EpisodeFailure, match="^conversion timed out$"):
        await convert(tmp_path / "in.opus", dest, "transcode", timeout_s=1)
    assert time.monotonic() - started < FAST  # the fake ffmpeg would sleep for a minute
    assert_gone(int((tmp_path / "pid").read_text()))
    assert not dest.exists()


async def test_a_hanging_ffprobe_is_killed_and_the_file_is_not_media(
    fake_tool, tmp_path, monkeypatch
):
    monkeypatch.setattr(audio, "PROBE_TIMEOUT_S", 1)
    monkeypatch.setenv("PIDFILE", str(tmp_path / "pid"))
    fake_tool("ffprobe", 'echo $$ > "$PIDFILE"; exec sleep 60')
    started = time.monotonic()
    with pytest.raises(EpisodeFailure, match=NOT_MEDIA):
        await probe(tmp_path / "anything")
    assert time.monotonic() - started < FAST
    assert_gone(int((tmp_path / "pid").read_text()))


async def test_cancelling_a_conversion_also_kills_ffmpeg(fake_tool, tmp_path, monkeypatch):
    monkeypatch.setenv("PIDFILE", str(tmp_path / "pid"))
    fake_tool("ffmpeg", HANGING_FFMPEG)
    dest = tmp_path / "out.m4a"
    task = asyncio.create_task(convert(tmp_path / "in.opus", dest, "transcode"))
    await wait_for_file(tmp_path / "pid")
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < FAST
    assert_gone(int((tmp_path / "pid").read_text()))
    assert not dest.exists()


async def test_extract_cover(media, tmp_path):
    src = media("mp3_cover")
    assert (await extract_cover(src, tmp_path / "ep", await probe(src))).suffix == ".png"
    src = media("mp3")
    assert await extract_cover(src, tmp_path / "ep2", await probe(src)) is None


async def test_extract_cover_writes_a_png_or_a_jpeg_at_the_stem(media, tmp_path):
    for kind, ext, magic in [
        ("mp3_cover", ".png", b"\x89PNG"),
        ("mp3_cover_jpg", ".jpg", b"\xff\xd8"),
    ]:
        src = media(kind)
        cover = await extract_cover(src, tmp_path / kind, await probe(src))
        assert cover == tmp_path / f"{kind}{ext}"
        assert cover.read_bytes().startswith(magic)


async def test_extract_cover_ignores_other_picture_codecs(tmp_path):
    p = Probe("mp3", 2.0, "mp3", "gif", {})
    assert await extract_cover(tmp_path / "in.mp3", tmp_path / "ep", p) is None
    assert list(tmp_path.iterdir()) == []


async def test_extract_cover_failure_is_none_and_leaves_nothing(media, tmp_path):
    src = media("mp3")  # has no picture, whatever the probe claims
    p = dataclasses.replace(await probe(src), cover_codec="png")
    assert await extract_cover(src, tmp_path / "ep", p) is None
    assert not (tmp_path / "ep.png").exists()


async def test_names_starting_with_a_dash_are_not_options(media, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    media("video").rename("-i.mp4")
    media("mp3_cover").rename("-c.mp3")
    p = await probe(Path("-i.mp4"))
    await convert(Path("-i.mp4"), Path("-out.m4a"), plan_output(p)[0])
    assert (await probe(Path("-out.m4a"))).audio_codec == "aac"
    cover = await extract_cover(Path("-c.mp3"), Path("-ep"), await probe(Path("-c.mp3")))
    assert cover is not None and cover.read_bytes().startswith(b"\x89PNG")
    await convert(Path("-c.mp3"), Path("-kept.mp3"), "keep")
    assert (tmp_path / "-kept.mp3").exists()
