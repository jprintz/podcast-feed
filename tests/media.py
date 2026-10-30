"""Tiny media files for the tests, made with ffmpeg (so the tests need it on PATH).

Every audio kind is a 2 s 440 Hz tone tagged `title=Tone`; the video kinds add a 160x120
test pattern. Nothing here is cached: generating a file takes a fraction of a second.
"""

import subprocess
from pathlib import Path

TONE = ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
PATTERN = ["-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=10"]
TITLE = ["-metadata", "title=Tone"]
MP3 = ["-c:a", "libmp3lame"]
# The picture goes in as input 1 and is stored as an ID3 attached picture.
WITH_COVER = ["-map", "0:a", "-map", "1:v", "-disposition:v", "attached_pic"]

# kind -> (file name, ffmpeg arguments between the global options and the output)
_KINDS: dict[str, tuple[str, list[str]]] = {
    "mp3": ("tone.mp3", [*TONE, *TITLE, *MP3]),
    "m4a": ("tone.m4a", [*TONE, *TITLE, "-c:a", "aac"]),
    "opus": ("tone.opus", [*TONE, *TITLE, "-c:a", "libopus"]),
    "video": ("clip.mp4", [*PATTERN, *TONE, *TITLE, "-c:v", "mpeg4", "-c:a", "aac"]),
    "video_mp3": ("clip_mp3.mkv", [*PATTERN, *TONE, *TITLE, "-c:v", "mpeg4", *MP3]),
    "video_silent": ("silent.mp4", [*PATTERN, *TITLE, "-c:v", "mpeg4"]),
}
# kind -> (file name, cover picture file name, cover codec)
_COVERED = {
    "mp3_cover": ("cover.mp3", "cover.png", "png"),
    "mp3_cover_jpg": ("cover_jpg.mp3", "cover.jpg", "mjpeg"),
}


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True, capture_output=True
    )


def _picture(tmp_path: Path, name: str) -> Path:
    """A 64x64 red square; the extension picks PNG or JPEG."""
    picture = tmp_path / name
    _ffmpeg("-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1", str(picture))
    return picture


def make_media(tmp_path: Path, kind: str) -> Path:
    """Write a media file of the given kind into tmp_path and return its path.

    Kinds: mp3, mp3_cover (PNG cover), mp3_cover_jpg (JPEG cover), m4a, opus, video (MPEG-4
    video and AAC audio), video_cover (the same, then a PNG attached picture as the third
    stream), video_mp3 (MPEG-4 video and MP3 audio in a .mkv), video_silent (no
    audio stream) and text (not media at all).
    """
    if kind == "text":
        path = tmp_path / "notes.txt"
        path.write_text("This is not a media file.\n")
        return path
    if kind in _COVERED:
        name, picture_name, codec = _COVERED[kind]
        picture = _picture(tmp_path, picture_name)
        path = tmp_path / name
        _ffmpeg(*TONE, "-i", str(picture), *WITH_COVER, *TITLE, *MP3, "-c:v", codec, str(path))
        return path
    if kind == "video_cover":
        # The real video comes first (stream 0), then the audio, then the picture (stream 2).
        path = tmp_path / "clip_cover.mp4"
        picture = _picture(tmp_path, "cover.png")
        _ffmpeg(
            *PATTERN, *TONE, "-i", str(picture), "-map", "0:v", "-map", "1:a", "-map", "2:v",
            *TITLE, "-c:v:0", "mpeg4", "-c:a", "aac", "-c:v:1", "png",
            "-disposition:v:1", "attached_pic", str(path),
        )  # fmt: skip
        return path
    name, args = _KINDS[kind]
    path = tmp_path / name
    _ffmpeg(*args, str(path))
    return path


def _syncsafe(number: int) -> bytes:
    """An ID3v2 size: four bytes of seven bits each."""
    return bytes([(number >> shift) & 0x7F for shift in (21, 14, 7, 0)])


def mp3_with_comment(tmp_path: Path, size: int) -> Path:
    """An MP3 file whose ID3v2.4 tag holds one comment (COMM) frame of `size` bytes of text, in
    front of the audio of the `mp3` kind. Written by hand, so a test can have a tag far bigger
    than any real one without ffmpeg making it; the file is as big as the tag, so delete it."""
    audio = make_media(tmp_path, "mp3").read_bytes()
    if audio.startswith(b"ID3"):  # ffmpeg wrote a tag of its own: replace it
        tag_size = sum((audio[6 + i] & 0x7F) << (7 * (3 - i)) for i in range(4))
        audio = audio[10 + tag_size :]
    body = b"\x00eng\x00" + b"A" * size  # Latin-1 text, language, an empty description
    frame = b"COMM" + _syncsafe(len(body)) + b"\x00\x00" + body
    path = tmp_path / "comment.mp3"
    path.write_bytes(b"ID3\x04\x00\x00" + _syncsafe(len(frame)) + frame + audio)
    return path
