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
from tests.media import mp3_with_comment

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
    assert plain.cover_codec is None and plain.cover_index is None
    assert covered.cover_codec == "png" and covered.cover_index == 1  # the stream after the audio
    assert covered.audio_codec == "mp3"
    assert (await probe(media("mp3_cover_jpg"))).cover_codec == "mjpeg"
    # A real video stream comes first, then the audio, then the picture.
    video_cover = await probe(media("video_cover"))
    assert video_cover.cover_codec == "png" and video_cover.cover_index == 2


async def test_probe_tags_come_from_the_stream_too_and_are_lower_case(media):
    # Ogg keeps its tags on the audio stream; Matroska writes ENCODER and DURATION in capitals.
    assert (await probe(media("opus"))).tags["title"] == "Tone"
    tags = (await probe(media("video_mp3"))).tags
    assert tags["title"] == "Tone" and "encoder" in tags
    assert all(key == key.lower() for key in tags)


async def test_probe_refuses_playlists_that_point_at_urls(
    ffmpeg_tools, tmp_path, local_http_server
):
    # The format check refuses them, and ffmpeg (with `-protocol_whitelist file`) would not
    # fetch the URL anyway: either way nothing may reach the server, which would bypass the guard.
    url = f"http://127.0.0.1:{local_http_server.port}/segment.ts"
    playlist = tmp_path / "list.m3u8"
    playlist.write_text(f"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\n{url}\n#EXT-X-ENDLIST\n")
    concat = tmp_path / "list.ffconcat"
    concat.write_text(f"ffconcat version 1.0\nfile {url}\n")
    for path in (playlist, concat):
        with pytest.raises(EpisodeFailure, match=NOT_MEDIA):
            await probe(path)
    assert local_http_server.last_host is None


async def test_probe_refuses_playlists_that_point_at_local_files(media, tmp_path):
    # ffmpeg reads local files named in a playlist, so a downloaded .m3u8 could pull in any file
    # on the server: the format check must refuse it even though the target is real media.
    target = media("mp3")
    hls = tmp_path / "list.m3u8"
    hls.write_text(f"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:2,\n{target}\n#EXT-X-ENDLIST\n")
    concat = tmp_path / "list.ffconcat"
    concat.write_text(f"ffconcat version 1.0\nfile {target.name}\n")  # relative names are allowed
    for path in (hls, concat):
        with pytest.raises(EpisodeFailure, match=NOT_MEDIA):
            await probe(path)


@pytest.mark.parametrize(
    "format_name", ["hls", "applehttp", "concat", "dash", "sdp", "mov,mp4,hls", "dash,concat"]
)
async def test_probe_refuses_every_playlist_format(fake_tool, tmp_path, format_name):
    streams = '[{"codec_type": "audio", "codec_name": "aac", "disposition": {"attached_pic": 0}}]'
    fake_tool(
        "ffprobe",
        f"""printf '%s' '{{"format": {{"format_name": "{format_name}"}}, "streams": {streams}}}'""",
    )
    with pytest.raises(EpisodeFailure, match=NOT_MEDIA):
        await probe(tmp_path / "anything")


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


async def test_extract_cover_takes_the_picture_not_the_first_video_stream(media, tmp_path):
    # An mp4 with video, audio and then a PNG attached picture: `-map 0:v:0` would write the
    # MPEG-4 video stream into the .png.
    src = media("video_cover")
    cover = await extract_cover(src, tmp_path / "ep", await probe(src))
    assert cover == tmp_path / "ep.png"
    assert cover.read_bytes().startswith(b"\x89PNG")


async def test_extract_cover_ignores_other_picture_codecs(tmp_path):
    p = Probe("mp3", 2.0, "mp3", "gif", {}, cover_index=1)
    assert await extract_cover(tmp_path / "in.mp3", tmp_path / "ep", p) is None
    assert list(tmp_path.iterdir()) == []


async def test_extract_cover_needs_to_know_which_stream(tmp_path):
    p = Probe("mp3", 2.0, "mp3", "png", {})  # no cover_index
    assert await extract_cover(tmp_path / "in.mp3", tmp_path / "ep", p) is None
    assert list(tmp_path.iterdir()) == []


async def test_extract_cover_failure_is_none_and_leaves_nothing(media, tmp_path):
    src = media("mp3")  # has no picture, whatever the probe claims
    p = dataclasses.replace(await probe(src), cover_codec="png", cover_index=1)
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


