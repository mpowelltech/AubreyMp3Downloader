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
import webbrowser
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from . import __version__
from .audio import make_mp3, waveform
from .downloader import (DownloadError, VideoInfo, download_audio, fetch_info,
                         has_playlist, has_single_video, looks_like_url, search)
from .media import fetch_image
from .paths import ffmpeg_binary, resource_path
from .paths import cache_dir
from .player import Player
from .timeline import TrimTimeline
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


def symfont(size: int, weight: str = "normal", underline: bool = False):
    """A font that can render symbol glyphs (▶ ⏸ ✓ ♪ ▾ …).

    CustomTkinter's default font is Roboto, which has NONE of these glyphs, so on
    Windows they render as tofu boxes. 'Segoe UI Symbol' (always present on Win)
    has them all. On macOS/Linux the default font already renders them, so we keep
    the default there. Must be called after the Tk root exists.
    """
    fam = "Segoe UI Symbol" if sys.platform == "win32" else None
    return ctk.CTkFont(family=fam, size=size, weight=weight, underline=underline)


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
        self.geometry("800x670")
        self.minsize(780, 600)
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
        self._choice = None      # a custom choice dialog overlay, when open
        self._preview_img = None # keep a ref so the CTkImage isn't garbage-collected
        self._sec: dict[int, dict] = {}   # accordion sections
        self._expanded = 1                # which section body is open
        # --- trim audio preview (experimental, streaming player) ---
        # We download the full audio ONCE in the background as soon as a song loads,
        # cache it, and reuse it for the player AND the final export.
        self._player = Player()
        self._preview_dir = Path(tempfile.mkdtemp(prefix="aubreyprev_"))
        self._cache_url: str | None = None   # which video the cached audio belongs to
        self._cache_audio: Path | None = None
        self._cache_thumb: Path | None = None
        self._prefetch_url: str | None = None   # url whose audio is downloading now
        self._pending_play = None               # seconds to play once audio is ready
        self._seek_pos = 0.0                    # where Play will start (set by waveform clicks)
        self._play_end = None                   # stop playback here (end of the trimmed section)
        self._audio_seq = 0                     # unique per-song audio subdirs
        self._play_anim = None                  # after-id of the playhead poll loop
        self._last_dir: str | None = None        # remembered save folder (also avoids a
        #   network-namespace folder-picker hang on Parallels: open at a local folder)
        self._badges: dict[int, ctk.CTkLabel] = {}
        self._titles: dict[int, ctk.CTkLabel] = {}
        self.q: "queue.Queue[tuple[str, object]]" = queue.Queue()

        self._build_ui()
        self.after(0, self._close_splash)   # dismiss the PyInstaller splash now the window is up
        self.after(100, self._poll)
        self._start_engine()

    # ---- UI construction ------------------------------------------------- #

    def _acc_section(self, number: int, title: str) -> tuple:
        """A collapsible accordion section: clickable header + a hideable body."""
        card = ctk.CTkFrame(self, corner_radius=12, fg_color=CARD_BG,
                            border_width=1, border_color=CARD_BORDER)
        card.grid_columnconfigure(0, weight=1)
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=14, pady=10)
        head.grid_columnconfigure(1, weight=1)
        badge = ctk.CTkLabel(head, text=str(number), width=26, height=26, corner_radius=13,
                             font=symfont(13, "bold"))
        badge.grid(row=0, column=0, padx=(0, 10))
        title_lbl = ctk.CTkLabel(head, text=title, anchor="w", font=ctk.CTkFont(size=15, weight="bold"))
        title_lbl.grid(row=0, column=1, sticky="w")
        chevron = ctk.CTkLabel(head, text="", width=18, text_color=MUTED, font=symfont(14))
        chevron.grid(row=0, column=2, padx=(8, 0))
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.grid_columnconfigure(0, weight=1)
        self._badges[number] = badge
        self._titles[number] = title_lbl
        self._sec[number] = {"card": card, "body": body, "chevron": chevron, "avail": False}
        for w in (head, badge, title_lbl, chevron):
            w.bind("<Button-1>", lambda _e, n=number: self._acc_click(n))
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
                light_image=Image.open(resource_path("assets/icon_header.png")), size=(58, 58))
            ctk.CTkLabel(header, text="", image=self._logo).grid(row=0, column=0, padx=(0, 12))
            tcol, ncol = 1, 2
        except Exception:
            pass
        header.grid_columnconfigure(tcol, weight=1)
        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.grid(row=0, column=tcol, sticky="w")
        ctk.CTkLabel(titles, text=APP_TITLE, font=ctk.CTkFont(size=20, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(
            titles, text="Turn a song from the web into an MP3 for the Yoto player.",
            text_color=MUTED, font=ctk.CTkFont(size=12)).pack(anchor="w")
        ctk.CTkLabel(
            titles, text="Built by Matt for my favourite niece ♥",
            text_color=PINK, font=symfont(11, "bold")).pack(anchor="w")
        right = ctk.CTkFrame(header, fg_color="transparent")
        right.grid(row=0, column=ncol, sticky="e", padx=(8, 0))
        self.several_btn = ctk.CTkButton(
            right, text="☰  Download several", width=160, height=30, fg_color=SECONDARY,
            hover_color=SECONDARY_H, text_color=TITLE_ON, font=symfont(13), command=self._on_several)
        self.several_btn.pack(fill="x")
        self.new_btn = ctk.CTkButton(
            right, text="↺  Start over", width=160, height=30, fg_color=SECONDARY,
            hover_color=SECONDARY_H, text_color=TITLE_ON, font=symfont(13), command=self._on_new)
        self.new_btn.pack(fill="x", pady=(5, 0))

        # ===== Section 1: find the song =====
        c1, b1 = self._acc_section(1, "Find your song")
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
        self.hint1_var = tk.StringVar(value="Paste a link, then click “Get info” to load the song.")
        ctk.CTkLabel(b1, textvariable=self.hint1_var, text_color=MUTED, font=ctk.CTkFont(size=12),
                     wraplength=720, justify="left").grid(row=2, column=0, sticky="w", pady=(7, 0))
        ctk.CTkLabel(
            b1, text="Works with YouTube, plus Vimeo, SoundCloud and many more "
                     "(non-YouTube sites are experimental).",
            text_color=MUTED, font=ctk.CTkFont(size=11, slant="italic"),
            wraplength=720, justify="left").grid(row=3, column=0, sticky="w", pady=(2, 0))

        # ===== Section 2: check the song (title on one full-width line) =====
        c2, b2 = self._acc_section(2, "Check the song")
        c2.grid(row=2, column=0, sticky="ew", padx=PADX, pady=4)
        b2.grid_columnconfigure(0, weight=1)
        # pack (not grid weights) guarantees the title fills ALL space right of the
        # picture — grid column-width quirks were leaving the title boxed in.
        toprow = ctk.CTkFrame(b2, fg_color="transparent")
        toprow.grid(row=0, column=0, sticky="ew")
        self.thumb_lbl = ctk.CTkLabel(
            toprow, text="♪", width=72, height=72, corner_radius=10,
            fg_color=THUMB_BG, text_color=MUTED, font=symfont(30))
        self.thumb_lbl.pack(side="left", padx=(0, 12))
        self.thumb_lbl.bind("<Button-1>", lambda _e: self._open_source())
        info_col = ctk.CTkFrame(toprow, fg_color="transparent")
        info_col.pack(side="left", fill="both", expand=True)
        self.title_var = tk.StringVar()
        self.title_entry = ctk.CTkEntry(info_col, textvariable=self.title_var, height=38,
                                        font=ctk.CTkFont(size=14),
                                        placeholder_text="(the song title loads here)")
        self.title_entry.pack(fill="x")
        self.meta_var = tk.StringVar(value="Length: ...")
        ctk.CTkLabel(info_col, textvariable=self.meta_var, text_color=MUTED, anchor="w",
                     font=ctk.CTkFont(size=12)).pack(fill="x", pady=(4, 0))
        self.src_link = ctk.CTkLabel(info_col, text="↗  Open the original video",
                                     text_color=PINK, anchor="w", cursor="hand2",
                                     font=symfont(12, underline=True))
        self.src_link.pack(anchor="w", pady=(4, 0))
        self.src_link.bind("<Button-1>", lambda _e: self._open_source())
        ctk.CTkLabel(
            b2, text="This is the track name shown in the Yoto app. Edit it if you like, "
                     "then trim it below.",
            text_color=MUTED, font=ctk.CTkFont(size=12), wraplength=720, justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(10, 0))
        ctk.CTkButton(b2, text="Next: Trim  ▾", width=130, height=30, fg_color=SECONDARY,
                      hover_color=SECONDARY_H, text_color=TITLE_ON, font=symfont(13),
                      command=lambda: self._acc_click(3)).grid(
            row=2, column=0, sticky="w", pady=(10, 0))

        # ===== Section 3: trim + mini player =====
        c3, b3 = self._acc_section(3, "Trim & preview")
        c3.grid(row=3, column=0, sticky="ew", padx=PADX, pady=4)
        b3.grid_columnconfigure(0, weight=1)

        self.timeline = TrimTimeline(b3, on_change=self._on_trim_drag,
                                     on_seek=self._on_seek, height=92)
        self.timeline.grid(row=0, column=0, sticky="ew")

        # transport: Download-to-preview (until audio ready) <-> Play / Stop + position
        trans = ctk.CTkFrame(b3, fg_color="transparent")
        trans.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.dl_preview_btn = ctk.CTkButton(
            trans, text="⬇  Download to preview", height=32, fg_color=PINK, hover_color=PINK_HOVER,
            font=symfont(13, "bold"), command=self._on_download_preview)
        self.play_btn = ctk.CTkButton(
            trans, text="▶  Play", width=120, height=32, fg_color=PINK, hover_color=PINK_HOVER,
            font=symfont(13, "bold"), command=self._toggle_play)
        self.stop_btn = ctk.CTkButton(
            trans, text="⏮  Back to start", width=140, height=32, fg_color=SECONDARY,
            hover_color=SECONDARY_H, text_color=TITLE_ON, font=symfont(13), command=self._player_back)
        self.pos_var = tk.StringVar(value="")
        self.pos_lbl = ctk.CTkLabel(trans, textvariable=self.pos_var, text_color=MUTED,
                                    font=ctk.CTkFont(size=12))
        self.preview_note_var = tk.StringVar(value="")
        self.preview_note = ctk.CTkLabel(trans, textvariable=self.preview_note_var, text_color=MUTED,
                                         font=ctk.CTkFont(size=12), anchor="w")

        times = ctk.CTkFrame(b3, fg_color="transparent")
        times.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ctk.CTkLabel(times, text="Start").grid(row=0, column=0, padx=(0, 4))
        self.start_var = tk.StringVar(value="0:00")
        self.start_entry = ctk.CTkEntry(times, textvariable=self.start_var, width=70)
        self.start_entry.grid(row=0, column=1)
        ctk.CTkLabel(times, text="End").grid(row=0, column=2, padx=(12, 4))
        self.end_var = tk.StringVar()
        self.end_entry = ctk.CTkEntry(times, textvariable=self.end_var, width=70, placeholder_text="end")
        self.end_entry.grid(row=0, column=3)
        ctk.CTkLabel(times, text="(mm:ss)", text_color=MUTED).grid(row=0, column=4, padx=(6, 0))
        self.start_var.trace_add("write", lambda *_: self._start_changed())
        self.end_var.trace_add("write", lambda *_: self._end_changed())
        ctk.CTkLabel(
            b3, text="Drag the pink handles (or type Start/End) to pick the part to keep. "
                     "Press Play to listen, and click anywhere on the bar to jump there.",
            text_color=MUTED, font=ctk.CTkFont(size=12), wraplength=720, justify="left",
        ).grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.trim_hint_var = tk.StringVar(value="")
        ctk.CTkLabel(b3, textvariable=self.trim_hint_var, text_color=ERR_TX,
                     font=ctk.CTkFont(size=12, weight="bold"), wraplength=720,
                     justify="left").grid(row=4, column=0, sticky="w", pady=(2, 0))
        self.src_link2 = ctk.CTkLabel(b3, text="↗  Open the original video",
                                      text_color=PINK, anchor="w", cursor="hand2",
                                      font=symfont(12, underline=True))
        self.src_link2.grid(row=5, column=0, sticky="w", pady=(8, 0))
        self.src_link2.bind("<Button-1>", lambda _e: self._open_source())

        # --- Download (primary action) ---
        self.download_btn = ctk.CTkButton(
            self, text="⬇  Download MP3", height=46,
            font=symfont(15, "bold"),
            fg_color=PINK, hover_color=PINK_HOVER, command=self._on_download)
        self.download_btn.grid(row=4, column=0, sticky="ew", padx=PADX, pady=(10, 4))

        # --- Footer: progress + status ---
        self.progress = ctk.CTkProgressBar(self, progress_color=PINK)
        self.progress.grid(row=5, column=0, sticky="ew", padx=PADX, pady=(4, 2))
        self.progress.set(0)
        self.status_var = tk.StringVar(value="Starting up…")
        ctk.CTkLabel(
            self, textvariable=self.status_var, anchor="w", justify="left", font=symfont(12),
            wraplength=780, text_color=MUTED).grid(row=6, column=0, sticky="ew", padx=PADX, pady=(0, 12))

        self._step3_widgets = [self.start_entry, self.end_entry]
        self._expand(1)

    # ---- accordion ------------------------------------------------------- #

    def _expand(self, n: int) -> None:
        self._expanded = n
        for i, sec in self._sec.items():
            if i == n:
                sec["body"].grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 14))
                sec["chevron"].configure(text="▾" if sec["avail"] or i == 1 else "")
            else:
                sec["body"].grid_remove()
                sec["chevron"].configure(text="▸" if sec["avail"] else "")

    def _acc_click(self, n: int) -> None:
        if self._sec.get(n, {}).get("avail"):
            self._expand(n)

    def _set_section_available(self, n: int, avail: bool) -> None:
        sec = self._sec.get(n)
        if not sec:
            return
        sec["avail"] = avail
        if n != self._expanded:
            sec["chevron"].configure(text="▸" if avail else "")

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
        try:
            self.timeline.set_locked(not loaded)
        except Exception:
            pass
        # Accordion: section 1 reachable only while empty; 2 & 3 once a song is loaded.
        self._set_section_available(1, state == "empty")
        self._set_section_available(2, loaded)
        self._set_section_available(3, loaded)
        self._refresh_transport()
        self._refresh_download_btn()
        self.new_btn.configure(state="normal" if loaded else "disabled")
        self.several_btn.configure(
            state="normal" if (self.ready and not self._searching
                               and state in ("empty", "loaded", "done")) else "disabled")

        for n, kind in _BADGES[state].items():
            self._set_badge(n, kind)
        self._refresh_go_btn()

    def _refresh_download_btn(self) -> None:
        ok = self.flow_state in ("loaded", "done") and self._trim_ok
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
        if self.flow_state != "empty" or not self.ready or self._searching or self._choice is not None:
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

    def _ask_choice(self, message: str, buttons, on_choice) -> None:
        """Show a custom in-window dialog with relabelled buttons (no Yes/No/Cancel).

        ``buttons`` is a list of (label, value); ``on_choice(value)`` runs after the
        dialog closes. Uses an overlay frame, not a Toplevel (flaky on macOS).
        """
        if self._choice is not None:
            return

        def done(value):
            if self._choice is not None:
                self._choice.destroy()
                self._choice = None
            on_choice(value)

        try:
            self._choice = ChoiceOverlay(self, message, buttons, done)
            self._choice.place(relx=0, rely=0, relwidth=1, relheight=1)
            self._choice.tkraise()
        except Exception:
            self._choice = None

    def _handle_playlist_link(self, url: str) -> None:
        """A link that carries a playlist: offer the whole list vs. one song."""
        def route(value):
            if value == "playlist":
                self._open_bulk_with_playlist(url)
            elif value == "single":
                self._load_url(url)

        if has_single_video(url):
            self._ask_choice(
                "This link includes a whole playlist.\n\nWhat would you like to do?",
                [("Import whole playlist", "playlist"),
                 ("Just this one song", "single"),
                 ("Cancel", "cancel")],
                route)
        else:
            self._ask_choice(
                "This looks like a playlist of songs.\n\nImport every song into "
                "“Download several”?",
                [("Import whole playlist", "playlist"), ("Cancel", "cancel")],
                route)

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
                or self._bulk is not None or self._chooser is not None or self._choice is not None):
            return
        self._open_bulk()

    def _open_bulk(self):
        from .bulk import BulkView  # lazy import avoids a circular import at startup
        try:
            self.minsize(770, 560)
            self.geometry("800x680")  # logical; list scrolls so height stays put
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
        self.minsize(780, 600)
        self.geometry("800x670")

    # ---- trim: timeline <-> Start/End text + validation ------------------- #

    def _sync_set(self, var, value) -> None:
        """Set a trim var without retriggering its own trace handler."""
        self._syncing = True
        try:
            var.set(value)
        finally:
            self._syncing = False

    def _on_trim_drag(self, start: float, end: float) -> None:
        """The user dragged a timeline handle: push the values into the text boxes."""
        if self.flow_state not in ("loaded", "done"):
            return
        self._sync_set(self.start_var, fmt_time(start))
        self._sync_set(self.end_var, fmt_time(end))
        self._validate_trim()

    def _start_changed(self) -> None:
        if self._syncing or self.flow_state not in ("loaded", "done"):
            return
        self._push_text_to_timeline()
        self._validate_trim()

    def _end_changed(self) -> None:
        if self._syncing or self.flow_state not in ("loaded", "done"):
            return
        self._push_text_to_timeline()
        self._validate_trim()

    def _push_text_to_timeline(self) -> None:
        """Reflect the Start/End text on the timeline (no-op if the text is invalid)."""
        if not self.info or self.info.duration <= 0:
            return
        try:
            s = max(0.0, parse_time(self.start_var.get()))
        except ValueError:
            return
        etext = self.end_var.get().strip()
        try:
            e = parse_time(etext) if etext else self.info.duration
        except ValueError:
            return
        self.timeline.set_trim(s, e)

    def _validate_trim(self) -> None:
        """Check the trim makes sense; show a hint and gate Download accordingly."""
        if self.flow_state not in ("loaded", "done"):
            return
        dur = self.info.duration if self.info else 0
        msg = ""
        try:
            start = parse_time(self.start_var.get())
        except ValueError:
            msg = "Start time isn't valid. Use mm:ss, like 1:05."
            start = None
        end = None
        if not msg:
            etext = self.end_var.get().strip()
            if etext:
                try:
                    end = parse_time(etext)
                except ValueError:
                    msg = "End time isn't valid. Use mm:ss, like 2:30."
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
        self._player.stop()
        self._cancel_playhead()
        self._prefetch_url = None
        self._pending_play = None
        self._seek_pos = 0.0
        self._clear_cache()
        self.info = None
        self.loaded_url = None
        self._trim_ok = True
        self.trim_hint_var.set("")
        self.pos_var.set("")
        self._reset_thumb("")
        self.url_var.set("")
        self.title_var.set("")
        self.meta_var.set("Length: ...")
        self.start_var.set("0:00")
        self.end_var.set("")
        self.timeline.set_duration(0)
        self._bar_set(0)
        self._set_state("empty")        # set state BEFORE mode reset so it isn't skipped
        self.mode_var.set(MODE_LINK)
        self._on_mode_change()           # now flow_state is "empty" -> placeholders reset properly
        self._expand(1)
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
            initialdir=self.default_dir(),
            initialfile=f"{safe_filename(display_title)}.mp3",
            filetypes=[("MP3 audio", "*.mp3")])
        if not dest:
            return
        dest = str(dest)
        import os
        self._last_dir = os.path.dirname(dest) or self._last_dir
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

        self._player.stop()  # silence any preview that's playing
        self._cancel_playhead()
        # Reuse the audio already pulled for this video (prefetch/preview), if any.
        cached = self._cached_for(url)
        dur = self.info.duration if self.info else 0
        self._set_state("downloading")
        self._status("Preparing..." if cached else "Downloading audio...")
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
        """Forget the cached preview audio and tidy its files/subdirs (best-effort)."""
        self._cache_url = None
        self._cache_audio = None
        self._cache_thumb = None
        import shutil
        try:
            for p in self._preview_dir.iterdir():
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    try:
                        p.unlink()
                    except OSError:
                        pass
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

    # ---- background audio prefetch (so preview + save are instant) -------- #

    def _ensure_audio(self, url: str) -> None:
        """Start downloading the full audio for ``url`` in the background (once)."""
        if self._cached_for(url) is not None or self._prefetch_url == url:
            return
        self._prefetch_url = url
        self._audio_seq += 1
        # A fresh subdir per song so a previous (still-finishing) prefetch can't
        # collide on the "audio.<ext>" output name.
        sub = self._preview_dir / f"v{self._audio_seq}"
        try:
            sub.mkdir(parents=True, exist_ok=True)
        except Exception:
            sub = self._preview_dir
        threading.Thread(target=self._prefetch_worker, args=(url, sub), daemon=True).start()

    def _prefetch_worker(self, url: str, workdir: Path) -> None:
        try:
            audio, thumb = download_audio(self.ytdlp, url, workdir, deno=self.deno)
            self.q.put(("audio_ready", (url, str(audio), str(thumb) if thumb else "")))
        except Exception as e:
            self.q.put(("audio_error", (url, str(e))))

    def _on_audio_ready(self, url: str, audio: str, thumb: str) -> None:
        if url != self.loaded_url:
            return  # the user moved on to a different song
        self._prefetch_url = None
        self._cache_url = url
        self._cache_audio = Path(audio)
        self._cache_thumb = Path(thumb) if thumb else None
        try:
            self._player.load(self._cache_audio, self.info.duration if self.info else 0)
        except Exception:
            pass
        threading.Thread(target=self._waveform_worker, args=(url, audio), daemon=True).start()
        self._refresh_transport()
        pending, self._pending_play = self._pending_play, None
        if pending is not None and self.flow_state in ("loaded", "done"):
            self._play_from(pending)
        elif self.flow_state in ("loaded", "done"):
            self._status("Ready. Press Play to listen, or just Download MP3.")

    def _waveform_worker(self, url: str, audio: str) -> None:
        peaks = waveform(Path(audio), buckets=480)
        if peaks:
            self.q.put(("waveform", (url, peaks)))

    # ---- mini player (experimental, streaming) ---------------------------- #

    def _refresh_transport(self) -> None:
        """Show the right transport: Download-to-preview, Play/Stop, or a note."""
        for w in (self.dl_preview_btn, self.play_btn, self.stop_btn, self.pos_lbl, self.preview_note):
            w.grid_remove()
        if self.flow_state not in ("loaded", "done") or not self.loaded_url:
            return
        if not self._player.is_available():
            self.preview_note_var.set(
                "Audio preview isn't available on this PC (you can still trim and save).")
            self.preview_note.grid(row=0, column=0, sticky="w")
            return
        if self._cached_for(self.loaded_url) is not None:
            self.play_btn.grid(row=0, column=0, padx=(0, 6))
            self.stop_btn.grid(row=0, column=1, padx=(0, 12))
            self.pos_lbl.grid(row=0, column=2, sticky="w")
            self._refresh_play_btn()
        elif self._prefetch_url == self.loaded_url:
            self.preview_note_var.set("Getting the song ready to preview...")
            self.preview_note.grid(row=0, column=0, sticky="w")
        else:
            self.dl_preview_btn.grid(row=0, column=0, sticky="w")

    def _fmt_pos(self, pos: float) -> str:
        dur = self.info.duration if self.info else 0
        return f"{fmt_time(pos)} / {fmt_time(dur)}" if dur > 0 else fmt_time(pos)

    def _on_download_preview(self) -> None:
        if self.flow_state not in ("loaded", "done") or not self.loaded_url:
            return
        self._ensure_audio(self.loaded_url)
        self._status("Getting the song ready to preview (downloads once, then it's instant).")
        self._refresh_transport()

    def _trim_bounds(self) -> tuple:
        """Return (start, end) of the kept section in seconds, clamped to the song."""
        dur = self.info.duration if self.info else 0.0
        try:
            s = max(0.0, parse_time(self.start_var.get()))
        except ValueError:
            s = 0.0
        etext = self.end_var.get().strip()
        try:
            e = parse_time(etext) if etext else dur
        except ValueError:
            e = dur
        if dur > 0:
            s = min(s, dur)
            e = min(e, dur) if e else dur
        if e <= s:
            e = dur if dur > s else s + 1.0
        return s, e

    def _refresh_play_btn(self) -> None:
        self.play_btn.configure(text="⏸  Pause" if self._player.is_playing() else "▶  Play")

    def _toggle_play(self) -> None:
        if self.flow_state not in ("loaded", "done") or not self.loaded_url:
            return
        if self._player.is_playing():
            self._player.pause()
            self._cancel_playhead()
            self._refresh_play_btn()
            self._status("Paused. Press Play to continue.")
            return
        if self._player.is_paused():
            self._player.resume()
            self._begin_playhead()
            self._refresh_play_btn()
            self._status("Playing your trimmed clip...")
            return
        # stopped: play from the chosen spot, within the trimmed section
        if self._cached_for(self.loaded_url) is None:
            self._pending_play = self._seek_pos
            self._ensure_audio(self.loaded_url)
            self._status("Getting the song ready, then it will play...")
            self._refresh_transport()
            return
        self._play_from(self._seek_pos)

    def _play_from(self, pos: float) -> None:
        """Play within the trimmed section, starting at ``pos`` (clamped into it)."""
        s, e = self._trim_bounds()
        pos = min(max(pos, s), max(s, e - 0.05))
        self._play_end = e            # the playhead loop stops here (don't play past the trim)
        self._seek_pos = pos
        if self._player.play(pos):
            self._refresh_play_btn()
            self._status("Playing your trimmed clip. Click the bar to jump, or Back to start.")
            self._begin_playhead()
        else:
            self._status("Couldn't play the preview on this PC, but your trim will still save fine.")

    def _player_back(self) -> None:
        """Stop and return the playhead to the START of the trimmed section."""
        self._player.stop()
        self._cancel_playhead()
        s, _ = self._trim_bounds()
        self._seek_pos = s
        try:
            self.timeline.set_playhead(s)
        except Exception:
            pass
        self.pos_var.set(self._fmt_pos(s))
        self._refresh_play_btn()

    def _on_seek(self, t: float) -> None:
        """User clicked the waveform: set the play position, clamped INTO the trim."""
        if self.flow_state not in ("loaded", "done"):
            return
        s, e = self._trim_bounds()
        t = min(max(float(t), s), max(s, e - 0.05))   # never outside the trimmed section
        self._seek_pos = t
        try:
            self.timeline.set_playhead(t)
        except Exception:
            pass
        self.pos_var.set(self._fmt_pos(t))
        if self._player.is_playing() or self._player.is_paused():
            self._play_from(t)

    # ---- playhead: poll the player's true position (main thread) ---------- #

    def _begin_playhead(self) -> None:
        self._cancel_playhead()
        self._tick_playhead()

    def _tick_playhead(self) -> None:
        pos = self._player.position()
        end = self._play_end if self._play_end else (self.info.duration if self.info else 0)
        # #4: only ever preview the trimmed section — stop at the end and rewind.
        if self._player.has_ended() or (self._player.is_playing() and end and pos >= end - 0.03):
            self._player.stop()
            self._cancel_playhead()
            s, _ = self._trim_bounds()
            self._seek_pos = s
            try:
                self.timeline.set_playhead(s)
            except Exception:
                pass
            self.pos_var.set(self._fmt_pos(s))
            self._refresh_play_btn()
            self._status("That's your trimmed clip. Press Play to hear it again.")
            return
        try:
            self.timeline.set_playhead(pos)
        except Exception:
            pass
        self.pos_var.set(self._fmt_pos(pos))
        if self._player.is_active():
            self._play_anim = self.after(60, self._tick_playhead)
        else:
            self._play_anim = None
            self._refresh_play_btn()

    def _cancel_playhead(self) -> None:
        if self._play_anim is not None:
            try:
                self.after_cancel(self._play_anim)
            except Exception:
                pass
            self._play_anim = None

    # ---- thumbnail preview ------------------------------------------------ #

    def _fetch_preview(self, url: str, thumb_url: str) -> None:
        img = fetch_image(thumb_url, box=160)
        if img is not None:
            self.q.put(("thumb", (url, img)))

    def default_dir(self) -> str | None:
        """A sensible LOCAL folder for save dialogs to open at.

        Opening the native folder picker without an initialdir lets it enumerate the
        shell root (incl. network/Parallels `\\Mac` shares), which can hang the UI.
        Anchoring it at a local folder avoids that and remembers the last choice.
        """
        import os
        if self._last_dir and os.path.isdir(self._last_dir):
            return self._last_dir
        for cand in (os.path.join(os.path.expanduser("~"), "Downloads"), os.path.expanduser("~")):
            if os.path.isdir(cand):
                return cand
        return None

    def _open_source(self) -> None:
        """Open the loaded video in the default browser (e.g. to find trim points)."""
        url = self.loaded_url
        if url and str(url).startswith("http"):
            try:
                webbrowser.open(url)
            except Exception:
                pass

    def _reset_thumb(self, cursor: str = "") -> None:
        """Clear the thumbnail back to the ♪ placeholder, safely.

        Use image="" (NOT None): Tk skips a None option so it wouldn't clear the
        image, and configuring image=None after the CTkImage was dereferenced
        raised 'image "pyimageN" doesn't exist' on Windows — which aborted Start
        over. image="" clears reliably (only a harmless CTk console warning).
        """
        try:
            self.thumb_lbl.configure(image="", text="♪", cursor=cursor)
        except Exception:
            pass
        self._preview_img = None

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
                elif kind == "audio_ready":
                    self._on_audio_ready(*payload)  # type: ignore[misc]
                elif kind == "audio_error":
                    url, msg = payload  # type: ignore[misc]
                    if url == self.loaded_url:
                        self._prefetch_url = None
                        self._pending_play = None
                        self._refresh_transport()
                        self._status("Couldn't get the audio to preview. You can still Download MP3.")
                elif kind == "waveform":
                    url, peaks = payload  # type: ignore[misc]
                    if url == self.loaded_url:
                        try:
                            self.timeline.set_waveform(peaks)
                        except Exception:
                            pass
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
                        self._expand(1)
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
            # A bug in one handler must NEVER kill the pump OR strand the user with
            # no way forward. Swallow, then recover to an actionable state.
            try:
                self._status("Something went wrong. Please try that again.")
                if self._searching:
                    self._lock_search(False)
                elif self.flow_state == "downloading":
                    pass  # the worker will still post done/error to resolve it
                elif self.info is not None and self.loaded_url:
                    self._set_state("loaded")  # a song is loaded: let them retry/save
                    self._validate_trim()
                elif self.flow_state != "starting":
                    self._set_state("empty")
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
        # Essential state first, so a cosmetic failure below can never leave the
        # screen stuck in "loading" (the cause of the old "something went wrong,
        # no way to retry" bug). Everything after this is wrapped + best-effort.
        self._player.stop()
        self._cancel_playhead()
        self._prefetch_url = None
        self._pending_play = None
        self._seek_pos = 0.0
        self._clear_cache()
        try:
            self._player.load(None, info.duration)  # drop the previous song's audio
        except Exception:
            pass
        self.info = info
        self.loaded_url = url
        self._trim_ok = True
        self.trim_hint_var.set("")
        self.pos_var.set("")
        self._reset_thumb("hand2")
        try:
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
            self.timeline.set_duration(info.duration)
            self._bar_set(0)
        except Exception:
            pass
        self._set_state("loaded")    # ALWAYS reached -> screen is usable
        try:
            self._validate_trim()
        except Exception:
            pass
        self._expand(2)              # accordion advances to "Check the song"
        self._status(f"✓ Loaded: {info.title}"[:140])
        # Fetch the cover preview, and prefetch the audio so preview/save are instant.
        if info.thumbnail:
            threading.Thread(target=self._fetch_preview, args=(url, info.thumbnail), daemon=True).start()
        self._ensure_audio(url)

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
        self._expand(1)
        self._status("That link is a playlist.")
        self._ask_choice(
            "That link is a playlist of songs.\n\nImport every song into "
            "“Download several”?",
            [("Import whole playlist", "playlist"), ("Cancel", "cancel")],
            lambda v: self._open_bulk_with_playlist(url) if v == "playlist" else None)

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
        if self.flow_state == "empty":
            self._expand(1)
        else:
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
                or self._prefetch_url is not None or self._chooser is not None
                or self._bulk is not None or self._choice is not None):
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

    def report_callback_exception(self, exc, val, tb) -> None:
        """Tk calls this for ANY unhandled exception in a callback (button/trace/
        after). In a windowed exe the default just prints to a stderr nobody sees,
        so a crashing handler looks like 'the button does nothing'. Log it to the
        per-user cache and tell the user, never crash here."""
        import traceback
        text = "".join(traceback.format_exception(exc, val, tb))
        try:
            with open(cache_dir() / "error.log", "a", encoding="utf-8") as f:
                f.write(text + "\n" + ("-" * 60) + "\n")
        except Exception:
            pass
        try:
            messagebox.showerror(
                APP_TITLE,
                "Sorry, something went wrong:\n\n" + (str(val) or type(val).__name__)
                + "\n\nTry again, or click Start over / reopen the app.")
        except Exception:
            pass

    def destroy(self) -> None:
        # Stop any preview and clean up the temp audio before the window closes.
        # Idempotent: _closing also stops _poll from rescheduling onto a dead window.
        if getattr(self, "_closing", False):
            return
        self._closing = True
        try:
            self._cancel_playhead()
        except Exception:
            pass
        try:
            self._player.close()
        except Exception:
            pass
        try:
            import shutil
            shutil.rmtree(self._preview_dir, ignore_errors=True)
        except Exception:
            pass
        super().destroy()


