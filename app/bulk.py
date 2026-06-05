"""Bulk mode: add many YouTube links, fetch them all, edit each, download all.

A separate window (opened from the single-song screen). Reuses the same engine
(yt-dlp + deno) and the proven per-song pipeline (fetch_info / download_audio /
make_mp3). Same can't-go-wrong philosophy as single mode: controls light up only
when usable, everything locks during a run, each row shows its own live status,
failed rows are skipped, and all files save to one folder you pick once.
"""

from __future__ import annotations

import queue
import re
import tempfile
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .audio import make_mp3
from .downloader import DownloadError, download_audio, fetch_info
from .main import (APP_TITLE, CARD_BG, DISABLED_BG, DISABLED_TX, DONE, MUTED,
                   PINK, PINK_HOVER, SECONDARY, SECONDARY_H, TITLE_ON, WINDOW_BG,
                   fmt_time, open_folder, parse_time, safe_filename)

ERR = ("#C0392B", "#E57373")
DEL_HOVER = ("#E8A6A6", "#7E3A3A")

# status -> (icon glyph, colour)
_ICON = {
    "queued":      ("•", MUTED),
    "loading":     ("◌", PINK),
    "ok":          ("✓", DONE),
    "error":       ("✕", ERR),
    "downloading": ("↓", PINK),
    "converting":  ("♪", PINK),
    "done":        ("✓", DONE),
    "failed":      ("✕", ERR),
}


def _short(url: str, n: int = 64) -> str:
    return url if len(url) <= n else url[: n - 1] + "…"


def _unique(path: Path) -> Path:
    """Avoid overwriting when two songs share a title."""
    if not path.exists():
        return path
    i = 2
    while True:
        p = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not p.exists():
            return p
        i += 1


class BulkRow:
    """One song: its URL, fetched info, per-song trim/title, and its widgets."""

    def __init__(self, master, url: str, on_delete) -> None:
        self.url = url
        self.on_delete = on_delete
        self.title = ""
        self.duration = 0.0
        self.status = "queued"
        self.error = ""
        self.disabled = False  # set True by the window while a batch runs

        self.title_var = tk.StringVar()
        self.start_var = tk.StringVar(value="0:00")
        self.end_var = tk.StringVar()

        H = 32
        self.frame = ctk.CTkFrame(master, corner_radius=8, fg_color=("white", "gray17"))
        self.frame.grid_columnconfigure(1, weight=1)
        # one aligned control row...
        self.icon = ctk.CTkLabel(self.frame, text="•", width=22, height=H, font=ctk.CTkFont(size=15, weight="bold"))
        self.icon.grid(row=0, column=0, padx=(12, 2), pady=(10, 0))
        self.title_entry = ctk.CTkEntry(self.frame, textvariable=self.title_var, height=H,
                                        placeholder_text="(loads on Get info)")
        self.title_entry.grid(row=0, column=1, sticky="ew", padx=4, pady=(10, 0))
        self.start_entry = ctk.CTkEntry(self.frame, textvariable=self.start_var, width=64, height=H, placeholder_text="start")
        self.start_entry.grid(row=0, column=2, padx=2, pady=(10, 0))
        self.end_entry = ctk.CTkEntry(self.frame, textvariable=self.end_var, width=64, height=H, placeholder_text="end")
        self.end_entry.grid(row=0, column=3, padx=(2, 4), pady=(10, 0))
        self.del_btn = ctk.CTkButton(
            self.frame, text="✕", width=32, height=H, fg_color=SECONDARY, hover_color=DEL_HOVER,
            text_color=TITLE_ON, command=lambda: self.on_delete(self))
        self.del_btn.grid(row=0, column=4, padx=(2, 12), pady=(10, 0))
        # ...with the status/URL line beneath the title.
        self.info_lbl = ctk.CTkLabel(
            self.frame, text=_short(self.url), text_color=MUTED,
            font=ctk.CTkFont(size=11), anchor="w", justify="left")
        self.info_lbl.grid(row=1, column=1, columnspan=3, sticky="w", padx=6, pady=(2, 10))
        self.refresh()

    # --- state ---
    def set_info(self, info) -> None:
        self.title, self.duration = info.title, info.duration
        self.title_var.set(info.title)
        self.start_var.set("0:00")
        self.end_var.set(fmt_time(info.duration))
        self.status = "ok"
        self.refresh()

    def set_status(self, status: str) -> None:
        self.status = status
        self.refresh()

    def set_error(self, msg: str, failed: bool = False) -> None:
        self.error = (msg or "").splitlines()[0] if msg else "Couldn't load"
        self.status = "failed" if failed else "error"
        self.refresh()

    def refresh(self) -> None:
        glyph, colour = _ICON[self.status]
        self.icon.configure(text=glyph, text_color=colour)
        editable = self.status in ("ok", "done") and not self.disabled
        st = "normal" if editable else "disabled"
        for w in (self.title_entry, self.start_entry, self.end_entry):
            w.configure(state=st)
        self.del_btn.configure(state="disabled" if self.disabled else "normal")

        if self.status == "queued":
            txt, col = _short(self.url), MUTED
        elif self.status == "loading":
            txt, col = "Reading…   " + _short(self.url), MUTED
        elif self.status == "ok":
            txt, col = f"✓  {fmt_time(self.duration)}    {_short(self.url, 48)}", MUTED
        elif self.status in ("error", "failed"):
            txt, col = "✕  " + (self.error or "Couldn't load"), ERR
        elif self.status == "downloading":
            txt, col = "Downloading…", MUTED
        elif self.status == "converting":
            txt, col = "Converting…", MUTED
        else:  # done
            txt, col = "✓  Saved", DONE
        self.info_lbl.configure(text=txt, text_color=col)


