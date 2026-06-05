"""ffmpeg post-processing: trim, encode to MP3, embed a title tag + cover art."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

from .downloader import DownloadError
from .paths import ffmpeg_binary

_CREATE_NO_WINDOW = 0x08000000


def _no_window() -> dict:
    return {"creationflags": _CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def make_mp3(
    audio_in: Path,
    out_path: Path,
    title: str,
    start: float = 0.0,
    end: Optional[float] = None,
    cover: Optional[Path] = None,
) -> None:
    """Trim to ``[start, end]``, encode MP3 (~190kbps VBR), tag + embed cover.

    Falls back to no-cover if embedding the thumbnail fails, then raises a
    user-facing error if even that fails.
    """
    rc, err = _run(_build_cmd(audio_in, out_path, title, start, end, cover))
    if rc == 0:
        return
    if cover is not None:
        rc, err = _run(_build_cmd(audio_in, out_path, title, start, end, None))
        if rc == 0:
            return
    raise DownloadError("Couldn't convert the audio to MP3. " + (_hint(err) or "Please try again."))


def _build_cmd(audio_in, out_path, title, start, end, cover) -> list[str]:
    cmd = [ffmpeg_binary(), "-y", "-hide_banner", "-loglevel", "error"]
    if start and start > 0:
        cmd += ["-ss", f"{start:.3f}"]          # fast input seek
    cmd += ["-i", str(audio_in)]
    if cover is not None:
        cmd += ["-i", str(cover)]
    if end is not None and end > (start or 0):
        cmd += ["-t", f"{end - (start or 0):.3f}"]  # output duration

    cmd += ["-map", "0:a:0", "-c:a", "libmp3lame", "-q:a", "2"]
    if cover is not None:
        cmd += [
            "-map", "1:v:0", "-c:v", "copy",
            "-disposition:v:0", "attached_pic",
            "-metadata:s:v", "title=Album cover",
            "-metadata:s:v", "comment=Cover (front)",
        ]
    cmd += ["-metadata", f"title={title}", "-id3v2_version", "3", str(out_path)]
    return cmd


def _run(cmd) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, **_no_window())
        return proc.returncode, proc.stderr or ""
    except FileNotFoundError:
        return 1, "ffmpeg not found"
    except Exception as e:  # pragma: no cover - defensive
        return 1, str(e)


def _hint(err: str) -> str:
    s = (err or "").lower()
    if "no space left" in s:
        return "Your disk is full."
    if "permission denied" in s:
        return "Couldn't write there — try a different folder."
    return ""
