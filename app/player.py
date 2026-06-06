"""Streaming preview player for the trim screen (experimental).

ffmpeg (already bundled) decodes the per-session source file (.opus/.webm/.m4a)
to raw PCM on the fly; miniaudio pushes that PCM to the OS mixer. Because we feed
the device ourselves we can report an accurate playback position for the moving
playhead, seek instantly (restart the ffmpeg pipe at a new offset), and stream
the whole song with bounded memory (no temp files).

Hard contract: every method is BEST-EFFORT. Any failure (miniaudio missing, no
audio device, ffmpeg hiccup) leaves the player unavailable/stopped and can never
raise into the UI or block a download. This module NEVER imports tkinter.
"""

from __future__ import annotations

import array
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

from .paths import ffmpeg_binary

_CREATE_NO_WINDOW = 0x08000000
_RATE = 48000
_CH = 2
_FRAME_BYTES = _CH * 2  # signed 16-bit stereo


def _no_window() -> dict:
    return {"creationflags": _CREATE_NO_WINDOW} if sys.platform == "win32" else {}


class Player:
    """Owns at most one ffmpeg pipe + one miniaudio device. Thread-safe, best-effort."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._dev = None
        self._src: Optional[Path] = None
        self._dur = 0.0
        self._start = 0.0       # seek offset of the current stream
        self._frames = 0        # PCM frames fed to the device since _start
        self._paused = False
        self._ended = False
        self._active = False
        self._avail: Optional[bool] = None

    # ---- capability (probed once) ----
    def is_available(self) -> bool:
        if self._avail is not None:
            return self._avail
        ok = False
        try:
            import miniaudio
            dev = miniaudio.PlaybackDevice(
                output_format=miniaudio.SampleFormat.SIGNED16, nchannels=_CH, sample_rate=_RATE)
            dev.close()
            ok = True
        except Exception:
            ok = False
        self._avail = ok
        return ok

    # ---- lifecycle ----
    def load(self, path, duration: float) -> bool:
        self.stop()
        try:
            self._src = Path(path) if path else None
            self._dur = float(duration or 0)
            return bool(self._src and self._src.exists())
        except Exception:
            self._src = None
            return False

    def play(self, from_seconds: float = 0.0) -> bool:
        """(Re)start playback at ``from_seconds``. Seeking is just play(new_pos)."""
        if not self.is_available() or not self._src or not self._src.exists():
            return False
        self.stop()
        try:
            import miniaudio
            start = max(0.0, float(from_seconds))
            cmd = [ffmpeg_binary(), "-nostdin", "-loglevel", "quiet",
                   "-ss", f"{start:.3f}", "-i", str(self._src),
                   "-vn", "-ac", str(_CH), "-ar", str(_RATE), "-f", "s16le", "pipe:1"]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    **_no_window())
            dev = miniaudio.PlaybackDevice(
                output_format=miniaudio.SampleFormat.SIGNED16, nchannels=_CH, sample_rate=_RATE)
            with self._lock:
                self._proc = proc
                self._dev = dev
                self._start = start
                self._frames = 0
                self._paused = False
                self._ended = False
                self._active = True
            gen = self._stream(proc)
            next(gen)  # miniaudio requires a primed generator before start()
            dev.start(gen)
            return True
        except Exception:
            self.stop()
            return False

    def _stream(self, proc):
        """miniaudio generator: pull decoded PCM from ffmpeg, yield it to the device."""
        required = yield b""
        while True:
            if not self._active:
                return
            want = int(required) * _FRAME_BYTES
            if self._paused:
                required = yield array.array("h", bytes(want))
                continue
            try:
                data = proc.stdout.read(want)  # buffered: returns `want` bytes unless EOF
            except Exception:
                data = b""
            if not data:
                with self._lock:
                    self._ended = True
                required = yield array.array("h", bytes(want))
                continue
            if len(data) % 2:
                data = data[:-1]
            if len(data) < want:
                with self._lock:
                    self._ended = True
                data = data + bytes(want - len(data))
            with self._lock:
                self._frames += int(required)
            required = yield array.array("h", data)

    def pause(self) -> None:
        with self._lock:
            self._paused = True

    def resume(self) -> None:
        with self._lock:
            self._paused = False

    def stop(self) -> None:
        """Stop playback, kill the ffmpeg pipe, close the device. Any state, any thread.

        NON-BLOCKING: setting _active False makes the audio callback return silence
        immediately (sound stops at once), and the device teardown — miniaudio's
        ma_device_uninit can block for a moment — runs on a daemon thread so it can
        NEVER freeze the Tk main thread (which is what calls stop()).
        """
        with self._lock:
            self._active = False
            proc, dev = self._proc, self._dev
            self._proc = self._dev = None
            self._paused = False
        # Kill ffmpeg FIRST so a generator blocked in read() unblocks.
        try:
            if proc and proc.poll() is None:
                proc.terminate()
        except Exception:
            pass
        if dev is not None:
            threading.Thread(target=self._teardown, args=(dev,), daemon=True).start()

    @staticmethod
    def _teardown(dev) -> None:
        try:
            dev.stop()
        except Exception:
            pass
        try:
            dev.close()
        except Exception:
            pass

    def close(self) -> None:
        self.stop()

    # ---- state (cheap, lock-guarded; the Tk main loop polls these) ----
    def position(self) -> float:
        with self._lock:
            pos = self._start + (self._frames / _RATE)
            dur = self._dur
        return min(pos, dur) if dur > 0 else pos

    def is_playing(self) -> bool:
        with self._lock:
            return self._active and not self._paused and not self._ended

    def is_paused(self) -> bool:
        with self._lock:
            return self._active and self._paused

    def is_active(self) -> bool:
        with self._lock:
            return self._active and not self._ended

    def has_ended(self) -> bool:
        with self._lock:
            return self._ended
