# CLAUDE.md

Guidance for Claude Code (and humans) working in this repo.

## What this is

**Aubrey's YT-MP3 Downloader** — a single-file Windows GUI app so a
non-technical user can grab a song (paste a link **or search by name**), trim
it, and export an MP3 to import into a Yoto player. Everything is
self-contained: the user installs nothing and never touches a terminal. As of
v1.1.0 it also imports whole playlists, accepts non-YouTube links
(experimental), and can play back the trim points before saving (experimental).

Author works on a **Mac**; the **target is Windows**. The download→trim→MP3
pipeline is fully testable on the Mac; only producing the `.exe` needs Windows.

## Architecture

```
run.py                 # entry point -> app.main:main
app/
  paths.py             # dev-vs-frozen resource paths; ffmpeg/cache locations
  updater.py           # ensure + auto-update yt-dlp AND ensure deno (Windows
                       #   downloads yt-dlp.exe + deno.exe; dev uses PATH)
  downloader.py        # shells out to yt-dlp: fetch_info(), download_audio(),
                       #   search() (ytsearch), fetch_playlist() (flat), URL helpers
  audio.py             # shells out to ffmpeg: make_mp3() (trim/encode/tag/cover)
                       #   + extract_preview() (short WAV snippet for the trim preview)
  media.py             # best-effort thumbnail fetch (certifi) for the song preview
  player.py            # Player: streams ffmpeg-decoded PCM to a miniaudio device
                       #   (play/stop/seek/position); best-effort, lazy-imported
  timeline.py          # TrimTimeline: draggable Canvas trim bar (handles/waveform/
                       #   playhead); on_change=trim, on_seek=set play position
  main.py              # single-song GUI; ACCORDION of 3 expandable steps; state
                       #   machine; workers -> queue -> Tk loop; search/chooser overlay;
                       #   mini player (Play/Stop/seek) over the cached audio
  bulk.py              # "Download several" in-window overlay: add a list/playlist, fetch all, download all
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
- **Trim UX:** Start/End fields (mm:ss) are the precise source of truth; a
  draggable `TrimTimeline` (timeline.py) mirrors them (drag a handle -> updates
  the text; type -> moves the handle). A mini player (Play/Stop) streams the whole
  song; clicking the waveform body (not a handle) seeks; a playhead tracks the
  real playback position. ffmpeg does the actual cut (`-ss` input seek + `-t`
  output duration), re-encoding so it's accurate. (v1.2.0 dropped "Skip
  first/last"; v1.3.0 replaced the snippet "Hear start/end" with the player.)
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
- **HiDPI sizing:** customtkinter's `CTk.geometry("WxH")` takes LOGICAL pixels
  and multiplies them by the display DPI factor → physical px. So pass logical
  sizes (800x670 single, 800x680 bulk) and it scales correctly on HiDPI. Do NOT
  measure `winfo_reqheight()` at `__init__` and raw-set it: before the window
  maps to a monitor the DPI is unknown, so reqheight comes back LOGICAL and the
  window ends up ~1/DPI too small (this caused a regression). Keep the bulk songs
  list a fixed-height `CTkScrollableFrame` so its content stays compact (scrolls)
  regardless of song count — that's what fixes the "bulk too tall" case.
- **Self-replacing updater:** the swap script retries `move /Y` until the old
  exe's lock releases (a one-file app is a *child* of the bootloader, which holds
  the .exe briefly after exit) and runs hidden via `CREATE_NO_WINDOW`. Before
  relaunching it clears **every** PyInstaller one-file env var (the whole `_PYI*`
  family **and** legacy `_MEIPASS2`) — PyInstaller 6.x renamed `_MEIPASS2` to
  `_PYI_*` (e.g. `_PYI_APPLICATION_HOME_DIR`), and an inherited one makes the new
  exe reuse the deleted temp dir → "Failed to load Python DLL". A given build's
  updater only fixes updates made *from* it onward (so the first update onto a
  fixed build can still show the old error once).
- **App self-update:** on startup the frozen Windows exe compares the GitHub
  `releases/latest` tag to `__version__`; if newer it prompts, downloads the new
  exe beside the current one, and a detached `apply_update.bat` waits for this
  process to exit, swaps the exe in place, and relaunches
  (`updater.check_for_app_update` / `download_and_relaunch`).

## v1.1.0 features (locked with the author)

- **Search by name (step 1 toggle).** A `CTkSegmentedButton` switches step 1
  between "Paste a link" and "Search by name". Search runs `yt-dlp -J
  --flat-playlist "ytsearchN:<query>"` (`downloader.search`, metadata-only, fast,
  live results dropped) and shows hits in a `ChooserView` overlay (same
  place/tkraise pattern as bulk). Picking one routes through the normal
  `_load_url` → `fetch_info` path, so everything downstream is unchanged. A URL
  typed in search mode is auto-routed to the link path; non-URL text in link mode
  nudges the user to the search tab. `looks_like_url` / `clean_url` classify input.
- **Playlist import.** `downloader.fetch_playlist` expands a playlist/mix link via
  `--flat-playlist --playlist-end 60` (cap so an endless "Mix" can't flood),
  skipping `[Private/Deleted]` and live entries and only keeping real http(s)
  URLs (`_entry_url`). In bulk, pasting a *pure* playlist link auto-expands into
  rows then auto-runs "Get info for all"; in single mode a playlist link prompts
  to open bulk. A `__PLAYLIST__` sentinel from `fetch_info` is the safety net if a
  playlist slips through.
- **Beyond YouTube (experimental).** yt-dlp already supports ~all sites; we just
  opened up the copy/placeholders and broadened URL acceptance. `VideoInfo.source`
  drives a "from <Site> (experimental)" label for non-YouTube. No code path is
  YouTube-specific except the JS-challenge engine args (harmless elsewhere).
- **Accordion (v1.3.0).** The single screen is three stacked expandable sections
  (Find your song / Check the song / Trim & preview), only one body open at a time
  (`_acc_section`/`_expand`/`_acc_click`). Section 1 is reachable only while
  `empty`; 2 & 3 once a song is loaded (use Start over to go back to 1). Loading a
  song auto-expands 2; a "Next: Trim" button opens 3. This is more compact than the
  old side-by-side and gives the title one full-width line + a wide waveform.
- **Mini player + audio prefetch (experimental, v1.3.0).** As soon as a song loads
  we **prefetch** the full bestaudio in the background into a per-song subdir of
  `_preview_dir` (`_ensure_audio` -> `_prefetch_worker` -> `audio_ready`), **cache
  it**, compute the waveform (`audio.waveform`), and `Player.load` it. The player
  (player.py) streams ffmpeg-decoded PCM to a miniaudio device: Play/Stop, click
  the waveform to seek (`on_seek` -> `Player.play(t)`), and a playhead driven by
  `Player.position()` polled in the Tk loop (`_tick_playhead`; workers never touch
  Tk). The **export reuses the cached audio**, so the ~9 s yt-dlp download happens
  **once per song**, in the background, overlapping trimming (yt-dlp also caches
  the JS-challenge solver, so it isn't a second "captcha"). Player is BEST-EFFORT
  and lazy-imported: if miniaudio/the device is unavailable the transport hides and
  the app/download are unaffected. `_cache_url` ties cached audio to its video;
  `_clear_cache` (Start over / new load) and `destroy()` remove the temp subdirs;
  each prefetch gets its OWN subdir so a still-finishing previous download can't
  collide on the `audio.<ext>` name. miniaudio is bundled via `collect_all` in the
  spec (one cffi `.pyd`).
- **Unbreakable hardening.** `_poll` (both screens) survives any handler
  exception and always reschedules in `finally` — a single UI bug can never freeze
  the app. `parse_time` rejects `inf`/`nan`. `_validate_trim` blocks Download on
  bad/over-length trims (inline red hint) and times are clamped to the real length
  before ffmpeg. Progress bars clamp to [0,1] and go indeterminate when the length
  is unknown (no fake/stuck percentage). Self-update is suppressed while an overlay
  or preview is mid-flight. `_friendly` never surfaces a raw traceback.

## Bulk mode (app/bulk.py)

`BulkView` is a `CTkFrame` (NOT a Toplevel) opened from the single screen's
"Download several" button (or automatically when a playlist link is detected).
`App._on_several` enlarges the window, creates the
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
