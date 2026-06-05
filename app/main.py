"""Aubrey's YT-MP3 Downloader — paste a YouTube link, trim, export an MP3.

A guided, can't-go-wrong wizard. A small state machine (see ``_set_state``)
locks every control that isn't usable yet, so the only thing you *can* do is
the next correct step:

    starting -> empty -> loading -> loaded -> downloading -> done
                 ^                                              |
                 +---------------- New video --------------------+

All slow work (resolving yt-dlp/deno, fetching, downloading, converting) runs
on worker threads that post messages to a thread-safe queue drained by the Tk
main loop via ``after`` — so the UI stays responsive and we never touch widgets
off the main thread.
"""

from __future__ import annotations

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

from .audio import make_mp3
from .downloader import DownloadError, VideoInfo, download_audio, fetch_info
from .paths import resource_path
from .updater import ensure_deno, ensure_ytdlp, update_in_background

APP_TITLE = "Aubrey's YT-MP3 Downloader"

# --- pastel-pink palette (light, dark) ---
PINK        = ("#E184AA", "#B5688A")
PINK_HOVER  = ("#D9729E", "#A4587B")
WINDOW_BG   = ("#FDF1F7", "#1C1418")
CARD_BG     = ("#FCE7F0", "#2A2026")
BADGE_PEND  = ("#E9D6DF", "#4A3D44")
BADGE_PEND_T = ("gray38", "gray70")
DONE        = ("#7FC2A0", "#5E9E78")
SECONDARY   = ("gray82", "gray30")
SECONDARY_H = ("gray73", "gray40")
MUTED       = ("gray40", "gray65")
TITLE_ON    = ("gray10", "gray95")

