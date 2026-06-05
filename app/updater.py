"""Keep a current yt-dlp binary available, auto-updating on launch.

YouTube changes frequently break a frozen copy of yt-dlp, so we never bake it
into the exe.  Instead:

  * Windows: download ``yt-dlp.exe`` into the per-user cache on first run, then
    run ``yt-dlp --update`` in the background on every later launch.
  * dev (mac/linux): just use the ``yt-dlp`` already on PATH (brew/pip managed).
"""

from __future__ import annotations

import subprocess
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from .paths import cache_dir

YTDLP_WIN_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"

# Stop a console window flashing up when we shell out on Windows.
_CREATE_NO_WINDOW = 0x08000000


def _no_window() -> dict:
    return {"creationflags": _CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def _bin_path() -> Path:
    return cache_dir() / "yt-dlp.exe"


def ensure_ytdlp(status: Optional[Callable[[str], None]] = None) -> str:
    """Return a path/command for yt-dlp, downloading it on first Windows run.

    ``status`` is an optional callback for user-facing progress messages.
    Raises on a failed first-run download (e.g. no internet and no cache).
    """
    if sys.platform != "win32":
        return "yt-dlp"  # dev: assume installed via brew/pip

    exe = _bin_path()
    if not exe.exists():
        if status:
            status("First run: downloading the YouTube engine…")
        _download(YTDLP_WIN_URL, exe)
    return str(exe)


def _download(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(".part")
    req = urllib.request.Request(url, headers={"User-Agent": "AubreysYT-MP3-Downloader"})
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    tmp.replace(dest)


def update_in_background(ytdlp_cmd: str) -> None:
    """Fire-and-forget ``yt-dlp --update``; swallow every error (e.g. offline).

    No-op on dev machines so we never disturb a brew/pip-managed yt-dlp.
    """
    if sys.platform != "win32":
        return

    def _run() -> None:
        try:
            subprocess.run(
                [ytdlp_cmd, "--update"],
                timeout=90,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **_no_window(),
            )
        except Exception:
            pass  # any failure just leaves the cached copy in place

    threading.Thread(target=_run, daemon=True).start()
