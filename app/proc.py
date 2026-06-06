"""Subprocess helpers shared by the downloader/audio modules.

Two things every shell-out in this app must never get wrong:

  * **No console flash on Windows** (``CREATE_NO_WINDOW``) and **tolerant text
    decoding** (``utf-8`` / ``errors="replace"``) so a stray non-UTF-8 byte in
    yt-dlp/ffmpeg output can't raise ``UnicodeDecodeError`` and surface to a
    non-technical user as an unexplained crash.

  * **Reliable cancellation.** yt-dlp spawns ffmpeg/deno child processes, and
    ffmpeg itself can sit in a long encode. ``Popen.terminate()`` on Windows
    kills ONLY the top process, orphaning those children — they keep running in
    the background after we've "cancelled", holding the network and file
    handles. And ``readline()`` blocks, so a cancel (or the window closing)
    isn't noticed until the next line arrives — which, for a stalled download,
    is never.

``kill_tree`` kills the whole process tree (``taskkill /T`` on Windows, the
process group on POSIX). ``stream`` runs a process line-by-line with a watcher
thread that kills the tree the instant ``cancel`` is set — which also unblocks
the blocked ``readline()`` — then drains the pipe and reaps the process with a
bounded wait, so we can never hang and never leak a child.

This module NEVER imports tkinter.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from typing import Callable, Optional

_CREATE_NO_WINDOW = 0x08000000


def no_window() -> dict:
    """Popen/run kwargs that suppress a console window on Windows."""
    return {"creationflags": _CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def _new_session() -> dict:
    """Put a child in its own process group (POSIX) so we can kill its tree."""
    return {} if sys.platform == "win32" else {"start_new_session": True}


def _text_kwargs() -> dict:
    """Decode child output as tolerant UTF-8 — never raise on odd bytes."""
    return {"text": True, "encoding": "utf-8", "errors": "replace"}


def run(cmd, **kwargs) -> subprocess.CompletedProcess:
    """``subprocess.run`` with the no-window flag (+ tolerant text if text=True)."""
    if kwargs.get("text"):
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    kwargs.update(no_window())
    return subprocess.run(cmd, **kwargs)


def popen(cmd, **kwargs) -> subprocess.Popen:
    """``subprocess.Popen`` with the no-window flag + POSIX session set."""
    kwargs.update(no_window())
    kwargs.update(_new_session())
    return subprocess.Popen(cmd, **kwargs)


def kill_tree(proc: Optional[subprocess.Popen]) -> None:
    """Kill ``proc`` AND every child it spawned, then reap it. Best-effort.

    yt-dlp -> ffmpeg/deno children must die too, or they keep downloading in the
    background after we 'cancelled'. On Windows ``terminate()`` won't do that;
    ``taskkill /T`` walks the tree. On POSIX we signal the whole process group
    (the child was started with ``start_new_session=True``).
    """
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return  # already gone
    except Exception:
        pass
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=_CREATE_NO_WINDOW)
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def stream(cmd, on_line: Callable[[str], None], cancel=None, **popen_kwargs) -> int:
    """Run ``cmd`` line-by-line, calling ``on_line(raw_line)``; return the exit code.

    stdout + stderr are merged and read a line at a time so progress streams in
    real time. If ``cancel`` (a ``threading.Event``) is set at any point, the
    whole process tree is killed promptly — a watcher thread does it, so the
    ``readline()`` the main thread is blocked in unblocks immediately — and the
    partial output is abandoned. Always drains the pipe and reaps the process
    with a bounded wait: it can't hang, and it can't leave an orphaned child.

    Raises ``FileNotFoundError`` if the binary doesn't exist (the caller maps
    that to a friendly message).
    """
    popen_kwargs.setdefault("stdout", subprocess.PIPE)
    popen_kwargs.setdefault("stderr", subprocess.STDOUT)
    popen_kwargs.update(_text_kwargs())
    proc = popen(cmd, **popen_kwargs)

    stop = threading.Event()
    if cancel is not None:
        def _watch() -> None:
            # Poll cancel; the instant it's set, kill the tree (which unblocks the
            # readline below). Exit cleanly once the read loop signals `stop`.
            while not stop.wait(0.1):
                if cancel.is_set():
                    kill_tree(proc)
                    return
        threading.Thread(target=_watch, daemon=True).start()

    try:
        if proc.stdout is not None:
            for raw in iter(proc.stdout.readline, ""):
                on_line(raw)
    finally:
        stop.set()
        # Drain anything still buffered, then reap. If we got here via cancel the
        # tree is already dying; bound the wait so a wedged child can't hang us.
        try:
            if proc.stdout is not None:
                proc.stdout.read()
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:
            kill_tree(proc)
    return proc.returncode if proc.returncode is not None else 1
