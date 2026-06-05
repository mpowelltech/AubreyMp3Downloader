"""Path resolution for bundled resources and the per-user cache.

Works in two situations:
  * dev (running ``python run.py`` from source on any OS)
  * frozen (a PyInstaller ``--onefile`` Windows build)

In a frozen build PyInstaller extracts bundled files to ``sys._MEIPASS`` and
sets ``sys.frozen``.  In dev we fall back to the repo root / the system PATH.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "AubreysYT-MP3Downloader"


def is_frozen() -> bool:
    """True when running inside a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def _base_dir() -> Path:
    """Directory that bundled resources are resolved against."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent  # repo root


def resource_path(rel: str) -> Path:
    """Absolute path to a bundled data/binary file (dev or frozen)."""
    return _base_dir() / rel


def cache_dir() -> Path:
    """Per-user writable dir for the downloaded/auto-updated yt-dlp binary."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def ffmpeg_binary() -> str:
    """Path/command for invoking ffmpeg directly.

    Frozen Windows build -> the bundled ``ffmpeg.exe``.
    Anything else (dev)  -> ``ffmpeg`` from PATH.
    """
    if is_frozen() and sys.platform == "win32":
        return str(resource_path("ffmpeg.exe"))
    return "ffmpeg"


def ffmpeg_location() -> str | None:
    """Value for yt-dlp's ``--ffmpeg-location`` (None = let yt-dlp use PATH)."""
    if is_frozen() and sys.platform == "win32":
        return str(resource_path("ffmpeg.exe"))
    return None
