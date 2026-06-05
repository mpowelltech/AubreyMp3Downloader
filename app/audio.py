"""ffmpeg post-processing: trim, encode to MP3, embed a title tag + cover art."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

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
    total_seconds: Optional[float] = None,
    on_progress: Optional[Callable[[float], None]] = None,
) -> None:
    """Trim to ``[start, end]``, encode MP3 (~190kbps VBR), tag + embed cover.

    When ``total_seconds`` and ``on_progress`` are given, reports conversion
    progress (0..1) parsed from ffmpeg. Falls back to no-cover if embedding the
    thumbnail fails, then raises a user-facing error if even that fails.
    """
    rc, err = _run(_build_cmd(audio_in, out_path, title, start, end, cover), on_progress, total_seconds)
    if rc == 0:
        return
    if cover is not None:
        rc, err = _run(_build_cmd(audio_in, out_path, title, start, end, None), on_progress, total_seconds)
        if rc == 0:
            return
    raise DownloadError("Couldn't convert the audio to MP3. " + (_hint(err) or "Please try again."))


def _build_cmd(audio_in, out_path, title, start, end, cover) -> list[str]:
    # -progress pipe:1 streams machine-readable progress to stdout so we can
    # show a real percentage bar during the (re-encode) conversion.
    cmd = [ffmpeg_binary(), "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1"]
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


# ffmpeg -progress key/value lines we don't want surfaced as error context.
_PROGRESS_KEYS = ("frame=", "fps=", "stream_", "bitrate=", "total_size=", "out_time",
                  "dup_frames=", "drop_frames=", "speed=", "progress=")


def _run(cmd, on_progress=None, total_seconds=None) -> tuple[int, str]:
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **_no_window())
    except FileNotFoundError:
        return 1, "ffmpeg not found"
    except Exception as e:  # pragma: no cover - defensive
        return 1, str(e)

    tail: list[str] = []
    if proc.stdout is not None:
        for raw in iter(proc.stdout.readline, ""):
            line = raw.strip()
            if line.startswith("out_time_us=") and on_progress and total_seconds:
                try:
                    frac = (int(line.split("=", 1)[1]) / 1_000_000) / total_seconds
                    on_progress(max(0.0, min(1.0, frac)))
                except ValueError:
                    pass  # e.g. "out_time_us=N/A" early on
            elif line and not line.startswith(_PROGRESS_KEYS):
                tail.append(line)
                del tail[:-15]
    proc.wait()
    return proc.returncode, "\n".join(tail)


def extract_preview(audio_in: Path, out_wav: Path, start: float, dur: float = 6.0) -> bool:
    """Cut a short WAV snippet for the trim preview. Best-effort: returns False on any error.

    PCM WAV at 44.1 kHz so Windows' ``winsound`` can play it directly.
    """
    cmd = [
        ffmpeg_binary(), "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, start):.3f}", "-i", str(audio_in),
        "-t", f"{max(0.5, dur):.3f}",
        "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(out_wav),
    ]
    try:
        rc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=60, **_no_window()).returncode
    except Exception:
        return False
    return rc == 0 and out_wav.exists() and out_wav.stat().st_size > 1024


def waveform(audio_in: Path, buckets: int = 400):
    """Return a list of ~``buckets`` peaks (0..1) for drawing a waveform, or None.

    Best-effort: decodes to low-rate mono PCM via ffmpeg and reduces to peaks.
    """
    import array
    cmd = [ffmpeg_binary(), "-v", "error", "-i", str(audio_in),
           "-ac", "1", "-ar", "4000", "-f", "s16le", "-"]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=120, **_no_window())
        raw = proc.stdout
        if proc.returncode != 0 or not raw:
            return None
        samples = array.array("h")
        samples.frombytes(raw[: (len(raw) // 2) * 2])
        n = len(samples)
        if n == 0:
            return None
        buckets = max(20, min(buckets, n))
        step = n / buckets
        peaks = []
        peak_max = 1
        for i in range(buckets):
            lo, hi = int(i * step), int((i + 1) * step)
            seg = samples[lo:hi] or samples[lo:lo + 1]
            m = max((abs(s) for s in seg), default=0)
            peaks.append(m)
            peak_max = max(peak_max, m)
        return [p / peak_max for p in peaks]
    except Exception:
        return None


def _hint(err: str) -> str:
    s = (err or "").lower()
    if "no space left" in s:
        return "Your disk is full."
    if "permission denied" in s:
        return "Couldn't write there. Try a different folder."
    return ""
