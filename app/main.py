"""Aubrey's YT-MP3 Downloader — paste a link (or search), trim, export an MP3.

A guided, can't-go-wrong wizard. A small state machine (see ``_set_state``)
locks every control that isn't usable yet, so the only thing you *can* do is
the next correct step:

    starting -> empty -> loading -> loaded -> downloading -> done
                 ^                                              |
                 +---------------- New video --------------------+

Step 1 has two ways in: paste a link (YouTube and many other sites), or search
by name. All slow work (resolving yt-dlp/deno, searching, fetching, downloading,
converting) runs on worker threads that post messages to a thread-safe queue
drained by the Tk main loop via ``after`` — so the UI stays responsive and we
never touch widgets off the main thread.
"""

from __future__ import annotations

import math
import queue
import re
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from . import __version__, player
from .audio import extract_preview, make_mp3
from .downloader import (DownloadError, VideoInfo, download_audio, fetch_info,
                         has_playlist, has_single_video, looks_like_url, search)
from .media import fetch_image
from .paths import ffmpeg_binary, resource_path
from .updater import (check_for_app_update, download_and_relaunch, ensure_deno,
                      ensure_ytdlp, update_in_background)

APP_TITLE = "Aubrey's YT-MP3 Downloader"

# --- pastel-pink palette (light, dark) ---
PINK        = ("#E184AA", "#B5688A")
PINK_HOVER  = ("#D9729E", "#A4587B")
WINDOW_BG   = ("#FDF1F7", "#1C1418")
CARD_BG     = ("#FCE7F0", "#2A2026")
CARD_BORDER = ("#F3CFE0", "#3A2D34")
BADGE_PEND  = ("#E9D6DF", "#4A3D44")
BADGE_PEND_T = ("gray38", "gray70")
DONE        = ("#7FC2A0", "#5E9E78")
SECONDARY   = ("gray82", "gray30")
SECONDARY_H = ("gray73", "gray40")
MUTED       = ("gray40", "gray65")
TITLE_ON    = ("gray10", "gray95")
DISABLED_BG = ("gray82", "gray32")   # greyed-out action buttons (not pink)
DISABLED_TX = ("gray55", "gray58")
ERR_TX      = ("#C0392B", "#E57373")
THUMB_BG    = ("#F3D9E6", "#352830")

MODE_LINK   = "Paste a link"
MODE_SEARCH = "Search by name"

