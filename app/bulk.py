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
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .audio import make_mp3, waveform
from .downloader import (DownloadError, download_audio, fetch_info, fetch_playlist,
                         has_playlist, has_single_video, looks_like_url)
from .timeline import TrimTimeline
from .main import (APP_TITLE, CARD_BG, DISABLED_BG, DISABLED_TX, DONE, MUTED,
                   PINK, PINK_HOVER, SECONDARY, SECONDARY_H, TITLE_ON, WINDOW_BG,
                   clean_url, fmt_time, open_folder, parse_time, safe_filename, symfont)

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
    """One song: URL, fetched info, per-song trim/title, and an OPTIONAL expandable
    trim+preview panel (draggable timeline + waveform + Play/Stop). The panel is
    built lazily on first expand and shares the app's single audio player."""

    def __init__(self, view, url: str, hint_title: str = "") -> None:
        self.view = view
        self.on_delete = view._delete_row
        self.url = url
        self.title = ""
        self.hint_title = hint_title  # shown before Get info (e.g. from a playlist)
        self.duration = 0.0
        self.status = "queued"
        self.error = ""
        self.disabled = False  # set True by the window while a batch runs
        self.audio = None      # cached preview audio (reused by the batch download)
        self.thumb = None
        self.expanded = False
        self.timeline = None   # built lazily on first expand
        self.detail = None
        self._syncing = False
        self._seek = 0.0       # play-from position within the trim

        self.title_var = tk.StringVar()
        self.start_var = tk.StringVar(value="0:00")
        self.end_var = tk.StringVar()

        H = 32
        self.frame = ctk.CTkFrame(view.list_frame, corner_radius=8, fg_color=("white", "gray17"))
        self.frame.grid_columnconfigure(1, weight=1)
        self.icon = ctk.CTkLabel(self.frame, text="•", width=22, height=H, font=symfont(15, "bold"))
        self.icon.grid(row=0, column=0, padx=(12, 2), pady=(10, 0))
        self.title_entry = ctk.CTkEntry(self.frame, textvariable=self.title_var, height=H,
                                        placeholder_text="(loads on Get info)")
        self.title_entry.grid(row=0, column=1, sticky="ew", padx=4, pady=(10, 0))
        self.start_entry = ctk.CTkEntry(self.frame, textvariable=self.start_var, width=58, height=H, placeholder_text="start")
        self.start_entry.grid(row=0, column=2, padx=2, pady=(10, 0))
        self.end_entry = ctk.CTkEntry(self.frame, textvariable=self.end_var, width=58, height=H, placeholder_text="end")
        self.end_entry.grid(row=0, column=3, padx=2, pady=(10, 0))
        self.expand_btn = ctk.CTkButton(
            self.frame, text="Trim  ▾", width=72, height=H, fg_color=SECONDARY, hover_color=SECONDARY_H,
            text_color=TITLE_ON, font=symfont(12), command=self._toggle)
        self.expand_btn.grid(row=0, column=4, padx=2, pady=(10, 0))
        self.del_btn = ctk.CTkButton(
            self.frame, text="✕", width=32, height=H, fg_color=SECONDARY, hover_color=DEL_HOVER,
            text_color=TITLE_ON, font=symfont(13), command=lambda: self.on_delete(self))
        self.del_btn.grid(row=0, column=5, padx=(2, 12), pady=(10, 0))
        self.info_lbl = ctk.CTkLabel(
            self.frame, text=_short(self.url), text_color=MUTED,
            font=symfont(11), anchor="w", justify="left")
        self.info_lbl.grid(row=1, column=1, columnspan=4, sticky="w", padx=6, pady=(2, 10))
        if str(self.url).startswith("http"):
            self.info_lbl.configure(cursor="hand2")
            self.info_lbl.bind("<Button-1>", lambda _e: self._open_source())
        self.start_var.trace_add("write", lambda *_: self._on_text_trim())
        self.end_var.trace_add("write", lambda *_: self._on_text_trim())
        self.refresh()

    def _open_source(self) -> None:
        try:
            if str(self.url).startswith("http"):
                webbrowser.open(self.url)
        except Exception:
            pass

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
        for w in (self.title_entry, self.start_entry, self.end_entry, self.expand_btn):
            w.configure(state=st)
        self.del_btn.configure(state="disabled" if self.disabled else "normal")
        if self.timeline is not None:
            self.timeline.set_locked(not editable)
            for b in (self.play_btn, self.stop_btn):
                b.configure(state=st)

        if self.status == "queued":
            txt, col = _short(self.hint_title or self.url), MUTED
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

    # --- expandable trim/preview panel ---
    def _toggle(self) -> None:
        if self.disabled or self.status not in ("ok", "done"):
            return
        if self.expanded:
            self._collapse()
        else:
            self._expand()

    def _expand(self) -> None:
        self.expanded = True
        self.expand_btn.configure(text="Trim  ▴")
        if self.detail is None:
            self._build_detail()
        self.detail.grid(row=2, column=0, columnspan=6, sticky="ew", padx=10, pady=(0, 10))
        self.timeline.set_duration(self.duration)
        self._push_text_to_timeline()
        self.view._row_ensure_audio(self)

    def _collapse(self) -> None:
        self.expanded = False
        self.expand_btn.configure(text="Trim  ▾")
        self.view._row_stop_if_active(self)
        if self.detail is not None:
            self.detail.grid_remove()

    def _build_detail(self) -> None:
        self.detail = ctk.CTkFrame(self.frame, fg_color="transparent")
        self.detail.grid_columnconfigure(0, weight=1)
        self.timeline = TrimTimeline(self.detail, on_change=self._tl_change,
                                     on_seek=self._tl_seek, height=62)
        self.timeline.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        tr = ctk.CTkFrame(self.detail, fg_color="transparent")
        tr.grid(row=1, column=0, sticky="w")
        self.play_btn = ctk.CTkButton(tr, text="▶  Play", width=104, height=28, fg_color=PINK,
                                      hover_color=PINK_HOVER, font=symfont(12, "bold"),
                                      command=lambda: self.view._row_play(self))
        self.play_btn.grid(row=0, column=0, padx=(0, 6))
        self.stop_btn = ctk.CTkButton(tr, text="⏮  Back", width=84, height=28, fg_color=SECONDARY,
                                      hover_color=SECONDARY_H, text_color=TITLE_ON, font=symfont(12),
                                      command=lambda: self.view._row_stop())
        self.stop_btn.grid(row=0, column=1, padx=(0, 10))
        self.prev_status_var = tk.StringVar(value="")
        ctk.CTkLabel(tr, textvariable=self.prev_status_var, text_color=MUTED,
                     font=symfont(11)).grid(row=0, column=2, sticky="w")

    def set_play_btn(self, playing: bool) -> None:
        if self.detail is not None:
            self.play_btn.configure(text="⏸  Pause" if playing else "▶  Play")

    def _tl_change(self, s: float, e: float) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            self.start_var.set(fmt_time(s))
            self.end_var.set(fmt_time(e))
        finally:
            self._syncing = False

    def _tl_seek(self, t: float) -> None:
        self.view._row_seek(self, t)

    def _on_text_trim(self) -> None:
        if self._syncing or not self.expanded or self.timeline is None:
            return
        self._push_text_to_timeline()

    def _push_text_to_timeline(self) -> None:
        if self.timeline is None or self.duration <= 0:
            return
        try:
            s = max(0.0, parse_time(self.start_var.get()))
            et = self.end_var.get().strip()
            e = parse_time(et) if et else self.duration
        except ValueError:
            return
        self.timeline.set_trim(s, e)

    def trim_bounds(self) -> tuple:
        try:
            s = max(0.0, parse_time(self.start_var.get()))
        except ValueError:
            s = 0.0
        et = self.end_var.get().strip()
        try:
            e = parse_time(et) if et else self.duration
        except ValueError:
            e = self.duration
        if self.duration > 0:
            s = min(s, self.duration)
            e = min(e or self.duration, self.duration)
        if e <= s:
            e = self.duration if self.duration > s else s + 1.0
        return s, e


