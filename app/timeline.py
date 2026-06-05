"""A draggable trim timeline for the single-song screen.

Shows the whole song as a bar, with two draggable handles for the start and end
of the kept section. The kept part is highlighted; the trimmed-off parts are
dimmed. If a waveform is available it's drawn behind. During a preview it shows
which slice is playing and a moving playhead. The mm:ss text boxes remain the
precise source of truth; this widget keeps them in sync via ``on_change``.

Self-contained (tkinter Canvas) so it never touches the worker/queue machinery.
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable, Optional

import customtkinter as ctk

# (light, dark) hex pairs — Canvas needs a concrete colour, not a CTk tuple.
_C = {
    "bg":        ("#FCE7F0", "#2A2026"),
    "track":     ("#EAD3DF", "#3A2D34"),   # trimmed-off region
    "keep":      ("#F6C7DD", "#5C3B4B"),   # kept region
    "wave_cut":  ("#CBA9B9", "#5A4A52"),
    "wave_keep": ("#D9729E", "#D98FB4"),
    "handle":    ("#B5688A", "#E184AA"),
    "playwin":   ("#7FC2A0", "#5E9E78"),
    "playhead":  ("#7A2E50", "#FDE7F1"),
    "disabled":  ("#E4D7DD", "#352830"),
    "text":      ("#8A6B78", "#B79AA8"),
}

PAD = 12
GRAB = 14


def _hex(name: str) -> str:
    return _C[name][1 if ctk.get_appearance_mode() == "Dark" else 0]


class TrimTimeline(ctk.CTkFrame):
    def __init__(self, master, on_change: Callable[[float, float], None], height: int = 60) -> None:
        super().__init__(master, fg_color="transparent")
        self.on_change = on_change
        self.duration = 0.0
        self.start = 0.0
        self.end = 0.0
        self.peaks: Optional[list] = None
        self._play: Optional[tuple] = None     # (a, b) slice being previewed
        self._playhead: Optional[float] = None
        self._enabled = False
        self._locked = False                   # True outside the loaded/done state
        self._drag: Optional[str] = None       # 'start' | 'end' | None

        self.canvas = tk.Canvas(self, height=height, highlightthickness=0, bd=0)
        self.canvas.pack(fill="x", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self._redraw())
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", lambda _e: setattr(self, "_drag", None))

    # ---- public API ----
    def set_duration(self, dur: float) -> None:
        self.duration = max(0.0, float(dur or 0))
        self._enabled = self.duration > 0
        if self._enabled:
            self.start = 0.0
            self.end = self.duration
        self.peaks = None
        self._play = None
        self._playhead = None
        self._redraw()

    def set_trim(self, start: float, end: float) -> None:
        if self.duration <= 0:
            return
        self.start = max(0.0, min(start, self.duration))
        self.end = max(self.start, min(end, self.duration))
        self._redraw()

    def set_waveform(self, peaks) -> None:
        self.peaks = peaks or None
        self._redraw()

    def set_play(self, a: float, b: float) -> None:
        self._play = (a, b)
        self._playhead = a
        self._redraw()

    def set_playhead(self, t: float) -> None:
        self._playhead = t
        self._redraw()

    def clear_play(self) -> None:
        self._play = None
        self._playhead = None
        self._redraw()

    def set_locked(self, locked: bool) -> None:
        self._locked = bool(locked)

    # ---- geometry ----
    def _cwidth(self) -> int:
        # NB: don't name this `_w` — tkinter widgets already use self._w internally.
        return max(1, self.canvas.winfo_width())

    def _x(self, t: float) -> float:
        usable = self._cwidth() - 2 * PAD
        if self.duration <= 0:
            return PAD
        return PAD + (t / self.duration) * usable

    def _t(self, x: float) -> float:
        usable = self._cwidth() - 2 * PAD
        if usable <= 0 or self.duration <= 0:
            return 0.0
        return max(0.0, min(1.0, (x - PAD) / usable)) * self.duration

    # ---- interaction ----
    def _on_press(self, event) -> None:
        if not self._enabled or self._locked:
            return
        x = event.x
        sx, ex = self._x(self.start), self._x(self.end)
        if abs(x - sx) <= GRAB and abs(x - ex) <= GRAB:
            self._drag = "start" if x <= (sx + ex) / 2 else "end"
        elif abs(x - sx) <= GRAB:
            self._drag = "start"
        elif abs(x - ex) <= GRAB:
            self._drag = "end"
        else:
            # click in the body: move whichever handle is nearer to where you clicked
            self._drag = "start" if abs(x - sx) <= abs(x - ex) else "end"
        self._on_motion(event)

    def _on_motion(self, event) -> None:
        if not self._enabled or not self._drag:
            return
        t = self._t(event.x)
        gap = max(0.5, self.duration * 0.005)  # keep handles from crossing
        if self._drag == "start":
            self.start = max(0.0, min(t, self.end - gap))
        else:
            self.end = min(self.duration, max(t, self.start + gap))
        self._redraw()
        try:
            self.on_change(self.start, self.end)
        except Exception:
            pass

    # ---- drawing ----
    def _redraw(self) -> None:
        c = self.canvas
        c.delete("all")
        w, h = self._cwidth(), int(c.winfo_height() or 60)
        top, bot = 8, h - 8
        midy = (top + bot) / 2

        if not self._enabled:
            c.create_rectangle(PAD, top, w - PAD, bot, fill=_hex("disabled"), outline="")
            c.create_text(w / 2, midy, text="Length unknown - trim by typing a Start time",
                          fill=_hex("text"), font=("", 11))
            return

        sx, ex = self._x(self.start), self._x(self.end)
        # base track (trimmed-off look), then the kept region highlighted
        c.create_rectangle(PAD, top, w - PAD, bot, fill=_hex("track"), outline="")
        c.create_rectangle(sx, top, ex, bot, fill=_hex("keep"), outline="")

        # waveform (mirror bars), coloured by whether each bar is kept or cut
        if self.peaks:
            n = len(self.peaks)
            usable = w - 2 * PAD
            half = (bot - top) / 2 - 1
            keep_c, cut_c = _hex("wave_keep"), _hex("wave_cut")
            for i, p in enumerate(self.peaks):
                bx = PAD + (i + 0.5) / n * usable
                amp = max(1.0, p * half)
                col = keep_c if sx <= bx <= ex else cut_c
                c.create_line(bx, midy - amp, bx, midy + amp, fill=col)
        else:
            c.create_line(PAD, midy, w - PAD, midy, fill=_hex("wave_cut"))

        # the slice being previewed + a moving playhead
        if self._play:
            a, b = self._play
            c.create_rectangle(self._x(a), top, self._x(b), top + 4, fill=_hex("playwin"), outline="")
        if self._playhead is not None:
            px = self._x(self._playhead)
            c.create_line(px, top, px, bot, fill=_hex("playhead"), width=2)

        # handles
        hc = _hex("handle")
        for hx in (sx, ex):
            c.create_line(hx, top - 1, hx, bot + 1, fill=hc, width=3)
            c.create_oval(hx - 6, midy - 9, hx + 6, midy + 9, fill=hc, outline="")
