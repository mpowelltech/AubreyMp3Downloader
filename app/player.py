"""Best-effort audio playback for the trim preview (experimental feature).

Plays a short WAV snippet so the user can hear where their trim lands. Every
function is best-effort: if playback isn't possible on this machine it returns
False and the caller just shows a message — it can never break a download.

  * Windows: ``winsound`` (stdlib, plays WAV asynchronously) — always present.
  * macOS:   ``afplay`` (ships with the OS).
  * Linux:   ``paplay`` / ``aplay`` if available (dev only).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

_CREATE_NO_WINDOW = 0x08000000
_proc: Optional[subprocess.Popen] = None  # current macOS/Linux player process


def _no_window() -> dict:
    return {"creationflags": _CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def stop() -> None:
    """Stop anything currently playing (no-op if nothing is)."""
    global _proc
    if sys.platform == "win32":
        try:
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass
        return
    if _proc is not None:
        try:
            if _proc.poll() is None:
                _proc.terminate()
        except Exception:
            pass
        _proc = None


def play(path: Path) -> bool:
    """Start playing ``path`` (a WAV) asynchronously. Returns False if it couldn't."""
    global _proc
    stop()
    try:
        if not Path(path).exists():
            return False
        if sys.platform == "win32":
            import winsound
            winsound.PlaySound(
                str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            return True
        if sys.platform == "darwin":
            _proc = subprocess.Popen(["afplay", str(path)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        for player in ("paplay", "aplay"):
            try:
                _proc = subprocess.Popen([player, str(path)],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except FileNotFoundError:
                continue
        return False
    except Exception:
        return False