# Badge appearance for each step depending on the current state.
_BADGES = {
    "starting":    {1: "pending", 2: "pending", 3: "pending"},
    "empty":       {1: "active",  2: "pending", 3: "pending"},
    "loading":     {1: "active",  2: "pending", 3: "pending"},
    "loaded":      {1: "done",    2: "active",  3: "active"},
    "downloading": {1: "done",    2: "active",  3: "active"},
    "done":        {1: "done",    2: "done",    3: "done"},
    "failed":      {1: "pending", 2: "pending", 3: "pending"},
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def parse_time(text: str) -> float:
    """Parse 'mm:ss', 'h:mm:ss' or plain seconds into seconds."""
    text = (text or "").strip()
    if not text:
        return 0.0
    try:
        if ":" in text:
            secs = 0.0
            for part in text.split(":"):
                secs = secs * 60 + float(part or 0)
        else:
            secs = float(text)
    except ValueError:
        raise ValueError(f"'{text}' isn't a valid time. Use mm:ss (e.g. 1:05).")
    # Reject inf/nan (float() accepts them) so a garbage time can't reach ffmpeg.
    if not math.isfinite(secs):
        raise ValueError(f"'{text}' isn't a valid time. Use mm:ss (e.g. 1:05).")
    return secs


def fmt_time(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    m, s = divmod(total, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', " ", name or "").strip()
    name = re.sub(r"\s+", " ", name)
    return (name or "audio")[:120]


def clean_url(text: str) -> str:
    """Pull a single usable link out of a messy paste (quotes, < >, extra lines)."""
    t = (text or "").strip().strip("<>“”\"'")
    for tok in t.split():
        if looks_like_url(tok):
            return tok.strip("<>“”\"'")
    return t


def _verify_ffmpeg() -> None:
    """Raise if ffmpeg isn't available (bundled in frozen builds, on PATH in dev)."""
    import os
    import shutil
    ff = ffmpeg_binary()
    ok = os.path.exists(ff) if os.path.isabs(ff) else shutil.which(ff) is not None
    if not ok:
        raise RuntimeError("ffmpeg is missing from the app. Please reinstall the app.")


def open_folder(folder: Path) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["explorer", str(folder)])
        elif sys.platform == "darwin":
            subprocess.run(["open", str(folder)])
        else:
            subprocess.run(["xdg-open", str(folder)])
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")

        self.title(APP_TITLE)
        # Logical sizes — customtkinter scales these up by the display DPI, so
        # this is correct on HiDPI (don't measure/raw-set: DPI is unknown until
        # the window maps). Overlays (search/bulk) sit on top; lists scroll.
        self.geometry("800x690")
        self.minsize(780, 660)
        self.configure(fg_color=WINDOW_BG)
        self._apply_icon()

        self.info: VideoInfo | None = None
        self.ytdlp: str | None = None
        self.deno: str | None = None
        self.ready = False
        self.flow_state = "starting"
        self.loaded_url: str | None = None
        self._indet = False
        self._syncing = False    # guards the Start/End <-> Skip first/last mirror
        self._searching = False  # a name-search is running
        self._closing = False    # set in destroy(); stops _poll rescheduling
        self._trim_ok = True     # current trim values parse + make sense
        self._bulk = None        # the bulk overlay frame, when open
        self._chooser = None     # the search-results overlay, when open
        self._preview_img = None # keep a ref so the CTkImage isn't garbage-collected
        # --- trim audio preview (experimental) ---
        self._preview_dir = Path(tempfile.mkdtemp(prefix="aubreyprev_"))
        self._preview_busy = False
        self._cache_url: str | None = None   # which video the cached audio belongs to
        self._cache_audio: Path | None = None
        self._cache_thumb: Path | None = None
        self._badges: dict[int, ctk.CTkLabel] = {}
        self._titles: dict[int, ctk.CTkLabel] = {}
        self.q: "queue.Queue[tuple[str, object]]" = queue.Queue()

        self._build_ui()
        self.after(0, self._close_splash)   # dismiss the PyInstaller splash now the window is up
        self.after(100, self._poll)
        self._start_engine()

    # ---- UI construction ------------------------------------------------- #

    def _step(self, parent, number: int, title: str) -> tuple:
        """Create a numbered 'step' card; store its badge/title; return (card, body)."""
        card = ctk.CTkFrame(parent, corner_radius=12, fg_color=CARD_BG,
                            border_width=1, border_color=CARD_BORDER)
        card.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 0))
        badge = ctk.CTkLabel(
            head, text=str(number), width=26, height=26, corner_radius=13,
            font=ctk.CTkFont(size=13, weight="bold"))
        badge.grid(row=0, column=0, padx=(0, 8))
        title_lbl = ctk.CTkLabel(head, text=title, font=ctk.CTkFont(size=14, weight="bold"))
        title_lbl.grid(row=0, column=1, sticky="w")
        self._badges[number] = badge
        self._titles[number] = title_lbl

        body = ctk.CTkFrame(card, fg_color="transparent")
        body.grid(row=1, column=0, sticky="ew", padx=14, pady=(6, 12))
        body.grid_columnconfigure(0, weight=1)
        return card, body

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        PADX = 16

        # --- Header (logo + title + actions) ---
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=PADX, pady=(12, 2))
        tcol, ncol = 0, 1
        try:  # logo next to the title (skipped gracefully if Pillow is missing)
            from PIL import Image
            self._logo = ctk.CTkImage(
                light_image=Image.open(resource_path("assets/icon_header.png")), size=(62, 62))
            ctk.CTkLabel(header, text="", image=self._logo).grid(row=0, column=0, padx=(0, 12))
            tcol, ncol = 1, 2
        except Exception:
            pass
        header.grid_columnconfigure(tcol, weight=1)
        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.grid(row=0, column=tcol, sticky="w")
        ctk.CTkLabel(titles, text=APP_TITLE, font=ctk.CTkFont(size=21, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(
            titles, text="Turn a song from the web into an MP3 for the Yoto player.",
            text_color=MUTED, font=ctk.CTkFont(size=12)).pack(anchor="w")
        ctk.CTkLabel(
            titles, text="Built by Matt for my favourite niece ♥",
            text_color=PINK, font=ctk.CTkFont(size=11, weight="bold")).pack(anchor="w")
        right = ctk.CTkFrame(header, fg_color="transparent")
        right.grid(row=0, column=ncol, sticky="e", padx=(8, 0))
        self.several_btn = ctk.CTkButton(
            right, text="☰  Download several", width=160, height=30, fg_color=SECONDARY,
            hover_color=SECONDARY_H, text_color=TITLE_ON, command=self._on_several)
        self.several_btn.pack(fill="x")
        self.new_btn = ctk.CTkButton(
            right, text="↺  Start over", width=160, height=30, fg_color=SECONDARY,
            hover_color=SECONDARY_H, text_color=TITLE_ON, command=self._on_new)
        self.new_btn.pack(fill="x", pady=(5, 0))

        # --- Step 1: find the song (link OR search) ---
        c1, b1 = self._step(self, 1, "Find your song")
        c1.grid(row=1, column=0, sticky="ew", padx=PADX, pady=4)

        self.mode_var = tk.StringVar(value=MODE_LINK)
        self.mode_seg = ctk.CTkSegmentedButton(
            b1, values=[MODE_LINK, MODE_SEARCH], variable=self.mode_var,
            command=self._on_mode_change, height=30,
            selected_color=PINK, selected_hover_color=PINK_HOVER,
            unselected_color=SECONDARY, unselected_hover_color=SECONDARY_H,
            text_color=TITLE_ON, font=ctk.CTkFont(size=12, weight="bold"))
        self.mode_seg.grid(row=0, column=0, sticky="w", pady=(0, 8))

        row1 = ctk.CTkFrame(b1, fg_color="transparent")
        row1.grid(row=1, column=0, sticky="ew")
        row1.grid_columnconfigure(0, weight=1)
        self.url_var = tk.StringVar()
        self.url_entry = ctk.CTkEntry(
            row1, textvariable=self.url_var, height=42,
            placeholder_text="https://www.youtube.com/watch?v=…")
        self.url_entry.grid(row=0, column=0, sticky="ew")
        self.url_entry.bind("<Return>", lambda _e: self._on_go())
        # Paste lives INSIDE the box (subtle), so it doesn't compete with the action.
        self.paste_btn = ctk.CTkButton(
            row1, text="Paste", width=58, height=28, fg_color=SECONDARY,
            hover_color=SECONDARY_H, text_color=TITLE_ON, command=self._on_paste)
        self.paste_btn.place(in_=self.url_entry, relx=1.0, rely=0.5, x=-6, anchor="e")
        self.go_btn = ctk.CTkButton(
            row1, text="Get info  →", width=128, height=42,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=PINK, hover_color=PINK_HOVER, command=self._on_go)
        self.go_btn.grid(row=0, column=1, padx=(10, 0))
        self.url_var.trace_add("write", lambda *_: self._refresh_go_btn())
        self.hint1_var = tk.StringVar(
            value="Paste a link, then click “Get info” to load the song.")
        ctk.CTkLabel(
            b1, textvariable=self.hint1_var, text_color=MUTED, font=ctk.CTkFont(size=12),
            wraplength=720, justify="left").grid(row=2, column=0, sticky="w", pady=(7, 0))
        ctk.CTkLabel(
            b1, text="Works with YouTube, plus Vimeo, SoundCloud and many more "
                     "(non-YouTube sites are experimental).",
            text_color=MUTED, font=ctk.CTkFont(size=11, slant="italic"),
            wraplength=720, justify="left").grid(row=3, column=0, sticky="w", pady=(2, 0))

        # --- Steps 2 & 3 side-by-side (uses the width instead of stacking tall) ---
        cols = ctk.CTkFrame(self, fg_color="transparent")
        cols.grid(row=2, column=0, sticky="ew", padx=PADX)
        cols.grid_columnconfigure((0, 1), weight=1, uniform="step")

        c2, b2 = self._step(cols, 2, "Check the song")
        c2.grid(row=0, column=0, sticky="nsew", padx=(0, 5), pady=4)
        b2.grid_columnconfigure(1, weight=1)
        self.thumb_lbl = ctk.CTkLabel(
            b2, text="♪", width=76, height=76, corner_radius=10,
            fg_color=THUMB_BG, text_color=MUTED, font=ctk.CTkFont(size=30))
        self.thumb_lbl.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 10), pady=(0, 2))
        self.title_var = tk.StringVar()
        self.title_entry = ctk.CTkEntry(b2, textvariable=self.title_var,
                                        placeholder_text="(loads after step 1)")
        self.title_entry.grid(row=0, column=1, sticky="ew")
        self.meta_var = tk.StringVar(value="Length: –")
        ctk.CTkLabel(b2, textvariable=self.meta_var, text_color=MUTED,
                     anchor="w", font=ctk.CTkFont(size=12)).grid(
            row=1, column=1, sticky="ew", pady=(4, 0))
        ctk.CTkLabel(
            b2, text="This is the name shown under the track in the Yoto app. Edit it if you like.",
            text_color=MUTED, font=ctk.CTkFont(size=12), wraplength=340, justify="left",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))

        c3, b3 = self._step(cols, 3, "Trim  (optional)")
        c3.grid(row=0, column=1, sticky="nsew", padx=(5, 0), pady=4)
        times = ctk.CTkFrame(b3, fg_color="transparent")
        times.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(times, text="Start").grid(row=0, column=0, padx=(0, 4))
        self.start_var = tk.StringVar(value="0:00")
        self.start_entry = ctk.CTkEntry(times, textvariable=self.start_var, width=66)
        self.start_entry.grid(row=0, column=1)
        ctk.CTkLabel(times, text="End").grid(row=0, column=2, padx=(12, 4))
        self.end_var = tk.StringVar()
        self.end_entry = ctk.CTkEntry(times, textvariable=self.end_var, width=66, placeholder_text="end")
        self.end_entry.grid(row=0, column=3)
        ctk.CTkLabel(times, text="(mm:ss)", text_color=MUTED).grid(row=0, column=4, padx=(6, 0))
        ctk.CTkLabel(b3, text="or", text_color=MUTED, font=ctk.CTkFont(size=12, slant="italic")).grid(
            row=1, column=0, sticky="w", pady=(4, 0))
        quick = ctk.CTkFrame(b3, fg_color="transparent")
        quick.grid(row=2, column=0, sticky="ew", pady=(2, 0))
        ctk.CTkLabel(quick, text="Skip first").grid(row=0, column=0, padx=(0, 4))
        self.skipfirst_var = tk.StringVar(value="0")
        self.skipfirst_entry = ctk.CTkEntry(quick, textvariable=self.skipfirst_var, width=46)
        self.skipfirst_entry.grid(row=0, column=1)
        ctk.CTkLabel(quick, text="sec").grid(row=0, column=2, padx=(3, 0))
        ctk.CTkLabel(quick, text="Skip last").grid(row=0, column=3, padx=(12, 4))
        self.skiplast_var = tk.StringVar(value="0")
        self.skiplast_entry = ctk.CTkEntry(quick, textvariable=self.skiplast_var, width=46)
        self.skiplast_entry.grid(row=0, column=4)
        ctk.CTkLabel(quick, text="sec").grid(row=0, column=5, padx=(3, 0))
        # Start/End and Skip first/last are two views of the SAME trim: editing
        # one updates the other live. self._syncing breaks the feedback loop.
        self.skipfirst_var.trace_add("write", lambda *_: self._skip_first())
        self.skiplast_var.trace_add("write", lambda *_: self._skip_last())
        self.start_var.trace_add("write", lambda *_: self._start_changed())
        self.end_var.trace_add("write", lambda *_: self._end_changed())

        # Preview: hear a few seconds at each cut point (experimental).
        prev = ctk.CTkFrame(b3, fg_color="transparent")
        prev.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.hear_start_btn = ctk.CTkButton(
            prev, text="▶  Hear start", width=120, height=30, fg_color=SECONDARY,
            hover_color=SECONDARY_H, text_color=TITLE_ON, command=lambda: self._on_preview("start"))
        self.hear_start_btn.grid(row=0, column=0, padx=(0, 6))
        self.hear_end_btn = ctk.CTkButton(
            prev, text="▶  Hear end", width=120, height=30, fg_color=SECONDARY,
            hover_color=SECONDARY_H, text_color=TITLE_ON, command=lambda: self._on_preview("end"))
        self.hear_end_btn.grid(row=0, column=1)
        self._preview_btns = [self.hear_start_btn, self.hear_end_btn]
        ctk.CTkLabel(
            b3, text="Tip: click Hear start / Hear end to listen to those few seconds before "
                     "you save (experimental). Two ways to set the same trim — Start/End times "
                     "or Skip first/last seconds — change either and the other matches.",
            text_color=MUTED, font=ctk.CTkFont(size=12), wraplength=340, justify="left",
        ).grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.trim_hint_var = tk.StringVar(value="")
        ctk.CTkLabel(b3, textvariable=self.trim_hint_var, text_color=ERR_TX,
                     font=ctk.CTkFont(size=12, weight="bold"), wraplength=340,
                     justify="left").grid(row=5, column=0, sticky="w", pady=(2, 0))

        # --- Download (primary action) ---
        self.download_btn = ctk.CTkButton(
            self, text="Download MP3", height=46,
            font=ctk.CTkFont(size=15, weight="bold"),
            fg_color=PINK, hover_color=PINK_HOVER, command=self._on_download)
        self.download_btn.grid(row=3, column=0, sticky="ew", padx=PADX, pady=(8, 4))

        # --- Footer: progress + status ---
        self.progress = ctk.CTkProgressBar(self, progress_color=PINK)
        self.progress.grid(row=4, column=0, sticky="ew", padx=PADX, pady=(4, 2))
        self.progress.set(0)
        self.status_var = tk.StringVar(value="Starting up…")
        ctk.CTkLabel(
            self, textvariable=self.status_var, anchor="w", justify="left",
            wraplength=780, text_color=MUTED).grid(row=5, column=0, sticky="ew", padx=PADX, pady=(0, 12))

        self._step3_widgets = [
            self.start_entry, self.end_entry, self.skipfirst_entry, self.skiplast_entry,
        ]

    # ---- window icon + progress bar (main thread only) ------------------- #

    def _close_splash(self) -> None:
        try:
            import pyi_splash
            pyi_splash.close()
        except Exception:
            pass

    def _apply_icon(self) -> None:
        try:
            ico = resource_path("assets/icon.ico")
            if sys.platform == "win32" and ico.exists():
                self.iconbitmap(default=str(ico))  # title bar + taskbar on Windows
                return
            png = resource_path("assets/icon.png")
            if png.exists():
                self._icon_img = tk.PhotoImage(file=str(png))
                self.iconphoto(True, self._icon_img)
        except Exception:
            pass

    def _bar_indeterminate(self) -> None:
        if not self._indet:
            self.progress.configure(mode="indeterminate")
            self.progress.start()
            self._indet = True

    def _bar_set(self, value: float) -> None:
        if self._indet:
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self._indet = False
        try:
            self.progress.set(max(0.0, min(1.0, float(value))))
        except (TypeError, ValueError):
            self.progress.set(0)

    # ---- the state machine ----------------------------------------------- #

    def _enable(self, widgets, on: bool) -> None:
        for w in widgets:
            try:
                w.configure(state="normal" if on else "disabled")
            except Exception:
                pass

    def _set_action(self, btn, on: bool) -> None:
        """Pink + readable when enabled; plain grey (not pink) when disabled."""
        if on:
            btn.configure(state="normal", fg_color=PINK, hover_color=PINK_HOVER, text_color="white")
        else:
            btn.configure(state="disabled", fg_color=DISABLED_BG, hover_color=DISABLED_BG,
                          text_color=DISABLED_TX)

    def _set_badge(self, n: int, kind: str) -> None:
        badge, title = self._badges[n], self._titles[n]
        if kind == "active":
            badge.configure(text=str(n), fg_color=PINK, text_color="white")
            title.configure(text_color=TITLE_ON)
        elif kind == "done":
            badge.configure(text="✓", fg_color=DONE, text_color="white")
            title.configure(text_color=TITLE_ON)
        else:  # pending
            badge.configure(text=str(n), fg_color=BADGE_PEND, text_color=BADGE_PEND_T)
            title.configure(text_color=MUTED)

    def _set_state(self, state: str) -> None:
        self.flow_state = state
        editing_link = state == "empty" and not self._searching
        loaded = state in ("loaded", "done")

        self._enable([self.url_entry, self.paste_btn, self.mode_seg], editing_link)
        self._enable([self.title_entry], loaded)
        self._enable(self._step3_widgets, loaded)
        self._set_preview_enabled(loaded and not self._preview_busy)
        self._refresh_download_btn()
        self.new_btn.configure(state="normal" if (loaded and not self._preview_busy) else "disabled")
        self.several_btn.configure(
            state="normal" if (self.ready and not self._searching and not self._preview_busy
                               and state in ("empty", "loaded", "done")) else "disabled")

        for n, kind in _BADGES[state].items():
            self._set_badge(n, kind)
        self._refresh_go_btn()

    def _set_preview_enabled(self, on: bool) -> None:
        st = "normal" if on else "disabled"
        for b in getattr(self, "_preview_btns", []):
            try:
                b.configure(state=st)
            except Exception:
                pass

    def _refresh_download_btn(self) -> None:
        ok = self.flow_state in ("loaded", "done") and self._trim_ok and not self._preview_busy
        self._set_action(self.download_btn, ok)

    def _refresh_go_btn(self) -> None:
        ok = (self.flow_state == "empty" and self.ready and not self._searching
              and bool(self.url_var.get().strip()))
        self._set_action(self.go_btn, ok)

    def _on_mode_change(self, _value: str = "") -> None:
        if self.flow_state != "empty" or self._searching:
            self.mode_var.set(MODE_LINK if self._searching else self.mode_var.get())
            return
        if self.mode_var.get() == MODE_SEARCH:
            self.url_entry.configure(placeholder_text="e.g. Twinkle Twinkle Little Star")
            self.go_btn.configure(text="Search  →")
            self.hint1_var.set("Type a song or artist name and click Search, then pick from the list.")
        else:
            self.url_entry.configure(placeholder_text="https://www.youtube.com/watch?v=…")
            self.go_btn.configure(text="Get info  →")
            self.hint1_var.set("Paste a link, then click “Get info” to load the song.")
        self._refresh_go_btn()

    # ---- background: resolve yt-dlp + deno ------------------------------- #

    def _start_engine(self) -> None:
        """Resolve yt-dlp/deno (downloading on first run) + verify ffmpeg.

        Called at launch and again by the Retry button after a setup failure.
        """
        self._set_state("starting")
        self._status("Starting up…")
        self._bar_indeterminate()
        threading.Thread(target=self._init_engine, daemon=True).start()

    def _init_engine(self) -> None:
        try:
            _verify_ffmpeg()  # bundled exe (frozen) / on PATH (dev) — fail loudly if absent
            cmd = ensure_ytdlp(status=lambda m: self.q.put(("status", m)))
            self.q.put(("ytdlp", cmd))
            update_in_background(cmd)
            deno = ensure_deno(status=lambda m: self.q.put(("status", m)))
            self.q.put(("deno", deno))
            self.q.put(("ready", None))
            self.q.put(("status", "Ready. Paste a link, or search by name."))
            info = check_for_app_update(__version__)  # no-op unless frozen Windows + newer release
            if info:
                self.q.put(("update_available", info))
        except Exception as e:
            self.q.put(("setup_error", str(e)))

    # ---- step 1: go (link / search dispatch) ----------------------------- #

    def _on_paste(self) -> None:
        try:
            self.url_var.set(self.clipboard_get().strip())
        except tk.TclError:
            return
        self.url_entry.focus_set()

    def _on_go(self) -> None:
        if self.flow_state != "empty" or not self.ready or self._searching:
            return
        raw = self.url_var.get().strip()
        if not raw:
            return
        if self.mode_var.get() == MODE_SEARCH and not looks_like_url(raw):
            self._run_search(raw)
            return
        # Link mode (or a link pasted into search): make sure it really is a link.
        url = clean_url(raw)
        if not looks_like_url(url):
            self.mode_var.set(MODE_SEARCH)
            self._on_mode_change()
            self._status("That doesn't look like a link, so I switched to "
                         "“Search by name”. Click Search to find it.")
            return
        if has_playlist(url):
            self._handle_playlist_link(url)
            return
        self._load_url(url)

    def _handle_playlist_link(self, url: str) -> None:
        """A link that carries a playlist: offer the whole list vs. one song."""
        if has_single_video(url):
            ans = messagebox.askyesnocancel(
                APP_TITLE,
                "This link includes a whole playlist.\n\n"
                "• Yes  – get every song (opens “Download several”)\n"
                "• No   – just this one song\n"
                "• Cancel – do nothing")
            if ans is None:
                return
            if ans:
                self._open_bulk_with_playlist(url)
            else:
                self._load_url(url)
        else:
            if messagebox.askyesno(
                APP_TITLE,
                "This looks like a playlist of songs.\n\n"
                "Open “Download several” to grab them all?"):
                self._open_bulk_with_playlist(url)

    def _load_url(self, url: str) -> None:
        self.loaded_url = url
        self._set_state("loading")
        self._status("Reading the details…")
        self._bar_indeterminate()
        threading.Thread(target=self._fetch_worker, args=(url,), daemon=True).start()

    def _fetch_worker(self, url: str) -> None:
        try:
            self.q.put(("info", (url, fetch_info(self.ytdlp, url, self.deno))))
        except DownloadError as e:
            if str(e) == "__PLAYLIST__":
                self.q.put(("playlist_detected", url))
            else:
                self.q.put(("error", str(e)))
        except Exception as e:
            self.q.put(("error", f"Couldn't read that link.\n\n{e}"))

    # ---- search by name --------------------------------------------------- #

    def _run_search(self, query: str) -> None:
        self._lock_search(True)
        self._status(f"Searching for “{query}”…")
        self._bar_indeterminate()
        threading.Thread(target=self._search_worker, args=(query,), daemon=True).start()

    def _search_worker(self, query: str) -> None:
        try:
            self.q.put(("results", (query, search(self.ytdlp, query, self.deno, n=6))))
        except DownloadError as e:
            self.q.put(("search_error", str(e)))
        except Exception as e:
            self.q.put(("search_error", f"Couldn't run that search.\n\n{e}"))

    def _lock_search(self, on: bool) -> None:
        self._searching = on
        if on:
            self._enable([self.url_entry, self.paste_btn, self.mode_seg], False)
            self._set_action(self.go_btn, False)
            self.several_btn.configure(state="disabled")
        else:
            self._bar_set(0)
            self._set_state("empty")  # restores step-1 controls

    def _open_chooser(self, query: str, results: list) -> None:
        if self._chooser is not None:
            return
        try:
            self._chooser = ChooserView(self, query, results,
                                        on_choose=self._chooser_pick, on_back=self._close_chooser)
            self._chooser.place(relx=0, rely=0, relwidth=1, relheight=1)
            self._chooser.tkraise()
        except Exception as e:
            self._close_chooser()
            messagebox.showerror(APP_TITLE, f"Couldn't show the results.\n\n{e}")

    def _close_chooser(self) -> None:
        if self._chooser is not None:
            self._chooser.destroy()
            self._chooser = None
        self._status("Ready. Paste a link, or search by name.")

    def _chooser_pick(self, url: str) -> None:
        self._close_chooser()
        self.mode_var.set(MODE_LINK)
        self._on_mode_change()
        self._load_url(url)

    # ---- bulk / playlist -------------------------------------------------- #

    def _on_several(self) -> None:
        if (not self.ready or self._searching
                or self.flow_state in ("starting", "loading", "downloading")
                or self._bulk is not None or self._chooser is not None):
            return
        self._open_bulk()

    def _open_bulk(self):
        from .bulk import BulkView  # lazy import avoids a circular import at startup
        try:
            self.minsize(770, 560)
            self.geometry("800x660")  # logical; list scrolls so height stays put
            self._bulk = BulkView(self)
            self._bulk.place(relx=0, rely=0, relwidth=1, relheight=1)
            self._bulk.tkraise()
            return self._bulk
        except Exception as e:
            self._close_bulk()
            messagebox.showerror(APP_TITLE, f"Couldn't open bulk mode.\n\n{e}")
            return None

    def _open_bulk_with_playlist(self, url: str) -> None:
        bulk = self._open_bulk()
        if bulk is not None:
            bulk.import_playlist(url)

    def _close_bulk(self) -> None:
        """Destroy the bulk overlay and bring the single-song screen back."""
        if self._bulk is not None:
            self._bulk.destroy()
            self._bulk = None
        self.minsize(770, 610)
        self.geometry("800x640")

    # ---- trim mirror + validation ---------------------------------------- #

    def _sync_set(self, var, value) -> None:
        """Set one trim var without retriggering the opposite mirror handler."""
        self._syncing = True
        try:
            var.set(value)
        finally:
            self._syncing = False

    def _skip_first(self) -> None:        # Skip first -> Start
        if self._syncing or self.flow_state not in ("loaded", "done"):
            return
        try:
            n = float(self.skipfirst_var.get() or 0)
        except ValueError:
            self._validate_trim(); return
        self._sync_set(self.start_var, fmt_time(max(0, n)))
        self._validate_trim()

    def _skip_last(self) -> None:         # Skip last -> End
        if self._syncing or self.flow_state not in ("loaded", "done") or not self.info:
            return
        try:
            n = float(self.skiplast_var.get() or 0)
        except ValueError:
            self._validate_trim(); return
        if self.info.duration > 0:
            self._sync_set(self.end_var, fmt_time(max(0, self.info.duration - n)))
        self._validate_trim()

    def _start_changed(self) -> None:     # Start -> Skip first
        if self._syncing or self.flow_state not in ("loaded", "done"):
            return
        try:
            s = parse_time(self.start_var.get())
        except ValueError:
            self._validate_trim(); return
        self._sync_set(self.skipfirst_var, str(int(round(max(0, s)))))
        self._validate_trim()

    def _end_changed(self) -> None:       # End -> Skip last
        if self._syncing or self.flow_state not in ("loaded", "done"):
            return
        text = self.end_var.get().strip()
        if text and self.info and self.info.duration > 0:
            try:
                e = parse_time(text)
                self._sync_set(self.skiplast_var, str(int(round(max(0, self.info.duration - e)))))
            except ValueError:
                pass
        self._validate_trim()

    def _validate_trim(self) -> None:
        """Check the trim makes sense; show a hint and gate Download accordingly."""
        if self.flow_state not in ("loaded", "done"):
            return
        dur = self.info.duration if self.info else 0
        msg = ""
        try:
            start = parse_time(self.start_var.get())
        except ValueError:
            msg = "Start time isn't valid — use mm:ss (e.g. 1:05)."
            start = None
        end = None
        if not msg:
            etext = self.end_var.get().strip()
            if etext:
                try:
                    end = parse_time(etext)
                except ValueError:
                    msg = "End time isn't valid — use mm:ss (e.g. 2:30)."
        if not msg and start is not None and start < 0:
            msg = "Start time can't be negative."
        if not msg and start is not None and dur > 0 and start >= dur:
            msg = f"Start is past the end of the song (length {fmt_time(dur)})."
        if not msg and start is not None and end is not None and end <= start:
            msg = "End time must be after the start time."
        self._trim_ok = not msg
        self.trim_hint_var.set(msg)
        self._refresh_download_btn()

    # ---- download --------------------------------------------------------- #

    def _on_new(self) -> None:
        player.stop()
        self._clear_cache()
        self.info = None
        self.loaded_url = None
        self._trim_ok = True
        self.trim_hint_var.set("")
        self._preview_img = None
        self.thumb_lbl.configure(image=None, text="♪")
        self.url_var.set("")
        self.mode_var.set(MODE_LINK)
        self._on_mode_change()
        self.title_var.set("")
        self.meta_var.set("Length: –")
        self.start_var.set("0:00")
        self.end_var.set("")
        self.skipfirst_var.set("0")
        self.skiplast_var.set("0")
        self._bar_set(0)
        self._set_state("empty")
        self._status("Ready. Paste a link, or search by name.")
        self.url_entry.focus_set()

    def _on_download(self) -> None:
        if self.flow_state not in ("loaded", "done") or not self.loaded_url:
            return
        self._validate_trim()
        if not self._trim_ok:
            messagebox.showwarning(APP_TITLE, self.trim_hint_var.get() or "Please fix the trim times.")
            return
        url = self.loaded_url
        start = max(0.0, parse_time(self.start_var.get()))
        end_text = self.end_var.get().strip()
        end = parse_time(end_text) if end_text else None
        # Clamp to the known length so a too-large end / start can't make an empty clip.
        dur = self.info.duration if self.info else 0
        if dur > 0:
            start = min(start, max(0.0, dur - 0.1))
            if end is not None:
                end = min(end, dur)
                if end <= start:
                    end = None

        display_title = self.title_var.get().strip() or "audio"
        dest = filedialog.asksaveasfilename(
            title="Save MP3 as…", defaultextension=".mp3",
            initialfile=f"{safe_filename(display_title)}.mp3",
            filetypes=[("MP3 audio", "*.mp3")])
        if not dest:
            return
        dest = str(dest)
        if not dest.lower().endswith(".mp3"):
            dest += ".mp3"
            # The Save dialog only confirmed overwrite for the name the user typed;
            # if adding ".mp3" now collides with an existing file, pick a free name
            # rather than silently clobbering it.
            p, i = Path(dest), 2
            while p.exists():
                p = p.with_name(f"{p.stem} ({i}){p.suffix}")
                i += 1
            dest = str(p)

        player.stop()  # silence any preview that's playing
        # Reuse the audio we already pulled for the preview, if it's the same video.
        cached = self._cached_for(url)
        dur = self.info.duration if self.info else 0
        self._set_state("downloading")
        self._status("Preparing…")
        self._bar_indeterminate()
        threading.Thread(
            target=self._download_worker,
            args=(url, Path(dest), display_title, start, end, dur, cached), daemon=True).start()

    def _cached_for(self, url: str):
        """Return (audio, thumb) already downloaded for ``url``, or None."""
        if (self._cache_url == url and self._cache_audio is not None
                and self._cache_audio.exists()):
            thumb = self._cache_thumb if (self._cache_thumb and self._cache_thumb.exists()) else None
            return (self._cache_audio, thumb)
        return None

    def _clear_cache(self) -> None:
        """Forget the cached preview audio and tidy its files (best-effort)."""
        self._cache_url = None
        self._cache_audio = None
        self._cache_thumb = None
        try:
            for f in self._preview_dir.glob("*"):
                f.unlink()
        except Exception:
            pass

    def _download_worker(self, url, dest: Path, title, start, end, dur, cached) -> None:
        try:
            clip_total = None
            base_end = end if end is not None else (dur or 0)
            if base_end and base_end > start:
                clip_total = max(0.1, base_end - start)
            # Unknown length -> indeterminate bar during convert (no fake percentage).
            convert_bar = ("busy_bar", None) if clip_total is None else ("progress", 0.0)
            if cached is not None:
                audio, thumb = cached
                self.q.put(("status", "Converting to MP3…"))
                self.q.put(convert_bar)
                make_mp3(audio, dest, title=title, start=start, end=end, cover=thumb,
                         total_seconds=clip_total,
                         on_progress=lambda f: self.q.put(("progress", f)))
            else:
                with tempfile.TemporaryDirectory(prefix="aubreymp3_") as tmp:
                    self.q.put(("status", "Downloading audio…"))
                    audio, thumb = download_audio(
                        self.ytdlp, url, Path(tmp), deno=self.deno,
                        on_progress=lambda p: self.q.put(("progress", p / 100.0)))
                    self.q.put(("status", "Converting to MP3…"))
                    self.q.put(convert_bar)  # restart the bar for the convert phase
                    make_mp3(audio, dest, title=title, start=start, end=end, cover=thumb,
                             total_seconds=clip_total,
                             on_progress=lambda f: self.q.put(("progress", f)))
            self.q.put(("done", dest))
        except DownloadError as e:
            self.q.put(("error", str(e)))
        except Exception as e:
            self.q.put(("error", f"Something went wrong.\n\n{e}"))

    # ---- trim audio preview (experimental) -------------------------------- #

    def _on_preview(self, which: str) -> None:
        if self.flow_state not in ("loaded", "done") or self._preview_busy or not self.loaded_url:
            return
        self._validate_trim()
        if not self._trim_ok:
            messagebox.showwarning(APP_TITLE, self.trim_hint_var.get() or "Please fix the trim times first.")
            return
        dur = self.info.duration if self.info else 0
        try:
            start = max(0.0, parse_time(self.start_var.get()))
            etext = self.end_var.get().strip()
            end = parse_time(etext) if etext else (dur if dur > 0 else 0)
        except ValueError:
            return
        seglen = 6.0
        if which == "start":
            at = start
            if end and end > start:
                seglen = min(6.0, max(1.0, end - start))
        else:  # end
            if not end or end <= 0:
                self._status("To hear the end, set an End time (or load a video with a known length).")
                return
            at = max(0.0, end - 6.0)
            seglen = min(6.0, end - at)

        url = self.loaded_url
        cached = self._cached_for(url)
        self._preview_busy = True
        player.stop()
        self._set_state(self.flow_state)  # re-gates: locks buttons while preparing
        if cached is None:
            self._status("Getting the audio to preview… (first time for this song)")
            self._bar_indeterminate()
        else:
            self._status("Preparing preview…")
        threading.Thread(target=self._preview_worker,
                         args=(which, url, at, seglen, cached), daemon=True).start()

    def _preview_worker(self, which, url, at, seglen, cached) -> None:
        try:
            if cached is not None:
                audio, _thumb = cached
            else:
                audio, thumb = download_audio(
                    self.ytdlp, url, self._preview_dir, deno=self.deno,
                    on_progress=lambda p: self.q.put(("progress", p / 100.0)))
                self.q.put(("preview_cached", (url, str(audio), str(thumb) if thumb else "")))
            snip = self._preview_dir / f"snip_{which}.wav"
            self.q.put(("busy_bar", None))
            self.q.put(("status", "Preparing preview…"))
            ok = extract_preview(Path(audio), snip, at, seglen)
            played = player.play(snip) if ok else False
            self.q.put(("preview_result", (which, bool(played), fmt_time(at))))
        except DownloadError as e:
            self.q.put(("preview_error", str(e)))
        except Exception as e:
            self.q.put(("preview_error", str(e)))
        finally:
            self.q.put(("preview_done", None))

    # ---- thumbnail preview ------------------------------------------------ #

    def _fetch_preview(self, url: str, thumb_url: str) -> None:
        img = fetch_image(thumb_url, box=160)
        if img is not None:
            self.q.put(("thumb", (url, img)))

    def _apply_thumb(self, url: str, pil_img) -> None:
        if url != self.loaded_url:
            return  # the user moved on before the image arrived
        try:
            self._preview_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(76, 76))
            self.thumb_lbl.configure(image=self._preview_img, text="")
        except Exception:
            pass

    # ---- queue pump (main thread) ---------------------------------------- #

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "status":
                    self._status(str(payload))
                elif kind == "progress":
                    self._bar_set(float(payload))  # type: ignore[arg-type]
                elif kind == "busy_bar":
                    self._bar_indeterminate()
                elif kind == "info":
                    url, info = payload  # type: ignore[misc]
                    self._on_info(url, info)
                elif kind == "results":
                    query, results = payload  # type: ignore[misc]
                    self._on_results(query, results)
                elif kind == "thumb":
                    url, img = payload  # type: ignore[misc]
                    self._apply_thumb(url, img)
                elif kind == "preview_cached":
                    url, audio, thumb = payload  # type: ignore[misc]
                    if url == self.loaded_url:  # ignore a result for a video we left
                        self._cache_url = url
                        self._cache_audio = Path(audio)
                        self._cache_thumb = Path(thumb) if thumb else None
                elif kind == "preview_result":
                    which, played, at = payload  # type: ignore[misc]
                    if played:
                        self._status(f"♪ Playing the {which} (from {at}). Adjust the time and listen again.")
                    else:
                        self._status("Couldn't play a preview on this PC — your trim will still save fine.")
                elif kind == "preview_error":
                    self._status("Couldn't prepare the preview. You can still save the MP3.")
                elif kind == "preview_done":
                    self._preview_busy = False
                    self._bar_set(0)
                    if self.flow_state in ("loaded", "done"):
                        self._set_state(self.flow_state)  # re-enable buttons
                elif kind == "playlist_detected":
                    self._on_playlist_detected(str(payload))
                elif kind == "ytdlp":
                    self.ytdlp = str(payload)
                elif kind == "deno":
                    self.deno = payload  # type: ignore[assignment]
                elif kind == "ready":
                    self.ready = True
                    if self.flow_state == "starting":
                        self._bar_set(0)
                        self._set_state("empty")
                elif kind == "done":
                    self._on_done(Path(str(payload)))
                elif kind == "setup_error":
                    self._on_setup_error(str(payload))
                elif kind == "update_available":
                    self._on_update_available(payload)  # type: ignore[arg-type]
                elif kind == "quit_for_update":
                    self.destroy()
                    return
                elif kind == "search_error":
                    self._lock_search(False)
                    self._status("")
                    messagebox.showerror(APP_TITLE, str(payload))
                elif kind == "error":
                    self._on_error(str(payload))
        except queue.Empty:
            pass
        except Exception:
            # A bug in one handler must NEVER kill the pump (that would freeze the
            # whole app). Swallow, tell the user, and keep draining next tick.
            try:
                self._status("Something went wrong updating the screen. Please try again.")
            except Exception:
                pass
        finally:
            if not getattr(self, "_closing", False):
                try:
                    self.after(100, self._poll)
                except Exception:
                    pass

    # ---- state updates ---------------------------------------------------- #

    def _on_info(self, url: str, info: VideoInfo) -> None:
        player.stop()
        self._clear_cache()  # a different video — drop any previously cached audio
        self.info = info
        self.loaded_url = url
        self.title_var.set(info.title)
        meta = f"Length: {fmt_time(info.duration)}" if info.duration > 0 else "Length: unknown"
        if info.source:
            src = "YouTube" if info.source.startswith("youtube") else info.source.title()
            meta += f"   •   from {src}"
            if not info.source.startswith("youtube"):
                meta += " (experimental)"
        self.meta_var.set(meta)
        self.start_var.set("0:00")
        self.end_var.set(fmt_time(info.duration) if info.duration > 0 else "")
        self.skipfirst_var.set("0")
        self.skiplast_var.set("0")
        self._preview_img = None
        self.thumb_lbl.configure(image=None, text="♪")
        self._bar_set(0)
        self._trim_ok = True
        self.trim_hint_var.set("")
        self._set_state("loaded")
        self._validate_trim()
        self._status(f"✓ Loaded: {info.title}")
        if info.thumbnail:
            threading.Thread(target=self._fetch_preview, args=(url, info.thumbnail), daemon=True).start()

    def _on_results(self, query: str, results: list) -> None:
        self._lock_search(False)
        if not results:
            self._status(f"No songs found for “{query}”. Try different words.")
            return
        self._status(f"Found {len(results)} result{'s' if len(results) != 1 else ''}. Pick one.")
        self._open_chooser(query, results)

    def _on_playlist_detected(self, url: str) -> None:
        self._bar_set(0)
        self._set_state("empty")
        self._status("That link is a playlist.")
        if messagebox.askyesno(
            APP_TITLE,
            "That link is a playlist of songs.\n\n"
            "Open “Download several” to grab them all?"):
            self._open_bulk_with_playlist(url)

    def _on_done(self, path: Path) -> None:
        self._bar_set(1.0)
        self._set_state("done")
        self._status(f"✓ Done! Saved {path.name}. Click “Start over” for another.")
        if messagebox.askyesno(APP_TITLE, f"Saved:\n{path.name}\n\nOpen the folder?"):
            open_folder(path.parent)

    def _on_error(self, message: str) -> None:
        self._bar_set(0)
        # return to the step the user can act on: re-enter the link, or retry download
        self._set_state("empty" if self.flow_state in ("loading", "starting") else "loaded")
        if self.flow_state in ("loaded", "done"):
            self._validate_trim()
        self._status("")
        messagebox.showerror(APP_TITLE, message)

    def _on_setup_error(self, detail: str) -> None:
        # Setup failed (no internet / download blocked / ffmpeg missing). Lock the
        # whole app so nothing can be attempted, and offer to retry.
        self._bar_set(0)
        self.ready = False
        self._set_state("failed")
        self._status("Setup failed. Connect to the internet, then click Retry.")
        msg = ("The app couldn't get the tools it needs to run (the downloader "
               "and helper), or a required file is missing.\n\n"
               f"Details: {detail}\n\n"
               "Make sure you're connected to the internet, then click Retry.")
        if messagebox.askretrycancel(APP_TITLE, msg):
            self._start_engine()

    def _on_update_available(self, info: dict) -> None:
        if (self.flow_state in ("loading", "downloading", "starting") or self._searching
                or self._preview_busy or self._chooser is not None or self._bulk is not None):
            return  # don't interrupt an in-progress job or yank away an open overlay
        if not messagebox.askyesno(
            APP_TITLE,
            f"A newer version ({info.get('tag')}) is available.\n"
            f"You have v{__version__}.\n\n"
            "Update now? The app will download it and restart."):
            return
        self._set_state("starting")
        self._status("Downloading update…")
        self._bar_indeterminate()
        threading.Thread(target=self._do_update, args=(info,), daemon=True).start()

    def _do_update(self, info: dict) -> None:
        try:
            download_and_relaunch(info["url"], status=lambda m: self.q.put(("status", m)))
            self.q.put(("status", "Update downloaded. Restarting…"))
            self.q.put(("quit_for_update", None))
        except Exception as e:
            self.q.put(("error", f"Couldn't install the update.\n\n{e}\n\n"
                                 "You can download the latest version from mpowell.tech/tools."))

    def _status(self, text: str) -> None:
        self.status_var.set(text)

    def destroy(self) -> None:
        # Stop any preview and clean up the temp audio before the window closes.
        # Idempotent: _closing also stops _poll from rescheduling onto a dead window.
        if getattr(self, "_closing", False):
            return
        self._closing = True
        try:
            player.stop()
        except Exception:
            pass
        try:
            import shutil
            shutil.rmtree(self._preview_dir, ignore_errors=True)
        except Exception:
            pass
        super().destroy()


