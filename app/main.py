"""Aubrey's YT-MP3 Downloader — paste a YouTube link, trim, export an MP3.

Single-window customtkinter app.  All slow work (resolving yt-dlp, fetching
info, downloading, converting) runs on worker threads; they post messages to a
thread-safe queue that the Tk main loop drains via ``after`` so the UI stays
responsive and we never touch widgets off the main thread.
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
from .updater import ensure_ytdlp, update_in_background

APP_TITLE = "Aubrey's YT-MP3 Downloader"


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
        self.geometry("580x460")
        self.minsize(560, 460)

        self.info: VideoInfo | None = None
        self.ytdlp: str | None = None
        self.busy = False
        self.q: "queue.Queue[tuple[str, object]]" = queue.Queue()

        self._build_ui()
        self.after(100, self._poll)
        threading.Thread(target=self._init_ytdlp, daemon=True).start()

    # ---- UI construction ------------------------------------------------- #

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        pad = {"padx": 16, "pady": 6}

        ctk.CTkLabel(
            self, text=APP_TITLE, font=ctk.CTkFont(size=20, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(14, 2))

        # --- URL row ---
        url_row = ctk.CTkFrame(self, fg_color="transparent")
        url_row.grid(row=1, column=0, sticky="ew", **pad)
        url_row.grid_columnconfigure(0, weight=1)
        self.url_var = tk.StringVar()
        ctk.CTkEntry(
            url_row, textvariable=self.url_var, placeholder_text="Paste a YouTube link…",
        ).grid(row=0, column=0, sticky="ew")
        self.paste_btn = ctk.CTkButton(url_row, text="Paste", width=70, command=self._on_paste)
        self.paste_btn.grid(row=0, column=1, padx=(8, 0))
        self.fetch_btn = ctk.CTkButton(url_row, text="Fetch info", width=90, command=self._on_fetch)
        self.fetch_btn.grid(row=0, column=2, padx=(8, 0))

        # --- Title row ---
        title_row = ctk.CTkFrame(self, fg_color="transparent")
        title_row.grid(row=2, column=0, sticky="ew", **pad)
        title_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(title_row, text="Title").grid(row=0, column=0, padx=(0, 8))
        self.title_var = tk.StringVar()
        ctk.CTkEntry(
            title_row, textvariable=self.title_var, placeholder_text="(song title)",
        ).grid(row=0, column=1, sticky="ew")
        self.length_var = tk.StringVar(value="Length: —")
        ctk.CTkLabel(title_row, textvariable=self.length_var, width=110).grid(row=0, column=2, padx=(8, 0))

        # --- Trim section ---
        trim = ctk.CTkFrame(self)
        trim.grid(row=3, column=0, sticky="ew", **pad)
        for c in range(4):
            trim.grid_columnconfigure(c, weight=1)
        ctk.CTkLabel(
            trim, text="Trim", font=ctk.CTkFont(weight="bold"),
        ).grid(row=0, column=0, columnspan=4, sticky="w", padx=12, pady=(8, 2))

        ctk.CTkLabel(trim, text="Start (mm:ss)").grid(row=1, column=0, sticky="e", padx=6)
        self.start_var = tk.StringVar(value="0:00")
        ctk.CTkEntry(trim, textvariable=self.start_var, width=90).grid(row=1, column=1, sticky="w")
        ctk.CTkLabel(trim, text="End (mm:ss)").grid(row=1, column=2, sticky="e", padx=6)
        self.end_var = tk.StringVar()
        ctk.CTkEntry(trim, textvariable=self.end_var, width=90).grid(row=1, column=3, sticky="w")

        ctk.CTkLabel(trim, text="Skip first").grid(row=2, column=0, sticky="e", padx=6, pady=(4, 10))
        self.skipfirst_var = tk.StringVar(value="0")
        ctk.CTkEntry(trim, textvariable=self.skipfirst_var, width=60).grid(row=2, column=1, sticky="w", pady=(4, 10))
        ctk.CTkButton(trim, text="↥ trim front", width=90, command=self._skip_first).grid(
            row=2, column=1, sticky="e", padx=(0, 6), pady=(4, 10))
        ctk.CTkLabel(trim, text="Skip last").grid(row=2, column=2, sticky="e", padx=6, pady=(4, 10))
        self.skiplast_var = tk.StringVar(value="0")
        ctk.CTkEntry(trim, textvariable=self.skiplast_var, width=60).grid(row=2, column=3, sticky="w", pady=(4, 10))
        ctk.CTkButton(trim, text="↧ trim end", width=90, command=self._skip_last).grid(
            row=2, column=3, sticky="e", padx=(0, 6), pady=(4, 10))

        # --- Progress + status ---
        self.progress = ctk.CTkProgressBar(self)
        self.progress.grid(row=4, column=0, sticky="ew", padx=16, pady=(10, 2))
        self.progress.set(0)
        self.status_var = tk.StringVar(value="Starting up…")
        ctk.CTkLabel(self, textvariable=self.status_var, anchor="w").grid(
            row=5, column=0, sticky="ew", padx=16)

        # --- Download button ---
        self.download_btn = ctk.CTkButton(
            self, text="Download MP3", height=40,
            font=ctk.CTkFont(size=15, weight="bold"), command=self._on_download)
        self.download_btn.grid(row=6, column=0, sticky="ew", padx=16, pady=(10, 16))

    # ---- background: resolve yt-dlp -------------------------------------- #

    def _init_ytdlp(self) -> None:
        try:
            cmd = ensure_ytdlp(status=lambda m: self.q.put(("status", m)))
            self.q.put(("ytdlp", cmd))
            update_in_background(cmd)
            self.q.put(("status", "Ready — paste a YouTube link and click Fetch info."))
        except Exception as e:
            self.q.put(("error", f"Couldn't set up the downloader.\n\n{e}\n\n"
                                 "Please check your internet connection and reopen the app."))

    # ---- button handlers -------------------------------------------------- #

    def _on_paste(self) -> None:
        try:
            self.url_var.set(self.clipboard_get().strip())
        except tk.TclError:
            pass

    def _skip_first(self) -> None:
        try:
            n = float(self.skipfirst_var.get() or 0)
        except ValueError:
            messagebox.showwarning(APP_TITLE, "Enter a number of seconds to skip.")
            return
        self.start_var.set(fmt_time(n))

    def _skip_last(self) -> None:
        if not self.info:
            messagebox.showinfo(APP_TITLE, "Click 'Fetch info' first so I know how long the video is.")
            return
        try:
            n = float(self.skiplast_var.get() or 0)
        except ValueError:
            messagebox.showwarning(APP_TITLE, "Enter a number of seconds to skip.")
            return
        self.end_var.set(fmt_time(max(0, self.info.duration - n)))

    def _on_fetch(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            messagebox.showinfo(APP_TITLE, "Please paste a YouTube link first.")
            return
        if not self.ytdlp:
            self._status("Still starting up… try again in a moment.")
            return
        self._set_busy(True, "Reading video…")
        threading.Thread(target=self._fetch_worker, args=(url,), daemon=True).start()

    def _fetch_worker(self, url: str) -> None:
        try:
            self.q.put(("info", fetch_info(self.ytdlp, url)))
        except DownloadError as e:
            self.q.put(("error", str(e)))
        except Exception as e:
            self.q.put(("error", f"Couldn't read that video.\n\n{e}"))
        finally:
            self.q.put(("idle", None))

    def _on_download(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            messagebox.showinfo(APP_TITLE, "Please paste a YouTube link first.")
            return
        if not self.ytdlp or self.busy:
            return
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
            title="Save MP3 as…",
            defaultextension=".mp3",
            initialfile=f"{safe_filename(display_title)}.mp3",
            filetypes=[("MP3 audio", "*.mp3")],
        )
        if not dest:
            return

        self._set_busy(True, "Starting…")
        self.progress.configure(mode="determinate")
        self.progress.set(0)
        threading.Thread(
            target=self._download_worker,
            args=(url, Path(dest), display_title, start, end),
            daemon=True,
        ).start()

    def _download_worker(self, url, dest: Path, title, start, end) -> None:
        try:
            with tempfile.TemporaryDirectory(prefix="aubreymp3_") as tmp:
                tmpdir = Path(tmp)
                self.q.put(("status", "Downloading audio…"))
                audio, thumb = download_audio(
                    self.ytdlp, url, tmpdir,
                    on_progress=lambda p: self.q.put(("progress", p / 100.0)),
                )
                self.q.put(("status", "Converting to MP3…"))
                self.q.put(("busy_bar", None))
                make_mp3(audio, dest, title=title, start=start, end=end, cover=thumb)
            self.q.put(("done", dest))
        except DownloadError as e:
            self.q.put(("error", str(e)))
        except Exception as e:
            self.q.put(("error", f"Something went wrong.\n\n{e}"))
        finally:
            self.q.put(("idle", None))

    # ---- queue pump (main thread) ---------------------------------------- #

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "status":
                    self._status(str(payload))
                elif kind == "progress":
                    self.progress.configure(mode="determinate")
                    self.progress.set(float(payload))
                elif kind == "busy_bar":
                    self.progress.configure(mode="indeterminate")
                    self.progress.start()
                elif kind == "info":
                    self._on_info(payload)  # type: ignore[arg-type]
                elif kind == "ytdlp":
                    self.ytdlp = str(payload)
                elif kind == "done":
                    self._on_done(Path(str(payload)))
                elif kind == "error":
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.progress.set(0)
                    self._status("")
                    messagebox.showerror(APP_TITLE, str(payload))
                elif kind == "idle":
                    self._set_busy(False)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # ---- state updates ---------------------------------------------------- #

    def _on_info(self, info: VideoInfo) -> None:
        self.info = info
        self.title_var.set(info.title)
        self.length_var.set(f"Length: {fmt_time(info.duration)}")
        if not self.start_var.get().strip():
            self.start_var.set("0:00")
        self.end_var.set(fmt_time(info.duration))
        self._status(f"Loaded: {info.title}")

    def _on_done(self, path: Path) -> None:
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress.set(1.0)
        self._status(f"Done! Saved {path.name}")
        if messagebox.askyesno(APP_TITLE, f"Saved:\n{path.name}\n\nOpen the folder?"):
            open_folder(path.parent)

    def _status(self, text: str) -> None:
        self.status_var.set(text)

    def _set_busy(self, busy: bool, status: str | None = None) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for w in (self.fetch_btn, self.download_btn, self.paste_btn):
            w.configure(state=state)
        if status is not None:
            self._status(status)


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
