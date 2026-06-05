# CLAUDE.md

Guidance for Claude Code (and humans) working in this repo.

## What this is

**Aubrey's YT-MP3 Downloader** — a single-file Windows GUI app so a
non-technical user can paste a YouTube link, trim it, and export an MP3 to
import into a Yoto player. Everything is self-contained: the user installs
nothing and never touches a terminal.

Author works on a **Mac**; the **target is Windows**. The download→trim→MP3
pipeline is fully testable on the Mac; only producing the `.exe` needs Windows.

## Architecture

```
run.py                 # entry point -> app.main:main
app/
  paths.py             # dev-vs-frozen resource paths; ffmpeg/cache locations
  updater.py           # ensure + auto-update yt-dlp AND ensure deno (Windows
                       #   downloads yt-dlp.exe + deno.exe; dev uses PATH)
  downloader.py        # shells out to yt-dlp: fetch_info() + download_audio()
  audio.py             # shells out to ffmpeg: make_mp3() — trim, encode, tag, cover
  main.py              # single-song GUI; state machine; workers -> queue -> Tk loop
  bulk.py              # "Download multiple" in-window overlay: add a list, fetch all, download all
scripts/smoke.py       # headless pipeline test (no GUI)
scripts/run_mac.command # run from source on macOS (no exe build) for quick UI testing
AubreyMp3.spec         # PyInstaller --onefile spec
.github/workflows/build.yml  # Windows runner: fetch ffmpeg -> pyinstaller -> artifact
```

Data flow: GUI worker thread → `fetch_info`/`download_audio` (yt-dlp) →
`make_mp3` (ffmpeg) → file. Workers communicate with the UI only via
`self.q` (a `queue.Queue`) drained by `_poll()` on the main thread — **never
touch Tk widgets from a worker thread.**

## Key design decisions (locked with the author)

- **yt-dlp is never frozen into the exe.** YouTube breaks frozen copies fast.
  Windows downloads `yt-dlp.exe` to `%LOCALAPPDATA%\AubreysYT-MP3-Downloader\`
  on first run and runs `--update` in the background each launch. We shell out
  to the binary (not the Python module) precisely so it can self-update.
- **ffmpeg IS bundled** into the exe (stable, no auto-update needed). Passed to
  yt-dlp via `--ffmpeg-location` and invoked directly by `audio.py`.
- **Deno is required, not optional.** YouTube's JS "n" challenge means yt-dlp
  needs a JS runtime or many videos fail with "This video is not available".
  We download `deno.exe` once (like yt-dlp) and pass `--js-runtimes deno:<path>`
  plus `--remote-components ejs:github` (downloader `_engine_args`). The official
  yt-dlp.exe bundles the EJS solver scripts; deno is the missing piece.
- **Trim UX:** Start/End fields (mm:ss) are the source of truth; the "Skip
  first/last" buttons just fill those fields. ffmpeg does the actual cut
  (`-ss` input seek + `-t` output duration), re-encoding so it's accurate.
- **Output:** user edits the title and picks the folder on every export
  (Save-As dialog). Title goes into the ID3 tag; thumbnail → cover art.
- **Build:** GitHub Actions Windows runner (free, private-repo friendly).
  PyInstaller cannot cross-compile from macOS.
- **Packaging hygiene:** `--onefile`, `console=False`, **UPX off** (UPX worsens
  antivirus false positives). Unsigned exe ⇒ one-time SmartScreen prompt.
- **Robust first launch:** a PyInstaller `Splash` (assets/splash.png) covers the
  one-file unpack; our HTTPS downloads verify against **certifi** (Windows OpenSSL
  won't fetch missing roots on demand, which caused CERTIFICATE_VERIFY_FAILED on
  fresh PCs); ffmpeg presence is verified at startup; and any setup failure drops
  to a locked `"failed"` state with a Retry prompt — the app refuses to proceed
  without its tools (`_verify_ffmpeg`, `_on_setup_error`, `_start_engine`).
- **HiDPI sizing:** customtkinter's `CTk.geometry("WxH")` multiplies W/H by the
  display DPI factor, so hardcoded sizes oversize on HiDPI Windows. Instead,
  `App._fit_window()` measures content via `winfo_reqheight()` (already physical
  px) and writes RAW `tk.Tk.geometry(self, ...)` (bypassing the CTk multiplier),
  clamped to the screen. The bulk list (`CTkScrollableFrame`, fixed `height`)
  scrolls so the window stays compact regardless of song count. Don't hardcode
  window geometries; call `_fit_window`. (Edge: moving across monitors of
  different DPI won't auto-refit — fine for a single-screen target.)
- **Self-replacing updater:** the swap script retries `move /Y` until the old
  exe's lock releases (a one-file app is a *child* of the bootloader, which holds
  the .exe briefly after exit) and runs hidden via `CREATE_NO_WINDOW`. A given
  build's updater only fixes updates made *from* it onward.
- **App self-update:** on startup the frozen Windows exe compares the GitHub
  `releases/latest` tag to `__version__`; if newer it prompts, downloads the new
  exe beside the current one, and a detached `apply_update.bat` waits for this
  process to exit, swaps the exe in place, and relaunches
  (`updater.check_for_app_update` / `download_and_relaunch`).

## Bulk mode (app/bulk.py)

`BulkView` is a `CTkFrame` (NOT a Toplevel) opened from the single screen's
"Download multiple" button. `App._on_several` enlarges the window, creates the
frame, and `place(relwidth=1, relheight=1)` + `tkraise()` overlays it over the
single screen; `App._close_bulk` destroys it and restores the geometry. This
single-window approach avoids the flaky macOS behaviour of withdrawing the root
and opening a CTkToplevel (which often wouldn't come forward). `BulkView._poll`
stops rescheduling once the frame is destroyed (`winfo_exists`). Reuses the same
engine (`app.ytdlp` / `app.deno`) and the same per-song pipeline. Flow: add links (one
`BulkRow` each) → "Get info for all" (ThreadPoolExecutor, 3 workers, metadata
only) → edit title/Start/End per row → "Download all" (sequential; pick one
folder; per-row status + overall progress). Same gating philosophy: actions
light up only when usable, everything locks during a run, failed rows are
skipped, trim is validated on the main thread before the worker starts (workers
never touch Tk vars — values are snapshotted into plain dicts first).

## Dev workflow

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # customtkinter only
python scripts/smoke.py                  # validate pipeline (needs yt-dlp+ffmpeg on PATH)
python run.py                            # launch the GUI
```

Requires `yt-dlp` and `ffmpeg` on PATH for dev (`brew install yt-dlp ffmpeg`).

## Conventions / gotchas

- `paths.py` is the single place that branches on `is_frozen()` /
  `sys.platform`. New bundled resources go through `resource_path()`.
- All subprocess calls pass `creationflags=CREATE_NO_WINDOW` on Windows to
  avoid console flashes — keep that when adding new shell-outs.
- User-facing errors are raised as `DownloadError` with plain-language text;
  `downloader._friendly()` maps yt-dlp stderr to friendly messages — extend it
  rather than surfacing raw stderr.
- Don't add `yt-dlp` to `requirements.txt` — it's intentionally runtime-managed.
- The exe filename is `Aubreys-YT-MP3-Downloader` (no apostrophe/spaces, to keep
  paths clean); the window title keeps the apostrophe (`APP_TITLE`).
