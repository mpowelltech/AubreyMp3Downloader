"""Thin wrapper around the yt-dlp binary: metadata fetch + audio download."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .paths import ffmpeg_location

_CREATE_NO_WINDOW = 0x08000000
_PCT = re.compile(r"(\d{1,3}(?:\.\d)?)%")

# Order matters: pick the real audio file before the .jpg thumbnail.
_AUDIO_GLOBS = ("audio.m4a", "audio.webm", "audio.opus", "audio.mp3",
                "audio.ogg", "audio.aac", "audio.wav", "audio.*")
_THUMB_GLOBS = ("audio.jpg", "audio.jpeg", "audio.png", "audio.webp")


@dataclass
class VideoInfo:
    title: str
    duration: float            # seconds (0 if unknown)
    thumbnail: Optional[str]   # url


class DownloadError(Exception):
    """A failure worded for a non-technical user."""


def _no_window() -> dict:
    return {"creationflags": _CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def _engine_args(deno: Optional[str]) -> list[str]:
    """Args that let yt-dlp solve YouTube's JS 'n' challenge.

    ``--remote-components ejs:github`` lets yt-dlp fetch fresh EJS solver
    scripts if its bundled ones are missing/stale; ``--js-runtimes deno:<path>``
    points it at our downloaded Deno when it isn't on PATH.
    """
    args = ["--remote-components", "ejs:github"]
    if deno:
        args += ["--js-runtimes", f"deno:{deno}"]
    return args


def fetch_info(ytdlp: str, url: str, deno: Optional[str] = None) -> VideoInfo:
    """Read title/duration/thumbnail without downloading the media."""
    try:
        proc = subprocess.run(
            [ytdlp, "-J", "--no-playlist", "--no-warnings", *_engine_args(deno), url],
            capture_output=True, text=True, timeout=120, **_no_window(),
        )
    except subprocess.TimeoutExpired:
        raise DownloadError("Timed out reading the video. Check your internet and try again.")
    except FileNotFoundError:
        raise DownloadError("The YouTube engine isn't available yet. Please reopen the app.")
    if proc.returncode != 0:
        raise DownloadError(_friendly(proc.stderr))
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise DownloadError("Couldn't read that video. Is the link correct?")
    return VideoInfo(
        title=data.get("title") or "audio",
        duration=float(data.get("duration") or 0),
        thumbnail=data.get("thumbnail"),
    )


def download_audio(
    ytdlp: str,
    url: str,
    workdir: Path,
    deno: Optional[str] = None,
    on_progress: Optional[Callable[[float], None]] = None,
) -> tuple[Path, Optional[Path]]:
    """Download best audio (+ a jpg thumbnail) into ``workdir``.

    Returns ``(audio_file, thumbnail_file_or_None)``.
    """
    cmd = [
        ytdlp, url,
        "--no-playlist", "--no-warnings",
        "-f", "bestaudio/best",
        "-o", str(workdir / "audio.%(ext)s"),
        "--write-thumbnail", "--convert-thumbnails", "jpg",
        "--newline", "--progress-template", "dl:%(progress._percent_str)s",
        *_engine_args(deno),
    ]
    loc = ffmpeg_location()
    if loc:
        cmd += ["--ffmpeg-location", loc]

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, **_no_window(),
        )
    except FileNotFoundError:
        raise DownloadError("The YouTube engine isn't available yet. Please reopen the app.")

    assert proc.stdout is not None
    tail: list[str] = []
    # readline() (not "for line in proc.stdout") so progress streams in real time
    # on Windows instead of being block-buffered until the process exits.
    for raw in iter(proc.stdout.readline, ""):
        line = raw.strip()
        if line.startswith("dl:"):
            m = _PCT.search(line)
            if m and on_progress:
                on_progress(float(m.group(1)))
        elif line:
            tail.append(line)
            del tail[:-25]  # keep only the last 25 lines for error context
    proc.wait()
    if proc.returncode != 0:
        raise DownloadError(_friendly("\n".join(tail)))

    audio = _find_one(workdir, _AUDIO_GLOBS, exclude_suffixes={".jpg", ".jpeg", ".png", ".webp"})
    if audio is None:
        raise DownloadError("Couldn't find the downloaded audio. Please try again.")
    thumb = _find_one(workdir, _THUMB_GLOBS)
    return audio, thumb


def _find_one(d: Path, patterns, exclude_suffixes: set[str] | None = None) -> Optional[Path]:
    for pat in patterns:
        for hit in sorted(d.glob(pat)):
            if exclude_suffixes and hit.suffix.lower() in exclude_suffixes:
                continue
            if hit.is_file():
                return hit
    return None


def _friendly(stderr: str) -> str:
    """Map yt-dlp's stderr to a plain-language message."""
    s = (stderr or "").lower()
    if "private video" in s:
        return "That video is private and can't be downloaded."
    if "age" in s and ("confirm your age" in s or "restricted" in s):
        return "That video is age-restricted and can't be downloaded here."
    if any(t in s for t in ("challenge", "js runtime", "javascript runtime",
                            "no solutions", "ejs", "[jsc]")):
        return ("Couldn't process this video. The YouTube helper isn't ready yet.\n"
                "Check your internet connection and reopen the app so it can finish setting up.")
    if "video unavailable" in s or "this video is not available" in s:
        return "That video is unavailable (it may be removed or region-locked)."
    if "is not a valid url" in s or "unsupported url" in s:
        return "That doesn't look like a valid YouTube link."
    if any(t in s for t in ("failed to resolve", "getaddrinfo", "temporary failure",
                            "network is unreachable", "connection")):
        return "No internet connection. Please connect and try again."
    last = [ln for ln in (stderr or "").strip().splitlines() if ln.strip()]
    return last[-1] if last else "Something went wrong while downloading."