class BulkView(ctk.CTkFrame):
    """Bulk UI as a full-window overlay frame (single window, no Toplevel).

    Lives inside the main window and is placed over the single-song screen;
    closing it just destroys the frame and the single screen reappears. Avoids
    the flaky macOS behaviour of withdrawing the root + opening a CTkToplevel.
    """

    def __init__(self, app) -> None:
        super().__init__(app, fg_color=WINDOW_BG, corner_radius=0)
        self.app = app  # provides .ytdlp, .deno, .ready and ._close_bulk()
        self.rows: list[BulkRow] = []
        self.busy = False
        self._indet = False
        self._alive = True
        self.q: "queue.Queue[tuple[str, object]]" = queue.Queue()

        self._build_ui()
        self.after(100, self._poll)
        self._refresh_actions()

    # ---- UI ----
    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 2))
        header.grid_columnconfigure(1, weight=1)
        ctk.CTkButton(header, text="←  One song", width=110, fg_color=SECONDARY,
                      hover_color=SECONDARY_H, text_color=TITLE_ON, command=self._close
                      ).grid(row=0, column=0, sticky="w")
        tl = ctk.CTkFrame(header, fg_color="transparent")
        tl.grid(row=0, column=1)
        ctk.CTkLabel(tl, text="Download several songs", font=ctk.CTkFont(size=20, weight="bold")).pack()
        self.clear_btn = ctk.CTkButton(header, text="Clear all", width=90, fg_color=SECONDARY,
                                        hover_color=SECONDARY_H, text_color=TITLE_ON, command=self._clear_all)
        self.clear_btn.grid(row=0, column=2, sticky="e")

        # add-links card
        add = ctk.CTkFrame(self, corner_radius=12, fg_color=CARD_BG)
        add.grid(row=1, column=0, sticky="ew", padx=18, pady=8)
        add.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(add, text="Add YouTube links", font=ctk.CTkFont(size=14, weight="bold")
                     ).grid(row=0, column=0, columnspan=2, sticky="w", padx=14, pady=(10, 0))
        rowin = ctk.CTkFrame(add, fg_color="transparent")
        rowin.grid(row=1, column=0, columnspan=2, sticky="ew", padx=14, pady=(6, 4))
        rowin.grid_columnconfigure(0, weight=1)
        self.url_var = tk.StringVar()
        self.url_entry = ctk.CTkEntry(rowin, textvariable=self.url_var, height=40,
                                      placeholder_text="https://www.youtube.com/watch?v=…")
        self.url_entry.grid(row=0, column=0, sticky="ew")
        self.url_entry.bind("<Return>", lambda _e: self._on_add())
        self.add_btn = ctk.CTkButton(rowin, text="+  Add", width=90, height=40,
                                     font=ctk.CTkFont(size=14, weight="bold"),
                                     fg_color=PINK, hover_color=PINK_HOVER, command=self._on_add)
        self.add_btn.grid(row=0, column=1, padx=(8, 0))
        ctk.CTkLabel(add, text="Paste or type a link and click Add (you can paste several at once). Repeat for each song.",
                     text_color=MUTED, font=ctk.CTkFont(size=12)
                     ).grid(row=2, column=0, columnspan=2, sticky="w", padx=14, pady=(0, 12))

        # the list
        ctk.CTkLabel(self, text="Songs to download", text_color=MUTED,
                     font=ctk.CTkFont(size=12, weight="bold"), anchor="w").grid(
            row=2, column=0, sticky="w", padx=22, pady=(2, 0))
        # Fixed height keeps the window compact no matter how many songs are
        # added — extra rows scroll. (height is widget-scaled by customtkinter.)
        self.list_frame = ctk.CTkScrollableFrame(self, fg_color=("gray94", "gray13"),
                                                 label_text="", height=220)
        self.list_frame.grid(row=3, column=0, sticky="nsew", padx=18, pady=(2, 4))
        self.list_frame.grid_columnconfigure(0, weight=1)

        # actions
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=4, column=0, sticky="ew", padx=18, pady=(8, 2))
        actions.grid_columnconfigure((0, 1), weight=1)
        self.get_btn = ctk.CTkButton(actions, text="Get info for all  →", height=44,
                                     font=ctk.CTkFont(size=15, weight="bold"),
                                     fg_color=PINK, hover_color=PINK_HOVER, command=self._get_info_all)
        self.get_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.download_btn = ctk.CTkButton(actions, text="Download all", height=44,
                                          font=ctk.CTkFont(size=15, weight="bold"),
                                          fg_color=PINK, hover_color=PINK_HOVER, command=self._download_all)
        self.download_btn.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        self.progress = ctk.CTkProgressBar(self, progress_color=PINK)
        self.progress.grid(row=5, column=0, sticky="ew", padx=18, pady=(8, 2))
        self.progress.set(0)
        self.status_var = tk.StringVar(value="Add some YouTube links to get started.")
        ctk.CTkLabel(self, textvariable=self.status_var, anchor="w", justify="left",
                     wraplength=740, text_color=MUTED).grid(row=6, column=0, sticky="ew", padx=18, pady=(0, 14))

    # ---- progress bar helpers ----
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

    def _set_action(self, btn, on: bool) -> None:
        if on:
            btn.configure(state="normal", fg_color=PINK, hover_color=PINK_HOVER, text_color="white")
        else:
            btn.configure(state="disabled", fg_color=DISABLED_BG, hover_color=DISABLED_BG, text_color=DISABLED_TX)

    # ---- adding / removing ----
    def _on_add(self) -> None:
        if self.busy:
            return
        text = self.url_var.get().strip()
        if not text:
            return
        parts = [p for p in re.split(r"\s+", text) if p]
        added = 0
        for p in parts:
            if "http" in p.lower() or "youtu" in p.lower():
                self.rows.append(BulkRow(self.list_frame, p, self._delete_row))
                added += 1
        if added == 0:  # accept whatever they typed as a single entry
            self.rows.append(BulkRow(self.list_frame, text, self._delete_row))
        self.url_var.set("")
        self.url_entry.focus_set()
        self._regrid()
        self._refresh_actions()

    def _delete_row(self, row: BulkRow) -> None:
        if self.busy:
            return
        if row in self.rows:
            self.rows.remove(row)
            row.frame.destroy()
        self._regrid()
        self._refresh_actions()

    def _clear_all(self) -> None:
        if self.busy:
            return
        for r in self.rows:
            r.frame.destroy()
        self.rows.clear()
        self._bar_set(0)
        self.status_var.set("Add some YouTube links to get started.")
        self._refresh_actions()

    def _regrid(self) -> None:
        for i, r in enumerate(self.rows):
            r.frame.grid(row=i, column=0, sticky="ew", padx=4, pady=4)

    def _refresh_actions(self) -> None:
        any_ok = any(r.status in ("ok", "done") for r in self.rows)
        n_ok = sum(1 for r in self.rows if r.status in ("ok", "done"))
        self._set_action(self.get_btn, bool(self.rows) and not self.busy)
        self._set_action(self.download_btn, any_ok and not self.busy)
        self.download_btn.configure(text=f"Download all ({n_ok})" if n_ok else "Download all")

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        st = "disabled" if busy else "normal"
        for w in (self.add_btn, self.url_entry, self.clear_btn):
            w.configure(state=st)
        for r in self.rows:
            r.disabled = busy
            r.refresh()
        self._refresh_actions()

    # ---- fetch all ----
    def _get_info_all(self) -> None:
        if self.busy or not self.app.ready:
            return
        todo = [r for r in self.rows if r.status in ("queued", "error")]
        if not todo:
            return
        self._set_busy(True)
        self._bar_indeterminate()
        self.status_var.set("Reading song details…")
        threading.Thread(target=self._fetch_worker, args=(todo,), daemon=True).start()

    def _fetch_worker(self, rows) -> None:
        for r in rows:
            self.q.put(("row_loading", r))

        def one(r):
            try:
                self.q.put(("row_ok", (r, fetch_info(self.app.ytdlp, r.url, self.app.deno))))
            except Exception as e:
                self.q.put(("row_err", (r, str(e))))

        n, done = len(rows), 0
        with ThreadPoolExecutor(max_workers=3) as ex:
            futs = [ex.submit(one, r) for r in rows]
            for _ in as_completed(futs):
                done += 1
                self.q.put(("status", f"Reading {done} of {n}…"))
        self.q.put(("fetch_done", None))

    # ---- download all ----
    def _download_all(self) -> None:
        if self.busy or not self.app.ready:
            return
        jobs, errs = [], []
        for r in self.rows:
            if r.status not in ("ok", "done"):
                continue
            try:
                start = parse_time(r.start_var.get())
                end_text = r.end_var.get().strip()
                end = parse_time(end_text) if end_text else None
            except ValueError as e:
                errs.append(f"“{r.title}”: {e}")
                continue
            if end is not None and end <= start:
                errs.append(f"“{r.title}”: end time must be after start time")
                continue
            jobs.append({"row": r, "url": r.url, "title": r.title_var.get().strip() or "audio",
                         "start": start, "end": end, "duration": r.duration})
        if errs:
            messagebox.showwarning(APP_TITLE, "Please fix these trims first:\n\n" + "\n".join(errs))
            return
        if not jobs:
            messagebox.showinfo(APP_TITLE, "Load at least one song first (Get info for all).")
            return
        folder = filedialog.askdirectory(title="Choose a folder to save all the MP3s")
        if not folder:
            return
        self._set_busy(True)
        self._bar_set(0)
        threading.Thread(target=self._download_worker, args=(jobs, Path(folder)), daemon=True).start()

    def _download_worker(self, jobs, folder: Path) -> None:
        n, ok, fail = len(jobs), 0, 0
        for i, job in enumerate(jobs, 1):
            row = job["row"]
            try:
                self.q.put(("row_status", (row, "downloading")))
                self.q.put(("status", f"Downloading {i} of {n}: {job['title']}"))
                self.q.put(("progress", 0.0))
                with tempfile.TemporaryDirectory(prefix="aubreybulk_") as tmp:
                    audio, thumb = download_audio(
                        self.app.ytdlp, job["url"], Path(tmp), deno=self.app.deno,
                        on_progress=lambda p: self.q.put(("progress", p / 100.0)))
                    self.q.put(("row_status", (row, "converting")))
                    self.q.put(("status", f"Converting {i} of {n}: {job['title']}"))
                    self.q.put(("progress", 0.0))
                    base_end = job["end"] if job["end"] is not None else job["duration"]
                    clip_total = max(0.1, base_end - job["start"])
                    dest = _unique(folder / f"{safe_filename(job['title'])}.mp3")
                    make_mp3(audio, dest, title=job["title"], start=job["start"], end=job["end"],
                             cover=thumb, total_seconds=clip_total,
                             on_progress=lambda f: self.q.put(("progress", f)))
                self.q.put(("row_status", (row, "done")))
                ok += 1
            except DownloadError as e:
                self.q.put(("row_fail", (row, str(e))))
                fail += 1
            except Exception as e:
                self.q.put(("row_fail", (row, str(e))))
                fail += 1
        self.q.put(("download_done", (ok, fail, folder)))

    # ---- queue pump ----
    def _poll(self) -> None:
        if not self._alive or not self.winfo_exists():
            return  # frame was closed; stop rescheduling
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "progress":
                    self._bar_set(float(payload))  # type: ignore[arg-type]
                elif kind == "busy_bar":
                    self._bar_indeterminate()
                elif kind == "row_loading":
                    payload.set_status("loading")  # type: ignore[union-attr]
                elif kind == "row_ok":
                    row, info = payload  # type: ignore[misc]
                    row.set_info(info)
                    self._refresh_actions()
                elif kind == "row_err":
                    row, msg = payload  # type: ignore[misc]
                    row.set_error(msg)
                elif kind == "row_status":
                    row, status = payload  # type: ignore[misc]
                    row.set_status(status)
                elif kind == "row_fail":
                    row, msg = payload  # type: ignore[misc]
                    row.set_error(msg, failed=True)
                elif kind == "fetch_done":
                    self._set_busy(False)
                    self._bar_set(0)
                    oks = sum(1 for r in self.rows if r.status == "ok")
                    self.status_var.set(
                        f"Loaded {oks} of {len(self.rows)}. Edit titles/trims if you like, then Download all.")
                elif kind == "download_done":
                    ok, fail, folder = payload  # type: ignore[misc]
                    self._set_busy(False)
                    self._bar_set(1.0 if ok else 0)
                    msg = f"Saved {ok} song{'s' if ok != 1 else ''}" + (f", {fail} failed" if fail else "") + "."
                    self.status_var.set("✓ " + msg)
                    if ok and messagebox.askyesno(APP_TITLE, msg + "\n\nOpen the folder?"):
                        open_folder(folder)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # ---- close / back to single ----
    def _close(self) -> None:
        if self.busy and not messagebox.askyesno(APP_TITLE, "A job is still running. Leave anyway?"):
            return
        self._alive = False
        self.app._close_bulk()  # the App destroys this frame + restores the single screen