# --------------------------------------------------------------------------- #
# Search-results chooser (full-window overlay, same pattern as bulk)
# --------------------------------------------------------------------------- #

class ChooserView(ctk.CTkFrame):
    """Pick one result from a name search. Static (no worker), so no queue."""

    def __init__(self, app, query: str, results: list, on_choose, on_back) -> None:
        super().__init__(app, fg_color=WINDOW_BG, corner_radius=0)
        self.on_choose = on_choose
        self.on_back = on_back
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 4))
        header.grid_columnconfigure(1, weight=1)
        ctk.CTkButton(header, text="←  Back", width=90, fg_color=SECONDARY,
                      hover_color=SECONDARY_H, text_color=TITLE_ON, command=on_back
                      ).grid(row=0, column=0, sticky="w")
        tl = ctk.CTkFrame(header, fg_color="transparent")
        tl.grid(row=0, column=1, sticky="ew")
        ctk.CTkLabel(tl, text="Choose your song", font=ctk.CTkFont(size=20, weight="bold")).pack()
        ctk.CTkLabel(tl, text=f"Results for “{query}”", text_color=MUTED,
                     font=ctk.CTkFont(size=12)).pack()
        ctk.CTkLabel(header, text="", width=90).grid(row=0, column=2)  # balance the back button

        lst = ctk.CTkScrollableFrame(self, fg_color=("gray94", "gray13"), label_text="")
        lst.grid(row=1, column=0, sticky="nsew", padx=18, pady=(4, 8))
        lst.grid_columnconfigure(0, weight=1)
        for i, r in enumerate(results):
            self._result_row(lst, i, r)

        ctk.CTkLabel(self, text="Not what you wanted? Click Back and try different words.",
                     text_color=MUTED, font=ctk.CTkFont(size=12)
                     ).grid(row=2, column=0, sticky="w", padx=20, pady=(0, 14))

    def _result_row(self, parent, i: int, r) -> None:
        row = ctk.CTkFrame(parent, corner_radius=8, fg_color=("white", "gray17"))
        row.grid(row=i, column=0, sticky="ew", padx=4, pady=4)
        row.grid_columnconfigure(1, weight=1)
        dur = fmt_time(r.duration) if r.duration and r.duration > 0 else "?"
        ctk.CTkLabel(row, text=dur, width=58, text_color=MUTED,
                     font=ctk.CTkFont(size=12, weight="bold")).grid(row=0, column=0, rowspan=2,
                                                                    padx=(12, 6), pady=10)
        ctk.CTkLabel(row, text=r.title, anchor="w", justify="left",
                     font=ctk.CTkFont(size=13, weight="bold"), wraplength=440).grid(
            row=0, column=1, sticky="ew", padx=4, pady=(10, 0))
        ctk.CTkLabel(row, text=r.uploader or "", anchor="w", text_color=MUTED,
                     font=ctk.CTkFont(size=11)).grid(row=1, column=1, sticky="ew", padx=4, pady=(0, 10))
        ctk.CTkButton(row, text="Use this  ▸", width=104, height=34,
                      fg_color=PINK, hover_color=PINK_HOVER,
                      command=lambda u=r.url: self.on_choose(u)).grid(
            row=0, column=2, rowspan=2, padx=(6, 12), pady=10)


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
