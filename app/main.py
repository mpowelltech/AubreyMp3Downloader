"""Aubrey's YT-MP3 Downloader — paste a YouTube link, trim, export an MP3.

Single-window customtkinter app with a clear numbered flow:
    1. paste a link (details load automatically)
    2. check the title
    3. trim (optional)
    4. download

All slow work (resolving yt-dlp/deno, fetching info, downloading, converting)
runs on worker threads; they post messages to a thread-safe queue that the Tk
main loop drains via ``after`` so the UI stays responsive and we never touch
widgets off the main thread.
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
from .updater import ensure_deno, ensure_ytdlp, update_in_background

APP_TITLE = "Aubrey's YT-MP3 Downloader"
ACCENT = ("#3B8ED0", "#1F6AA5")
GO_GREEN = ("#2FA572", "#2FA572")
GO_GREEN_HOVER = ("#268A61", "#217954")
MUTED = ("gray45", "gray60")


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
        self.geometry("660x640")
        self.minsize(640, 640)

        self.info: VideoInfo | None = None
        self.ytdlp: str | None = None
        self.deno: str | None = None
        self.ready = False
        self.busy = False
        self.loaded_url: str | None = None
        self.q: "queue.Queue[tuple[str, object]]" = queue.Queue()

        self._build_ui()
        self.after(100, self._poll)
        threading.Thread(target=self._init_engine, daemon=True).start()

    # ---- UI construction ------------------------------------------------- #

    def _step(self, row: int, number: int, title: str) -> ctk.CTkFrame:
        """Create a numbered 'step' card and return its body frame to fill."""
        card = ctk.CTkFrame(self, corner_radius=10)
        card.grid(row=row, column=0, sticky="ew", padx=18, pady=7)
        card.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 0))
        ctk.CTkLabel(
            head, text=str(number), width=26, height=26, corner_radius=13,
            fg_color=ACCENT, text_color="white", font=ctk.CTkFont(size=14, weight="bold"),
        ).grid(row=0, column=0, padx=(0, 10))
        ctk.CTkLabel(head, text=title, font=ctk.CTkFont(size=15, weight="bold")).grid(
            row=0, column=1, sticky="w")

        body = ctk.CTkFrame(card, fg_color="transparent")
        body.grid(row=1, column=0, sticky="ew", padx=14, pady=(8, 14))
        body.grid_columnconfigure(0, weight=1)
        return body

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)

        # --- Header ---
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 2))
        ctk.CTkLabel(header, text=APP_TITLE, font=ctk.CTkFont(size=22, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(
            header, text="Turn a YouTube song into an MP3 for the Yoto player.",
            text_color=MUTED, font=ctk.CTkFont(size=13),
        ).pack(anchor="w")

        # --- Step 1: link ---
        b1 = self._step(1, 1, "Paste a YouTube link")
        row1 = ctk.CTkFrame(b1, fg_color="transparent")
        row1.grid(row=0, column=0, sticky="ew")
        row1.grid_columnconfigure(0, weight=1)
        self.url_var = tk.StringVar()
        self.url_entry = ctk.CTkEntry(row1, textvariable=self.url_var, placeholder_text="https://www.youtube.com/watch?v=…")
        self.url_entry.grid(row=0, column=0, sticky="ew")
        self.url_entry.bind("<Return>", lambda _e: self._on_fetch())
        self.paste_btn = ctk.CTkButton(row1, text="Paste", width=72, command=self._on_paste)
        self.paste_btn.grid(row=0, column=1, padx=(8, 0))
        self.fetch_btn = ctk.CTkButton(row1, text="Get info", width=84, command=self._on_fetch)
        self.fetch_btn.grid(row=0, column=2, padx=(8, 0))
        ctk.CTkLabel(
            b1, text="Copy the link from YouTube, click Paste — the song details load automatically.",
            text_color=MUTED, font=ctk.CTkFont(size=12),
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))

        # --- Step 2: title ---
        b2 = self._step(2, 2, "Check the song title")
        row2 = ctk.CTkFrame(b2, fg_color="transparent")
        row2.grid(row=0, column=0, sticky="ew")
        row2.grid_columnconfigure(0, weight=1)
        self.title_var = tk.StringVar()
        ctk.CTkEntry(row2, textvariable=self.title_var, placeholder_text="(loads after you paste a link)").grid(
            row=0, column=0, sticky="ew")
        self.length_var = tk.StringVar(value="Length: —")
        ctk.CTkLabel(row2, textvariable=self.length_var, width=110, text_color=MUTED).grid(row=0, column=1, padx=(10, 0))
        ctk.CTkLabel(
            b2, text="This is the name that shows under the track in the Yoto app — edit it if you like.",
            text_color=MUTED, font=ctk.CTkFont(size=12),
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))

        # --- Step 3: trim ---
        b3 = self._step(3, 3, "Trim the song  (optional)")
        times = ctk.CTkFrame(b3, fg_color="transparent")
        times.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(times, text="Start").grid(row=0, column=0, padx=(0, 6))
        self.start_var = tk.StringVar(value="0:00")
        ctk.CTkEntry(times, textvariable=self.start_var, width=80).grid(row=0, column=1)
        ctk.CTkLabel(times, text="End").grid(row=0, column=2, padx=(18, 6))
        self.end_var = tk.StringVar()
        ctk.CTkEntry(times, textvariable=self.end_var, width=80, placeholder_text="end").grid(row=0, column=3)
        ctk.CTkLabel(times, text="(mm:ss)", text_color=MUTED).grid(row=0, column=4, padx=(8, 0))

        quick = ctk.CTkFrame(b3, fg_color="transparent")
        quick.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ctk.CTkLabel(quick, text="Skip first").grid(row=0, column=0, padx=(0, 6))
        self.skipfirst_var = tk.StringVar(value="0")
        ctk.CTkEntry(quick, textvariable=self.skipfirst_var, width=52).grid(row=0, column=1)
        ctk.CTkButton(quick, text="sec ✓", width=58, command=self._skip_first).grid(row=0, column=2, padx=(4, 0))
        ctk.CTkLabel(quick, text="Skip last").grid(row=0, column=3, padx=(18, 6))
        self.skiplast_var = tk.StringVar(value="0")
        ctk.CTkEntry(quick, textvariable=self.skiplast_var, width=52).grid(row=0, column=4)
        ctk.CTkButton(quick, text="sec ✓", width=58, command=self._skip_last).grid(row=0, column=5, padx=(4, 0))
        ctk.CTkLabel(
            b3, text="Leave as-is to keep the whole song. Use 'Skip first/last' to cut an intro or outro.",
            text_color=MUTED, font=ctk.CTkFont(size=12),
        ).grid(row=2, column=0, sticky="w", pady=(8, 0))

        # --- Step 4: download (primary action) ---
        self.download_btn = ctk.CTkButton(
            self, text="4   Download MP3", height=46,
            font=ctk.CTkFont(size=16, weight="bold"),
            fg_color=GO_GREEN, hover_color=GO_GREEN_HOVER, command=self._on_download)
        self.download_btn.grid(row=4, column=0, sticky="ew", padx=18, pady=(12, 6))

        # --- Footer: progress + status ---
        self.progress = ctk.CTkProgressBar(self)
        self.progress.grid(row=5, column=0, sticky="ew", padx=18, pady=(4, 2))
        self.progress.set(0)
        self.status_var = tk.StringVar(value="Starting up…")
        ctk.CTkLabel(self, textvariable=self.status_var, anchor="w", text_color=MUTED).grid(
            row=6, column=0, sticky="ew", padx=18, pady=(0, 14))

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
        self._on_fetch()

    def _skip_first(self) -> None:
        try:
            n = float(self.skipfirst_var.get() or 0)
        except ValueError:
            messagebox.showwarning(APP_TITLE, "Enter a number of seconds to skip.")
            return
        self.start_var.set(fmt_time(n))

    def _skip_last(self) -> None:
        if not self.info:
            messagebox.showinfo(APP_TITLE, "Paste a link first so I know how long the song is.")
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
            return
        if not self.ready:
            self._status("Still starting up… one moment, then try again.")
            return
        if self.busy:
            return
        self._set_busy(True, "Reading song details…")
        threading.Thread(target=self._fetch_worker, args=(url,), daemon=True).start()

    def _fetch_worker(self, url: str) -> None:
        try:
            self.q.put(("info", (url, fetch_info(self.ytdlp, url, self.deno))))
        except DownloadError as e:
            self.q.put(("error", str(e)))
        except Exception as e:
            self.q.put(("error", f"Couldn't read that link.\n\n{e}"))
        finally:
            self.q.put(("idle", None))

    def _on_download(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            messagebox.showinfo(APP_TITLE, "Paste a YouTube link first (step 1).")
            return
        if not self.ready:
            self._status("Still starting up… one moment.")
            return
        if self.busy:
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
            title="Save MP3 as…", defaultextension=".mp3",
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
                    self.ytdlp, url, tmpdir, deno=self.deno,
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
                    self.progress.set(float(payload))  # type: ignore[arg-type]
                elif kind == "busy_bar":
                    self.progress.configure(mode="indeterminate")
                    self.progress.start()
                elif kind == "info":
                    url, info = payload  # type: ignore[misc]
                    self._on_info(url, info)
                elif kind == "ytdlp":
                    self.ytdlp = str(payload)
                elif kind == "deno":
                    self.deno = payload  # type: ignore[assignment]
                elif kind == "ready":
                    self.ready = True
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

    def _on_info(self, url: str, info: VideoInfo) -> None:
        self.info = info
        self.loaded_url = url
        self.title_var.set(info.title)
        self.length_var.set(f"Length: {fmt_time(info.duration)}")
        if not self.start_var.get().strip():
            self.start_var.set("0:00")
        self.end_var.set(fmt_time(info.duration))
        self._status(f"✓ Loaded: {info.title}")

    def _on_done(self, path: Path) -> None:
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress.set(1.0)
        self._status(f"✓ Done! Saved {path.name}")
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
