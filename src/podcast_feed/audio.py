"""Looking at a downloaded file with ffprobe and turning it into podcast audio with ffmpeg.

`probe` says what is in a file; `plan_output` decides how to make podcast audio of it: an MP3
file stays as it is, AAC is copied into an .m4a, and anything else is transcoded to AAC-LC at
128 kbps in an .m4a; `convert` does that; `extract_cover` pulls out embedded cover art.

Every path handed to ffmpeg or ffprobe is made absolute first, so a file name that starts with
`-` can never be read as an option, and every input is opened with the `file` protocol only, so
a downloaded playlist cannot make ffmpeg fetch other URLs (the URL guard only sees our own
download).
"""

import asyncio
import contextlib
import json
import math
import os
from asyncio.subprocess import DEVNULL, PIPE
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from podcast_feed.errors import EpisodeFailure

NOT_MEDIA = "not an audio or video file"
CONVERT_TIMEOUT_S = 1800
# ffprobe and the cover extraction only read a little of the file; these are generous.
PROBE_TIMEOUT_S = 120
COVER_TIMEOUT_S = 120
AUDIO_BITRATE = "128k"

_LOCAL_FILES_ONLY = ["-protocol_whitelist", "file"]
_COVER_EXTENSIONS = {"mjpeg": ".jpg", "png": ".png"}

OutputPlan = Literal["keep", "copy", "transcode"]


@dataclass(frozen=True)
class Probe:
    """What ffprobe found. `tags` has lower-cased keys, from the format and the first audio
    stream (the format's win). `cover_codec` is the codec of the attached picture, if any."""

    format_name: str
    duration_s: float | None
    audio_codec: str | None
    cover_codec: str | None
    tags: dict[str, str]


def _arg(path: Path) -> str:
    """path as an absolute string: never mistaken for an option or a protocol by ffmpeg."""
    return str(path.absolute())


async def _run(argv: list[str], timeout_s: float, *, capture: bool = False) -> tuple[int, bytes]:
    """Run argv to the end; returns its exit code and (if capture) its output.

    If it takes longer than timeout_s (TimeoutError) or this task is cancelled, the process is
    killed and reaped before the exception goes on.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=DEVNULL, stdout=PIPE if capture else DEVNULL, stderr=DEVNULL
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    return proc.returncode, stdout or b""


def _duration(fmt: dict) -> float | None:
    try:
        seconds = float(fmt.get("duration"))
    except (TypeError, ValueError):  # absent, or ffprobe's "N/A"
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


async def probe(path: Path) -> Probe:
    """Inspect a file. Raises EpisodeFailure if it is not audio or video, or has no audio."""
    argv = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams"]
    try:
        code, out = await _run(
            [*argv, *_LOCAL_FILES_ONLY, "-i", _arg(path)], PROBE_TIMEOUT_S, capture=True
        )
    except TimeoutError:
        raise EpisodeFailure(NOT_MEDIA) from None
    try:
        info = json.loads(out)
    except ValueError:
        info = None
    if code != 0 or not isinstance(info, dict) or not info.get("streams"):
        raise EpisodeFailure(NOT_MEDIA)

    streams = [s for s in info["streams"] if isinstance(s, dict)]
    fmt = info.get("format") or {}
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio is None:
        raise EpisodeFailure("no audio stream")
    cover = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video"
            and (s.get("disposition") or {}).get("attached_pic") == 1
        ),
        None,
    )
    tags = {
        str(key).lower(): str(value)
        for source in (audio.get("tags"), fmt.get("tags"))  # later wins: the format's tags
        for key, value in (source or {}).items()
    }
    return Probe(
        format_name=str(fmt.get("format_name") or ""),
        duration_s=_duration(fmt),
        audio_codec=audio.get("codec_name"),
        cover_codec=cover.get("codec_name") if cover else None,
        tags=tags,
    )


def plan_output(p: Probe) -> tuple[OutputPlan, str, str]:
    """How to make podcast audio of a probed file: (plan, extension, MIME type)."""
    if p.audio_codec == "mp3" and p.format_name == "mp3":
        return "keep", ".mp3", "audio/mpeg"
    if p.audio_codec == "aac":
        return "copy", ".m4a", "audio/mp4"
    return "transcode", ".m4a", "audio/mp4"


async def convert(
    src: Path, dest: Path, plan: OutputPlan, timeout_s: float = CONVERT_TIMEOUT_S
) -> None:
    """Make the podcast audio file dest from src according to plan (see plan_output).

    "keep" moves src to dest (a rename, so it never blocks on a big copy); the others run ffmpeg,
    which only takes the first audio stream, and need dest to end in .m4a. Raises
    EpisodeFailure("conversion timed out") if ffmpeg takes longer than timeout_s, or
    EpisodeFailure("not an audio or video file") if it fails. No partial dest is left behind.
    """
    if plan == "keep":
        os.replace(src, dest)
        return
    codec = ["-c:a", "copy"] if plan == "copy" else ["-c:a", "aac", "-b:a", AUDIO_BITRATE]
    argv = ["ffmpeg", "-nostdin", "-v", "error", "-y", *_LOCAL_FILES_ONLY, "-i", _arg(src)]
    argv += ["-map", "0:a:0", "-vn", *codec, "-movflags", "+faststart", _arg(dest)]
    done = False
    try:
        code, _ = await _run(argv, timeout_s)
        done = code == 0
    except TimeoutError:
        raise EpisodeFailure("conversion timed out") from None
    finally:
        if not done:
            dest.unlink(missing_ok=True)
    if not done:
        raise EpisodeFailure(NOT_MEDIA)


async def extract_cover(src: Path, dest_stem: Path, p: Probe) -> Path | None:
    """Save the cover art embedded in src as <dest_stem>.jpg or .png and return that path.

    Returns None if there is no JPEG or PNG cover or it cannot be extracted: art is optional.
    """
    extension = _COVER_EXTENSIONS.get(p.cover_codec or "")
    if extension is None:
        return None
    dest = dest_stem.with_name(dest_stem.name + extension)
    argv = ["ffmpeg", "-nostdin", "-v", "error", "-y", *_LOCAL_FILES_ONLY, "-i", _arg(src)]
    argv += ["-map", "0:v:0", "-frames:v", "1", "-c:v", "copy", _arg(dest)]
    done = False
    try:
        code, _ = await _run(argv, COVER_TIMEOUT_S)
        done = code == 0 and dest.is_file()
    except (TimeoutError, OSError):
        pass
    finally:
        if not done:
            dest.unlink(missing_ok=True)
    return dest if done else None