# Badge appearance for each step depending on the current state.
_BADGES = {
    "starting":    {1: "pending", 2: "pending", 3: "pending"},
    "empty":       {1: "active",  2: "pending", 3: "pending"},
    "loading":     {1: "active",  2: "pending", 3: "pending"},
    "loaded":      {1: "done",    2: "active",  3: "active"},
    "downloading": {1: "done",    2: "active",  3: "active"},
    "done":        {1: "done",    2: "done",    3: "done"},
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
            return secs
        return float(text)
    except ValueError:
        raise ValueError(f"'{text}' isn't a valid time. Use mm:ss (e.g. 1:05).")


def fmt_time(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    m, s = divmod(total, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', " ", name or "").strip()
    name = re.sub(r"\s+", " ", name)
    return (name or "audio")[:120]


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
        self.geometry("660x680")
        self.minsize(640, 680)
        self.configure(fg_color=WINDOW_BG)
        self._apply_icon()

        self.info: VideoInfo | None = None
        self.ytdlp: str | None = None
        self.deno: str | None = None
        self.ready = False
        self.flow_state = "starting"
        self.loaded_url: str | None = None
        self._indet = False
        self._badges: dict[int, ctk.CTkLabel] = {}
        self._titles: dict[int, ctk.CTkLabel] = {}
        self.q: "queue.Queue[tuple[str, object]]" = queue.Queue()

        self._build_ui()
        self._set_state("starting")
        self.after(100, self._poll)
        threading.Thread(target=self._init_engine, daemon=True).start()

    # ---- UI construction ------------------------------------------------- #

    def _step(self, row: int, number: int, title: str) -> ctk.CTkFrame:
        """Create a numbered 'step' card; store its badge/title; return its body."""
        card = ctk.CTkFrame(self, corner_radius=12, fg_color=CARD_BG)
        card.grid(row=row, column=0, sticky="ew", padx=18, pady=7)
        card.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 0))
        badge = ctk.CTkLabel(
            head, text=str(number), width=28, height=28, corner_radius=14,
            font=ctk.CTkFont(size=14, weight="bold"))
        badge.grid(row=0, column=0, padx=(0, 10))
        title_lbl = ctk.CTkLabel(head, text=title, font=ctk.CTkFont(size=15, weight="bold"))
        title_lbl.grid(row=0, column=1, sticky="w")
        self._badges[number] = badge
        self._titles[number] = title_lbl

        body = ctk.CTkFrame(card, fg_color="transparent")
        body.grid(row=1, column=0, sticky="ew", padx=14, pady=(8, 14))
        body.grid_columnconfigure(0, weight=1)
        return body

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)

        # --- Header (title + New video) ---
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 2))
        header.grid_columnconfigure(0, weight=1)
        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(titles, text=APP_TITLE, font=ctk.CTkFont(size=22, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(
            titles, text="Turn a YouTube song into an MP3 for the Yoto player.",
            text_color=MUTED, font=ctk.CTkFont(size=13)).pack(anchor="w")
        self.new_btn = ctk.CTkButton(
            header, text="↺  New video", width=110, fg_color=SECONDARY,
            hover_color=SECONDARY_H, text_color=TITLE_ON, command=self._on_new)
        self.new_btn.grid(row=0, column=1, sticky="e", padx=(8, 0))

        # --- Step 1: link ---
        b1 = self._step(1, 1, "Paste a YouTube link")
        row1 = ctk.CTkFrame(b1, fg_color="transparent")
        row1.grid(row=0, column=0, sticky="ew")
        row1.grid_columnconfigure(0, weight=1)
        self.url_var = tk.StringVar()
        self.url_entry = ctk.CTkEntry(
            row1, textvariable=self.url_var, height=42,
            placeholder_text="https://www.youtube.com/watch?v=…")
        self.url_entry.grid(row=0, column=0, sticky="ew")
        self.url_entry.bind("<Return>", lambda _e: self._on_fetch())
        # Paste lives INSIDE the box (subtle), so it doesn't compete with the
        # real "proceed" action, which is Get info.
        self.paste_btn = ctk.CTkButton(
            row1, text="Paste", width=60, height=28, fg_color=SECONDARY,
            hover_color=SECONDARY_H, text_color=TITLE_ON, command=self._on_paste)
        self.paste_btn.place(in_=self.url_entry, relx=1.0, rely=0.5, x=-6, anchor="e")
        self.fetch_btn = ctk.CTkButton(
            row1, text="Get info  →", width=120, height=42,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=PINK, hover_color=PINK_HOVER, command=self._on_fetch)
        self.fetch_btn.grid(row=0, column=1, padx=(10, 0))
        self.url_var.trace_add("write", lambda *_: self._refresh_fetch_btn())
        ctk.CTkLabel(
            b1, text="Paste your link, then click “Get info” to load the song.",
            text_color=MUTED, font=ctk.CTkFont(size=12)).grid(row=1, column=0, sticky="w", pady=(6, 0))

        # --- Step 2: title ---
        b2 = self._step(2, 2, "Check the song title")
        row2 = ctk.CTkFrame(b2, fg_color="transparent")
        row2.grid(row=0, column=0, sticky="ew")
        row2.grid_columnconfigure(0, weight=1)
        self.title_var = tk.StringVar()
        self.title_entry = ctk.CTkEntry(row2, textvariable=self.title_var, placeholder_text="(loads after step 1)")
        self.title_entry.grid(row=0, column=0, sticky="ew")
        self.length_var = tk.StringVar(value="Length: —")
        ctk.CTkLabel(row2, textvariable=self.length_var, width=110, text_color=MUTED).grid(row=0, column=1, padx=(10, 0))
        ctk.CTkLabel(
            b2, text="This name shows under the track in the Yoto app — edit it if you like.",
            text_color=MUTED, font=ctk.CTkFont(size=12)).grid(row=1, column=0, sticky="w", pady=(6, 0))

        # --- Step 3: trim ---
        b3 = self._step(3, 3, "Trim the song  (optional)")
        times = ctk.CTkFrame(b3, fg_color="transparent")
        times.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(times, text="Start").grid(row=0, column=0, padx=(0, 6))
        self.start_var = tk.StringVar(value="0:00")
        self.start_entry = ctk.CTkEntry(times, textvariable=self.start_var, width=80)
        self.start_entry.grid(row=0, column=1)
        ctk.CTkLabel(times, text="End").grid(row=0, column=2, padx=(18, 6))
        self.end_var = tk.StringVar()
        self.end_entry = ctk.CTkEntry(times, textvariable=self.end_var, width=80, placeholder_text="end")
        self.end_entry.grid(row=0, column=3)
        ctk.CTkLabel(times, text="(mm:ss)", text_color=MUTED).grid(row=0, column=4, padx=(8, 0))

        quick = ctk.CTkFrame(b3, fg_color="transparent")
        quick.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ctk.CTkLabel(quick, text="Skip first").grid(row=0, column=0, padx=(0, 6))
        self.skipfirst_var = tk.StringVar(value="0")
        self.skipfirst_entry = ctk.CTkEntry(quick, textvariable=self.skipfirst_var, width=52)
        self.skipfirst_entry.grid(row=0, column=1)
        self.skipfirst_btn = ctk.CTkButton(
            quick, text="sec ✓", width=58, fg_color=SECONDARY, hover_color=SECONDARY_H,
            text_color=TITLE_ON, command=self._skip_first)
        self.skipfirst_btn.grid(row=0, column=2, padx=(4, 0))
        ctk.CTkLabel(quick, text="Skip last").grid(row=0, column=3, padx=(18, 6))
        self.skiplast_var = tk.StringVar(value="0")
        self.skiplast_entry = ctk.CTkEntry(quick, textvariable=self.skiplast_var, width=52)
        self.skiplast_entry.grid(row=0, column=4)
        self.skiplast_btn = ctk.CTkButton(
            quick, text="sec ✓", width=58, fg_color=SECONDARY, hover_color=SECONDARY_H,
            text_color=TITLE_ON, command=self._skip_last)
        self.skiplast_btn.grid(row=0, column=5, padx=(4, 0))
        ctk.CTkLabel(
            b3, text="Leave as-is to keep the whole song. Use Skip first/last to cut an intro or outro.",
            text_color=MUTED, font=ctk.CTkFont(size=12)).grid(row=2, column=0, sticky="w", pady=(8, 0))

        # --- Download (primary action) ---
        self.download_btn = ctk.CTkButton(
            self, text="⬇   Download MP3", height=48,
            font=ctk.CTkFont(size=16, weight="bold"),
            fg_color=PINK, hover_color=PINK_HOVER, command=self._on_download)
        self.download_btn.grid(row=4, column=0, sticky="ew", padx=18, pady=(12, 6))

        # --- Footer: progress + status ---
        self.progress = ctk.CTkProgressBar(self, progress_color=PINK)
        self.progress.grid(row=5, column=0, sticky="ew", padx=18, pady=(4, 2))
        self.progress.set(0)
        self.status_var = tk.StringVar(value="Starting up…")
        ctk.CTkLabel(self, textvariable=self.status_var, anchor="w", text_color=MUTED).grid(
            row=6, column=0, sticky="ew", padx=18, pady=(0, 14))

        self._step3_widgets = [
            self.start_entry, self.end_entry, self.skipfirst_entry,
            self.skiplast_entry, self.skipfirst_btn, self.skiplast_btn,
        ]

    # ---- window icon + progress bar (main thread only) ------------------- #

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
        self.progress.set(value)

    # ---- the state machine ----------------------------------------------- #

    def _enable(self, widgets, on: bool) -> None:
        for w in widgets:
            try:
                w.configure(state="normal" if on else "disabled")
            except Exception:
                pass

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
        editing_link = state == "empty"
        loaded = state in ("loaded", "done")

        self._enable([self.url_entry, self.paste_btn], editing_link)
        self._enable([self.title_entry], loaded)
        self._enable(self._step3_widgets, loaded)
        self.download_btn.configure(state="normal" if loaded else "disabled")
        self.new_btn.configure(state="normal" if loaded else "disabled")

        for n, kind in _BADGES[state].items():
            self._set_badge(n, kind)
        self._refresh_fetch_btn()

    def _refresh_fetch_btn(self) -> None:
        ok = self.flow_state == "empty" and self.ready and bool(self.url_var.get().strip())
        try:
            self.fetch_btn.configure(state="normal" if ok else "disabled")
        except Exception:
            pass

    # ---- background: resolve yt-dlp + deno ------------------------------- #

    def _init_engine(self) -> None:
        try:
            cmd = ensure_ytdlp(status=lambda m: self.q.put(("status", m)))
            self.q.put(("ytdlp", cmd))
            update_in_background(cmd)
            deno = ensure_deno(status=lambda m: self.q.put(("status", m)))
            self.q.put(("deno", deno))
            self.q.put(("ready", None))
            self.q.put(("status", "Ready — paste a YouTube link above."))
        except Exception as e:
            self.q.put(("error", f"Couldn't finish setting up.\n\n{e}\n\n"
                                 "Please check your internet connection and reopen the app."))

    # ---- button handlers -------------------------------------------------- #

    def _on_paste(self) -> None:
        try:
            self.url_var.set(self.clipboard_get().strip())
        except tk.TclError:
            return
        self.url_entry.focus_set()

    def _on_new(self) -> None:
        self.info = None
        self.loaded_url = None
        self.url_var.set("")
        self.title_var.set("")
        self.length_var.set("Length: —")
        self.start_var.set("0:00")
        self.end_var.set("")
        self.skipfirst_var.set("0")
        self.skiplast_var.set("0")
        self._bar_set(0)
        self._set_state("empty")
        self._status("Ready — paste a YouTube link above.")
        self.url_entry.focus_set()

    def _skip_first(self) -> None:
        try:
            n = float(self.skipfirst_var.get() or 0)
        except ValueError:
            messagebox.showwarning(APP_TITLE, "Enter a number of seconds to skip.")
            return
        self.start_var.set(fmt_time(n))

    def _skip_last(self) -> None:
        if not self.info:
            return
        try:
            n = float(self.skiplast_var.get() or 0)
        except ValueError:
            messagebox.showwarning(APP_TITLE, "Enter a number of seconds to skip.")
            return
        self.end_var.set(fmt_time(max(0, self.info.duration - n)))

    def _on_fetch(self) -> None:
        if self.flow_state != "empty" or not self.ready:
            return
        url = self.url_var.get().strip()
        if not url:
            return
        self._set_state("loading")
        self._status("Reading song details…")
        self._bar_indeterminate()
        threading.Thread(target=self._fetch_worker, args=(url,), daemon=True).start()

    def _fetch_worker(self, url: str) -> None:
        try:
            self.q.put(("info", (url, fetch_info(self.ytdlp, url, self.deno))))
        except DownloadError as e:
            self.q.put(("error", str(e)))
        except Exception as e:
            self.q.put(("error", f"Couldn't read that link.\n\n{e}"))

    def _on_download(self) -> None:
        if self.flow_state not in ("loaded", "done"):
            return
        url = self.url_var.get().strip()
        try:
            start = parse_time(self.start_var.get())
            end_text = self.end_var.get().strip()
            end = parse_time(end_text) if end_text else None
        except ValueError as e:
            messagebox.showwarning(APP_TITLE, str(e))
            return
        if end is not None and end <= start:
            messagebox.showwarning(APP_TITLE, "The end time must be after the start time.")
            return

        display_title = self.title_var.get().strip() or "audio"
        dest = filedialog.asksaveasfilename(
            title="Save MP3 as…", defaultextension=".mp3",
            initialfile=f"{safe_filename(display_title)}.mp3",
            filetypes=[("MP3 audio", "*.mp3")])
        if not dest:
            return

        self._set_state("downloading")
        self._status("Preparing…")
        self._bar_indeterminate()
        threading.Thread(
            target=self._download_worker,
            args=(url, Path(dest), display_title, start, end), daemon=True).start()

    def _download_worker(self, url, dest: Path, title, start, end) -> None:
        try:
            with tempfile.TemporaryDirectory(prefix="aubreymp3_") as tmp:
                tmpdir = Path(tmp)
                self.q.put(("status", "Downloading audio…"))
                audio, thumb = download_audio(
                    self.ytdlp, url, tmpdir, deno=self.deno,
                    on_progress=lambda p: self.q.put(("progress", p / 100.0)))
                self.q.put(("status", "Converting to MP3…"))
                self.q.put(("busy_bar", None))
                make_mp3(audio, dest, title=title, start=start, end=end, cover=thumb)
            self.q.put(("done", dest))
        except DownloadError as e:
            self.q.put(("error", str(e)))
        except Exception as e:
            self.q.put(("error", f"Something went wrong.\n\n{e}"))

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
                elif kind == "ytdlp":
                    self.ytdlp = str(payload)
                elif kind == "deno":
                    self.deno = payload  # type: ignore[assignment]
                elif kind == "ready":
                    self.ready = True
                    if self.flow_state == "starting":
                        self._set_state("empty")
                elif kind == "done":
                    self._on_done(Path(str(payload)))
                elif kind == "error":
                    self._on_error(str(payload))
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # ---- state updates ---------------------------------------------------- #

    def _on_info(self, url: str, info: VideoInfo) -> None:
        self.info = info
        self.loaded_url = url
        self.title_var.set(info.title)
        self.length_var.set(f"Length: {fmt_time(info.duration)}")
        self.start_var.set("0:00")
        self.end_var.set(fmt_time(info.duration))
        self._bar_set(0)
        self._set_state("loaded")
        self._status(f"✓ Loaded: {info.title}")

    def _on_done(self, path: Path) -> None:
        self._bar_set(1.0)
        self._set_state("done")
        self._status(f"✓ Done! Saved {path.name}  —  click “New video” for another.")
        if messagebox.askyesno(APP_TITLE, f"Saved:\n{path.name}\n\nOpen the folder?"):
            open_folder(path.parent)

    def _on_error(self, message: str) -> None:
        self._bar_set(0)
        # return to the step the user can act on: re-enter the link, or retry download
        self._set_state("empty" if self.flow_state in ("loading", "starting") else "loaded")
        self._status("")
        messagebox.showerror(APP_TITLE, message)

    def _status(self, text: str) -> None:
        self.status_var.set(text)


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