class BulkView(ctk.CTkFrame):
    """Bulk UI as a full-window overlay frame (single window, no Toplevel).

    Lives inside the main window and is placed over the single-song screen;
    closing it just destroys the frame and the single screen reappears. Avoids
    the flaky macOS behaviour of withdrawing the root + opening a CTkToplevel.
    """

    def __init__(self, app) -> None:
        super().__init__(app, fg_color=WINDOW_BG, corner_radius=0)
        self.app = app  # provides .ytdlp, .deno, .ready, ._player and ._close_bulk()
        self.rows: list[BulkRow] = []
        self.busy = False
        self._indet = False
        self._alive = True
        # per-row preview state (shares the app's single audio player)
        self._row_active = None     # the BulkRow currently playing, if any
        self._row_anim = None       # after-id of the playhead poll
        self._row_end = 0.0         # stop playback here (end of that row's trim)
        self._row_fetching = set()  # ids of rows whose preview audio is downloading
        self._rowdir = Path(tempfile.mkdtemp(prefix="aubreybulkprev_"))
        self._cancel = threading.Event()   # set to stop a download batch partway
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
                      hover_color=SECONDARY_H, text_color=TITLE_ON, font=symfont(13), command=self._close
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
        ctk.CTkLabel(add, text="Add links", font=ctk.CTkFont(size=14, weight="bold")
                     ).grid(row=0, column=0, columnspan=2, sticky="w", padx=14, pady=(10, 0))
        rowin = ctk.CTkFrame(add, fg_color="transparent")
        rowin.grid(row=1, column=0, columnspan=2, sticky="ew", padx=14, pady=(6, 4))
        rowin.grid_columnconfigure(0, weight=1)
        self.url_var = tk.StringVar()
        self.url_entry = ctk.CTkEntry(rowin, textvariable=self.url_var, height=40,
                                      placeholder_text="Paste a song link, or a whole playlist link")
        self.url_entry.grid(row=0, column=0, sticky="ew")
        self.url_entry.bind("<Return>", lambda _e: self._on_add())
        self.add_btn = ctk.CTkButton(rowin, text="+  Add", width=90, height=40,
                                     font=ctk.CTkFont(size=14, weight="bold"),
                                     fg_color=PINK, hover_color=PINK_HOVER, command=self._on_add)
        self.add_btn.grid(row=0, column=1, padx=(8, 0))
        ctk.CTkLabel(add, text="Paste a link and click Add (you can paste several at once). Paste a "
                              "playlist link to add every song. Non-YouTube sites are experimental.",
                     text_color=MUTED, font=ctk.CTkFont(size=12), wraplength=720, justify="left"
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
                                     font=symfont(15, "bold"),
                                     fg_color=PINK, hover_color=PINK_HOVER, command=self._get_info_all)
        self.get_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.download_btn = ctk.CTkButton(actions, text="⬇  Download all", height=44,
                                          font=symfont(15, "bold"),
                                          fg_color=PINK, hover_color=PINK_HOVER, command=self._download_all)
        self.download_btn.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        # Shown only while a download batch runs, so it can be stopped partway.
        self.cancel_btn = ctk.CTkButton(actions, text="■  Cancel download", height=36,
                                        font=symfont(13, "bold"), fg_color=ERR, hover_color=DEL_HOVER,
                                        text_color="white", command=self._on_cancel)

        self.progress = ctk.CTkProgressBar(self, progress_color=PINK)
        self.progress.grid(row=5, column=0, sticky="ew", padx=18, pady=(8, 2))
        self.progress.set(0)
        self.status_var = tk.StringVar(value="Add some YouTube links to get started.")
        ctk.CTkLabel(self, textvariable=self.status_var, anchor="w", justify="left", font=symfont(12),
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
        try:
            self.progress.set(max(0.0, min(1.0, float(value))))
        except (TypeError, ValueError):
            self.progress.set(0)

    def _set_action(self, btn, on: bool) -> None:
        if on:
            btn.configure(state="normal", fg_color=PINK, hover_color=PINK_HOVER, text_color="white")
        else:
            btn.configure(state="disabled", fg_color=DISABLED_BG, hover_color=DISABLED_BG, text_color=DISABLED_TX)

    # ---- per-row trim preview (shares app._player; one row plays at a time) ----
    def _row_ensure_audio(self, row) -> None:
        if not self.app._player.is_available():
            if row.detail is not None:
                row.prev_status_var.set("Audio preview isn't available on this PC; trimming still works.")
            return
        if row.audio and row.audio.exists():
            threading.Thread(target=self._row_wave_worker, args=(row, str(row.audio)), daemon=True).start()
            return
        if id(row) in self._row_fetching:
            return
        self._row_fetching.add(id(row))
        if row.detail is not None:
            row.prev_status_var.set("Getting the audio to preview… (first time)")
        sub = self._rowdir / f"r{id(row)}"
        try:
            sub.mkdir(parents=True, exist_ok=True)
        except Exception:
            sub = self._rowdir
        threading.Thread(target=self._row_audio_worker, args=(row, str(sub)), daemon=True).start()

    def _row_audio_worker(self, row, workdir) -> None:
        try:
            # cancel=self._cancel so closing bulk mode (which sets it) kills this
            # yt-dlp child instead of orphaning it mid-download.
            audio, thumb = download_audio(self.app.ytdlp, row.url, Path(workdir),
                                          deno=self.app.deno, cancel=self._cancel)
            self.q.put(("row_audio", (row, str(audio), str(thumb) if thumb else "")))
        except DownloadError as e:
            if str(e) == "__CANCELLED__":
                self.q.put(("row_audio_err", (row, "cancelled")))
                return
            self.q.put(("row_audio_err", (row, str(e))))
        except Exception as e:
            self.q.put(("row_audio_err", (row, str(e))))

    def _row_wave_worker(self, row, path) -> None:
        peaks = waveform(Path(path), buckets=320)
        if peaks:
            self.q.put(("row_wave", (row, peaks)))

    def _on_row_audio(self, row, path, thumb) -> None:
        self._row_fetching.discard(id(row))
        if row not in self.rows:
            return  # the row was deleted/cleared while its audio was downloading
        row.audio = Path(path)
        row.thumb = Path(thumb) if thumb else None
        if row.expanded and row.detail is not None:
            row.prev_status_var.set("Ready. Press Play to hear your trimmed clip.")
            threading.Thread(target=self._row_wave_worker, args=(row, path), daemon=True).start()

    def _on_row_audio_err(self, row) -> None:
        self._row_fetching.discard(id(row))
        if row not in self.rows:
            return
        if row.detail is not None:
            row.prev_status_var.set("Couldn't get the audio to preview. You can still download it.")

    def _on_row_wave(self, row, peaks) -> None:
        if row not in self.rows:
            return
        if row.timeline is not None:
            try:
                row.timeline.set_waveform(peaks)
            except Exception:
                pass

    def _row_play(self, row) -> None:
        if self.busy or row.status not in ("ok", "done"):
            return
        if not (row.audio and row.audio.exists()):
            if row.detail is not None:
                row.prev_status_var.set("Getting the audio ready, then press Play…")
            self._row_ensure_audio(row)
            return
        p = self.app._player
        if self._row_active is row and p.is_playing():
            p.pause()
            self._cancel_row_tick()
            row.set_play_btn(False)
            row.prev_status_var.set("Paused. Press Play to continue.")
            return
        if self._row_active is row and p.is_paused():
            p.resume()
            row.set_play_btn(True)
            row.prev_status_var.set("Playing…")
            self._row_tick()
            return
        self._row_start(row, row._seek)

    def _row_start(self, row, pos) -> None:
        prev = self._row_active
        if prev is not None and prev is not row:
            prev.set_play_btn(False)
            if prev.detail is not None:
                prev.prev_status_var.set("")
        self._cancel_row_tick()
        p = self.app._player
        try:
            p.load(row.audio, row.duration)
        except Exception:
            pass
        s, e = row.trim_bounds()
        pos = min(max(pos, s), max(s, e - 0.05))
        self._row_end = e
        if p.play(pos):
            self._row_active = row
            row._seek = pos
            row.set_play_btn(True)
            row.prev_status_var.set("Playing your trimmed clip. Click the bar to jump, or Back.")
            self._row_tick()
        else:
            row.prev_status_var.set("Couldn't play the preview on this PC.")

    def _row_stop(self) -> None:
        """Stop and rewind to the START of the active row's trimmed section."""
        try:
            self.app._player.stop()
        except Exception:
            pass
        self._cancel_row_tick()
        r, self._row_active = self._row_active, None
        if r is not None:
            s, _ = r.trim_bounds()
            r._seek = s
            try:
                if r.timeline is not None:
                    r.timeline.set_playhead(s)
            except Exception:
                pass
            r.set_play_btn(False)

    def _row_stop_if_active(self, row) -> None:
        if self._row_active is row:
            self._row_stop()

    def _row_seek(self, row, t) -> None:
        if row.status not in ("ok", "done"):
            return
        s, e = row.trim_bounds()
        t = min(max(float(t), s), max(s, e - 0.05))
        row._seek = t
        try:
            if row.timeline is not None:
                row.timeline.set_playhead(t)
        except Exception:
            pass
        p = self.app._player
        if self._row_active is row and (p.is_playing() or p.is_paused()):
            self._row_start(row, t)

    def _cancel_row_tick(self) -> None:
        if self._row_anim is not None:
            try:
                self.after_cancel(self._row_anim)
            except Exception:
                pass
            self._row_anim = None

    def _row_tick(self) -> None:
        r = self._row_active
        if r is None or not self._alive or not self.winfo_exists():
            self._row_anim = None
            return
        p = self.app._player
        pos = p.position()
        # only ever preview the trimmed section: stop at the end and rewind
        if p.has_ended() or (p.is_playing() and self._row_end and pos >= self._row_end - 0.03):
            self._row_stop()
            if r.detail is not None:
                r.prev_status_var.set("That's the clip. Press Play to hear it again.")
            return
        try:
            if r.timeline is not None:
                r.timeline.set_playhead(pos)
        except Exception:
            pass
        if p.is_active():
            self._row_anim = self.after(60, self._row_tick)
        else:
            self._row_anim = None
            r.set_play_btn(False)

    # ---- adding / removing ----
    def _on_add(self) -> None:
        if self.busy:
            return
        text = self.url_var.get().strip()
        if not text:
            return
        parts = [p for p in re.split(r"\s+", text) if p]
        # A single playlist link (not a specific video) -> expand into many rows.
        if len(parts) == 1 and has_playlist(parts[0]) and not has_single_video(parts[0]):
            self.url_var.set("")
            self._expand_playlist(parts[0])
            return
        added = 0
        for p in parts:
            if looks_like_url(p):
                self.rows.append(BulkRow(self, clean_url(p)))
                added += 1
        if added == 0:
            # Don't add a row that could only ever fail — guide instead.
            self.status_var.set("Please paste a web link (it should start with http). "
                                "To search by name, use the one-song screen.")
            return
        self.url_var.set("")
        self.url_entry.focus_set()
        self._regrid()
        self._refresh_actions()

    # ---- playlist import ----
    def import_playlist(self, url: str) -> None:
        """Public entry point: expand ``url`` (called when opened from a playlist link)."""
        self._expand_playlist(url)

    def _expand_playlist(self, url: str) -> None:
        if self.busy or not self.app.ready:
            return
        self._set_busy(True)
        self._bar_indeterminate()
        self.status_var.set("Reading the playlist…")
        threading.Thread(target=self._expand_worker, args=(url,), daemon=True).start()

    def _expand_worker(self, url: str) -> None:
        try:
            pl = fetch_playlist(self.app.ytdlp, url, self.app.deno, limit=60)
            self.q.put(("playlist_rows", pl))
        except DownloadError as e:
            self.q.put(("playlist_err", str(e)))
        except Exception as e:
            self.q.put(("playlist_err", str(e)))

    def _on_playlist_rows(self, pl) -> None:
        self._set_busy(False)
        self._bar_set(0)
        for u, title in pl.entries:
            self.rows.append(BulkRow(self, u, hint_title=title))
        self._regrid()
        self._refresh_actions()
        note = f"Added {len(pl.entries)} songs from “{pl.title}”."
        if pl.truncated:
            note += f" (the first {len(pl.entries)} of {pl.total})"
        self.status_var.set(note + " Loading details…")
        self._get_info_all()  # auto-fetch metadata so the user can edit straight away

    def _delete_row(self, row: BulkRow) -> None:
        if self.busy:
            return
        self._row_stop_if_active(row)
        self._row_fetching.discard(id(row))  # don't track a row that's gone
        if row in self.rows:
            self.rows.remove(row)
            row.frame.destroy()
        self._regrid()
        self._refresh_actions()

    def _clear_all(self) -> None:
        if self.busy:
            return
        self._row_stop()
        for r in self.rows:
            self._row_fetching.discard(id(r))
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
        self.download_btn.configure(text=f"⬇  Download all ({n_ok})" if n_ok else "⬇  Download all")

    def _set_busy(self, busy: bool) -> None:
        if busy:
            self._row_stop()  # stop any preview before a batch run takes over
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
        self._bar_set(0)  # determinate: fill as each song's info comes in
        self.status_var.set("Reading song details...")
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
                self.q.put(("progress", done / n))  # real overall progress
                self.q.put(("status", f"Reading {done} of {n}..."))
        self.q.put(("fetch_done", None))

    # ---- download all ----
    def _download_all(self) -> None:
        if self.busy or not self.app.ready:
            return
        # Stop any preview FIRST, so the audio device is torn down before the native
        # folder picker opens — an open device + the modal picker can hang on Parallels.
        self._row_stop()
        jobs, errs = [], []
        for r in self.rows:
            if r.status not in ("ok", "done"):
                continue
            label = r.title_var.get().strip() or r.title or "this song"
            try:
                start = parse_time(r.start_var.get())
                end_text = r.end_var.get().strip()
                end = parse_time(end_text) if end_text else None
            except ValueError as e:
                errs.append(f"“{label}”: {e}")
                continue
            if end is not None and end <= start:
                errs.append(f"“{label}”: end time must be after start time")
                continue
            # Clamp to the known length so a too-large value can't make an empty clip.
            start = max(0.0, start)
            if r.duration and r.duration > 0:
                start = min(start, max(0.0, r.duration - 0.1))
                if end is not None:
                    end = min(end, r.duration)
                    if end <= start:
                        end = None
            cached = r.audio if (r.audio and r.audio.exists()) else None
            jobs.append({"row": r, "url": r.url, "title": r.title_var.get().strip() or "audio",
                         "start": start, "end": end, "duration": r.duration,
                         "audio": cached, "thumb": (r.thumb if cached else None)})
        if errs:
            messagebox.showwarning(APP_TITLE, "Please fix these trims first:\n\n" + "\n".join(errs))
            return
        if not jobs:
            messagebox.showinfo(APP_TITLE, "Load at least one song first (Get info for all).")
            return
        # initialdir keeps the native folder picker from enumerating the shell root
        # (incl. Parallels \\Mac network shares), which can hang the UI on a VM.
        folder = filedialog.askdirectory(
            title="Choose a folder to save all the MP3s", initialdir=self.app.default_dir())
        if not folder:
            return
        self.app._last_dir = folder or self.app._last_dir
        self._cancel.clear()
        self.cancel_btn.configure(state="normal", text="■  Cancel download", fg_color=ERR)
        self.cancel_btn.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self._set_busy(True)
        self._bar_set(0)
        threading.Thread(target=self._download_worker, args=(jobs, Path(folder)), daemon=True).start()

    def _on_cancel(self) -> None:
        self._cancel.set()
        self.cancel_btn.configure(state="disabled", text="Stopping…")
        self.status_var.set("Stopping after the current song…")

    def _download_worker(self, jobs, folder: Path) -> None:
        n, ok, fail, cancelled = len(jobs), 0, 0, False
        for i, job in enumerate(jobs, 1):
            if self._cancel.is_set():
                cancelled = True
                break
            row = job["row"]
            # Overall batch progress: songs already finished, plus this song's own
            # fraction (download = first 85%, convert = last 15% of one song).
            def overall(frac, _i=i):
                return ((_i - 1) + max(0.0, min(1.0, frac))) / n
            try:
                base_end = job["end"] if job["end"] is not None else job["duration"]
                clip_total = (max(0.1, base_end - job["start"])
                              if base_end and base_end > job["start"] else None)
                cached = job.get("audio")

                def _convert(audio, thumb):
                    self.q.put(("row_status", (row, "converting")))
                    self.q.put(("status", f"Converting {i} of {n}: {job['title']}"))
                    self.q.put(("progress", overall(0.85)))
                    dest = _unique(folder / f"{safe_filename(job['title'])}.mp3")
                    make_mp3(audio, dest, title=job["title"], start=job["start"], end=job["end"],
                             cover=thumb, total_seconds=clip_total, cancel=self._cancel,
                             on_progress=lambda f: self.q.put(("progress", overall(0.85 + 0.15 * f))))

                if cached and Path(cached).exists():
                    # reuse the audio already pulled for this row's trim preview
                    self.q.put(("row_status", (row, "converting")))
                    self.q.put(("progress", overall(0.85)))
                    _convert(Path(cached), job.get("thumb"))
                else:
                    self.q.put(("row_status", (row, "downloading")))
                    self.q.put(("status", f"Downloading {i} of {n}: {job['title']}"))
                    self.q.put(("progress", overall(0.0)))
                    with tempfile.TemporaryDirectory(prefix="aubreybulk_") as tmp:
                        audio, thumb = download_audio(
                            self.app.ytdlp, job["url"], Path(tmp), deno=self.app.deno, cancel=self._cancel,
                            on_progress=lambda p: self.q.put(("progress", overall(0.85 * (p / 100.0)))))
                        _convert(audio, thumb)
                self.q.put(("progress", overall(1.0)))
                self.q.put(("row_status", (row, "done")))
                ok += 1
            except DownloadError as e:
                if str(e) == "__CANCELLED__" or self._cancel.is_set():
                    cancelled = True
                    self.q.put(("row_status", (row, "ok")))  # revert to a usable state
                    break
                self.q.put(("row_fail", (row, str(e))))
                fail += 1
            except Exception as e:
                self.q.put(("row_fail", (row, str(e))))
                fail += 1
        self.q.put(("download_done", (ok, fail, folder, cancelled)))

    # ---- queue pump ----
    def _poll(self) -> None:
        if not self._alive or not self.winfo_exists():
            return  # frame was closed; stop rescheduling
        _last_prog = None  # coalesce a flood of progress msgs into ONE redraw per tick
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "progress":
                    _last_prog = float(payload)  # type: ignore[arg-type]
                    continue
                elif kind == "busy_bar":
                    _last_prog = None  # indeterminate supersedes any pending progress
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
                elif kind == "row_audio":
                    row, path, thumb = payload  # type: ignore[misc]
                    self._on_row_audio(row, path, thumb)
                elif kind == "row_audio_err":
                    row, _msg = payload  # type: ignore[misc]
                    self._on_row_audio_err(row)
                elif kind == "row_wave":
                    row, peaks = payload  # type: ignore[misc]
                    self._on_row_wave(row, peaks)
                elif kind == "playlist_rows":
                    self._on_playlist_rows(payload)  # type: ignore[arg-type]
                elif kind == "playlist_err":
                    self._set_busy(False)
                    self._bar_set(0)
                    self.status_var.set("Couldn't read that playlist.")
                    messagebox.showerror(APP_TITLE, str(payload))
                elif kind == "fetch_done":
                    self._set_busy(False)
                    self._bar_set(0)
                    oks = sum(1 for r in self.rows if r.status == "ok")
                    self.status_var.set(
                        f"Loaded {oks} of {len(self.rows)}. Edit titles/trims if you like, then Download all.")
                elif kind == "download_done":
                    ok, fail, folder, cancelled = payload  # type: ignore[misc]
                    self.cancel_btn.grid_remove()
                    self._set_busy(False)
                    self._bar_set(1.0 if (ok and not cancelled) else 0)
                    msg = f"Saved {ok} song{'s' if ok != 1 else ''}" + (f", {fail} failed" if fail else "") + "."
                    self.status_var.set(("Stopped. " if cancelled else "Done. ") + msg)
                    if ok and messagebox.askyesno(APP_TITLE, msg + "\n\nOpen the folder?"):
                        open_folder(folder)
        except queue.Empty:
            pass
        except Exception:
            # never let one bad UI update kill the pump (it would freeze bulk mode)
            try:
                self.status_var.set("Something went wrong. Please try again.")
            except Exception:
                pass
        finally:
            if _last_prog is not None:
                try:
                    self._bar_set(_last_prog)   # one redraw per tick, not per message
                except Exception:
                    pass
            if self._alive and self.winfo_exists():
                self.after(100, self._poll)

    # ---- close / back to single ----
    def _close(self) -> None:
        if self.busy and not messagebox.askyesno(APP_TITLE, "A job is still running. Leave anyway?"):
            return
        self._alive = False
        # Kill any in-flight batch/preview download (proc.kill_tree) so we don't
        # orphan a yt-dlp/ffmpeg child or delete _rowdir out from under it.
        self._cancel.set()
        try:
            self._row_stop()
        except Exception:
            pass
        try:
            import shutil
            shutil.rmtree(self._rowdir, ignore_errors=True)
        except Exception:
            pass
        self.app._close_bulk()  # the App destroys this frame + restores the single screen