# --------------------------------------------------------------------------- #
# Custom choice dialog (overlay, so we can relabel the buttons)
# --------------------------------------------------------------------------- #

class ChoiceOverlay(ctk.CTkFrame):
    """A modal-looking in-window dialog with custom button labels."""

    def __init__(self, app, message: str, buttons, on_pick) -> None:
        super().__init__(app, fg_color=WINDOW_BG, corner_radius=0)
        self.on_pick = on_pick
        card = ctk.CTkFrame(self, corner_radius=14, fg_color=CARD_BG,
                            border_width=1, border_color=CARD_BORDER)
        card.place(relx=0.5, rely=0.45, anchor="center")
        ctk.CTkLabel(card, text=APP_TITLE, text_color=PINK,
                     font=ctk.CTkFont(size=13, weight="bold")).pack(padx=26, pady=(20, 2))
        ctk.CTkLabel(card, text=message, justify="left", wraplength=420,
                     font=ctk.CTkFont(size=14)).pack(padx=26, pady=(6, 16))
        btnrow = ctk.CTkFrame(card, fg_color="transparent")
        btnrow.pack(padx=26, pady=(0, 20))
        for i, (label, value) in enumerate(buttons):
            primary = (i == 0)
            b = ctk.CTkButton(
                btnrow, text=label, height=38, width=150,
                font=ctk.CTkFont(size=13, weight="bold"),
                fg_color=PINK if primary else SECONDARY,
                hover_color=PINK_HOVER if primary else SECONDARY_H,
                text_color="white" if primary else TITLE_ON,
                command=lambda v=value: self.on_pick(v))
            b.grid(row=0, column=i, padx=6)


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
                      fg_color=PINK, hover_color=PINK_HOVER, font=symfont(13),
                      command=lambda u=r.url: self.on_choose(u)).grid(
            row=0, column=2, rowspan=2, padx=(6, 12), pady=10)


def _selfcheck() -> int:
    """`--selfcheck`: report whether the bundled audio backend + ffmpeg work, then
    exit WITHOUT opening the GUI. Writes JSON to cache_dir()/selfcheck.json (and
    stdout). Lets us verify a built exe without needing to click around."""
    import json
    import os
    import shutil
    res = {"version": __version__}
    try:
        import miniaudio  # noqa: F401
        import _miniaudio  # noqa: F401
        res["miniaudio"] = miniaudio.__version__
        try:
            dev = miniaudio.PlaybackDevice()
            dev.close()
            res["device"] = "ok"
        except Exception as e:
            res["device"] = f"fail: {e!r}"
    except Exception as e:
        res["miniaudio"] = f"import-fail: {e!r}"
    ff = ffmpeg_binary()
    res["ffmpeg"] = "ok" if ((os.path.isabs(ff) and os.path.exists(ff)) or shutil.which(ff)) else "missing"
    text = json.dumps(res)
    try:
        with open(cache_dir() / "selfcheck.json", "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass
    print(text)
    return 0


def main() -> None:
    if "--selfcheck" in sys.argv:
        _selfcheck()
        return
    App().mainloop()


if __name__ == "__main__":
    main()