# ------------------------------------------------- what a file's metadata can cost


MIB = 1024 * 1024
STREAM_JSON = '{"codec_type": "audio", "codec_name": "mp3", "disposition": {"attached_pic": 0}}'
PROBE_JSON = '{"format": {"format_name": "mp3", "duration": "2"}, "streams": [' + STREAM_JSON + "]}"


async def test_a_tag_that_makes_ffprobes_output_huge_is_not_media(ffmpeg_tools, tmp_path):
    # ffprobe prints every tag, so a 9 MiB comment is 9 MiB of JSON that would all be held in
    # memory (and then in the database and the feed).
    src = mp3_with_comment(tmp_path, 9 * MIB)
    try:
        with pytest.raises(EpisodeFailure, match=f"^{NOT_MEDIA}$"):
            await probe(src)
    finally:
        src.unlink()


@pytest.mark.parametrize("extra,ok", [(0, True), (1, False)])
async def test_ffprobes_output_may_be_8_mib_but_not_a_byte_more(fake_tool, tmp_path, extra, ok):
    padding = 8 * MIB - len(PROBE_JSON) + extra  # trailing white space is valid JSON
    fake_tool("ffprobe", f"printf '%s' '{PROBE_JSON}'; head -c {padding} /dev/zero | tr '\\0' ' '")
    if ok:
        assert (await probe(tmp_path / "anything")).audio_codec == "mp3"
    else:
        with pytest.raises(EpisodeFailure, match=f"^{NOT_MEDIA}$"):
            await probe(tmp_path / "anything")


async def test_output_past_the_limit_kills_ffprobe_at_once(fake_tool, tmp_path, monkeypatch):
    # The fake writes 20 MB and then hangs: nothing but the limit can end this before the timeout.
    monkeypatch.setattr(audio, "PROBE_TIMEOUT_S", 60)
    monkeypatch.setenv("PIDFILE", str(tmp_path / "pid"))
    fake_tool("ffprobe", 'echo $$ > "$PIDFILE"; head -c 20000000 /dev/zero; exec sleep 60')
    started = time.monotonic()
    with pytest.raises(EpisodeFailure, match=f"^{NOT_MEDIA}$"):
        await probe(tmp_path / "anything")
    assert time.monotonic() - started < FAST
    assert_gone(int((tmp_path / "pid").read_text()))


async def test_every_ffprobe_and_ffmpeg_run_caps_allocations(fake_tool, tmp_path, monkeypatch):
    monkeypatch.setenv("ARGV_FILE", str(tmp_path / "argv"))
    record = 'echo "${0##*/} $*" >> "$ARGV_FILE"'
    fake_tool("ffprobe", f"{record}; printf '%s' '{PROBE_JSON}'")
    fake_tool("ffmpeg", f'{record}; for out; do :; done; echo x > "$out"')
    src = tmp_path / "in.mp3"
    info = await probe(src)
    await convert(src, tmp_path / "copy.m4a", "copy")
    await convert(src, tmp_path / "transcode.m4a", "transcode")
    with_cover = dataclasses.replace(info, cover_codec="png", cover_index=1)
    assert await extract_cover(src, tmp_path / "ep", with_cover) == tmp_path / "ep.png"
    runs = (tmp_path / "argv").read_text().splitlines()
    assert [run.split()[0] for run in runs] == ["ffprobe", "ffmpeg", "ffmpeg", "ffmpeg"]
    for run in runs:
        assert " -max_alloc 134217728 " in run, run


async def test_the_allocation_cap_makes_the_tools_skip_a_huge_tag(
    ffmpeg_tools, tmp_path, monkeypatch
):
    # A frame bigger than the cap is skipped ("Error reading comment frame, skipped") instead of
    # being read into memory, by ffprobe and by ffmpeg, which would copy it into its output.
    # The cap is lowered to 1 MiB here, so that the test file need not be 160 MB.
    src = mp3_with_comment(tmp_path, 3 * MIB)
    try:
        assert len((await probe(src)).tags["comment"]) == 3 * MIB  # no cap that matters
        monkeypatch.setattr(audio, "MAX_ALLOC_BYTES", MIB)
        info = await probe(src)
        assert info.audio_codec == "mp3" and "comment" not in info.tags
        await convert(src, tmp_path / "out.m4a", "transcode")
        assert "comment" not in (await probe(tmp_path / "out.m4a")).tags
    finally:
        src.unlink()
