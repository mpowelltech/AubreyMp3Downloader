"""ffmpeg post-processing: trim, encode to MP3, embed a title tag + cover art."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable, Optional

from . import proc
from .downloader import DownloadError
from .paths import ffmpeg_binary


def make_mp3(
    audio_in: Path,
    out_path: Path,
    title: str,
    start: float = 0.0,
    end: Optional[float] = None,
    cover: Optional[Path] = None,
    total_seconds: Optional[float] = None,
    on_progress: Optional[Callable[[float], None]] = None,
    cancel=None,
) -> None:
    """Trim to ``[start, end]``, encode MP3 (~190kbps VBR), tag + embed cover.

    When ``total_seconds`` and ``on_progress`` are given, reports conversion
    progress (0..1) parsed from ffmpeg. Falls back to no-cover if embedding the
    thumbnail fails, then raises a user-facing error if even that fails. If
    ``cancel`` (a threading.Event) is set, ffmpeg's whole process tree is killed
    and a DownloadError("__CANCELLED__") is raised.

    Encodes to a sibling ``.part.mp3`` and only ``os.replace``s it onto
    ``out_path`` once it's verified non-empty — so a cancel, crash, or empty
    encode can NEVER leave a half-written .mp3 the user might try to import.
    """
    out_path = Path(out_path)
    tmp_out = out_path.with_name(out_path.stem + ".part.mp3")
    try:
        rc, err = _run(_build_cmd(audio_in, tmp_out, title, start, end, cover),
                       on_progress, total_seconds, cancel)
        if rc != 0 and not (cancel is not None and cancel.is_set()) and cover is not None:
            # Cover embedding can fail on odd thumbnails — retry without it.
            rc, err = _run(_build_cmd(audio_in, tmp_out, title, start, end, None),
                           on_progress, total_seconds, cancel)
        if cancel is not None and cancel.is_set():
            raise DownloadError("__CANCELLED__")
        if rc != 0:
            raise DownloadError("Couldn't convert the audio to MP3. "
                                + (_hint(err) or "Please try again."))
        if not tmp_out.exists() or tmp_out.stat().st_size <= 0:
            # ffmpeg said OK but produced nothing usable (e.g. a zero-length clip).
            raise DownloadError("Couldn't convert the audio to MP3. Please try again.")
        os.replace(tmp_out, out_path)  # atomic: out_path appears only when complete
    finally:
        try:
            if tmp_out.exists():
                tmp_out.unlink()  # only reached on failure/cancel (replace consumed it)
        except OSError:
            pass


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


def _run(cmd, on_progress=None, total_seconds=None, cancel=None) -> tuple[int, str]:
    tail: list[str] = []

    def _on_line(raw: str) -> None:
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

    try:
        # proc.stream kills ffmpeg's whole tree the moment `cancel` is set (even
        # mid-readline) and reaps it, so a cancel can't hang or orphan ffmpeg.
        rc = proc.stream(cmd, _on_line, cancel)
    except FileNotFoundError:
        return 1, "ffmpeg not found"
    except Exception as e:  # pragma: no cover - defensive
        return 1, str(e)
    return rc, "\n".join(tail)


def waveform(audio_in: Path, buckets: int = 400):
    """Return a list of ~``buckets`` peaks (0..1) for drawing a waveform, or None.

    Best-effort: decodes to low-rate mono PCM via ffmpeg and reduces to peaks.
    """
    import array
    cmd = [ffmpeg_binary(), "-v", "error", "-i", str(audio_in),
           "-ac", "1", "-ar", "4000", "-f", "s16le", "-"]
    try:
        cp = proc.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                      timeout=120)
        raw = cp.stdout
        if cp.returncode != 0 or not raw:
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
