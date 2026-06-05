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
  main.py              # customtkinter GUI; workers -> queue -> Tk main loop
scripts/smoke.py       # headless pipeline test (no GUI)
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
