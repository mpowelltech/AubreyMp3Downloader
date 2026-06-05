# Aubrey's YT-MP3 Downloader

A small, friendly Windows app that turns YouTube songs into MP3s for importing
into the [Yoto](https://yotoplay.com) player. Paste a link, trim it if you want,
edit the title, and save. No installing, no command line. It can do one song at
a time or a whole batch at once.

> Built by Matt for my favourite niece.

---

## For the person using it (Windows)

1. Double-click **`Aubreys-YT-MP3-Downloader.exe`**.
   * The first time, Windows may show a blue **"Windows protected your PC"** box.
     Click **More info**, then **Run anyway**. This is normal for small apps that
     aren't signed by a big company, and it's safe.
   * The very first launch quietly downloads a couple of small helpers in the
     background, so give it a few seconds and make sure you're online.

### One song
1. **Paste** a YouTube link into the box.
2. Click **Get info**. The title and length fill in.
3. (Optional) Trim it. Set a **Start** / **End** time, or type seconds into
   **Skip first** / **Skip last**. The two ways stay in sync, so use whichever is
   easier.
4. (Optional) Edit the **Title**. This is what shows under the track in Yoto.
5. Click **Download MP3** and choose where to save it.
6. To do another, click **New video**.

### Several songs at once
1. Click **Download multiple**.
2. Paste or type a link and click **+ Add** (you can paste several at once).
   Repeat for each song. Use the **✕** to remove any.
3. Click **Get info for all**. Each row loads its title and length; any that
   can't load are marked and simply skipped.
4. Edit titles and trims per row if you like.
5. Click **Download all** and pick **one folder**. Every song saves there.

### Putting them on Yoto
Upload the MP3s to a Make Your Own card at
[my.yotoplay.com/my-cards](https://my.yotoplay.com/my-cards) or in the Yoto app.

> Yoto limits: up to 100 tracks per card, max 100 MB / 60 minutes per track.

---

## For developers

Under the hood: a Python + customtkinter GUI that drives **yt-dlp** (downloaded
and auto-updated at runtime) and a bundled **ffmpeg** for converting, trimming,
and tagging. YouTube now needs a JavaScript runtime to play many videos, so the
app also fetches **Deno** on first run (see [CLAUDE.md](CLAUDE.md) for the why).

### Run from source

Needs Python 3.12+, plus `yt-dlp`, `ffmpeg` and `deno` on your PATH
(`brew install yt-dlp ffmpeg deno`).

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt "yt-dlp[default]"
python run.py
```

On macOS you can also just double-click **`scripts/run_mac.command`**, which runs
the app from source using the project's venv. Handy for quick UI testing without
building an exe.

### Smoke-test the pipeline (no GUI)

```bash
python scripts/smoke.py            # downloads a short clip, trims it, writes an MP3
```

### Build the Windows .exe

You can't build a Windows exe on a Mac (PyInstaller doesn't cross-compile). Two
options:

* **GitHub Actions (recommended).** Push the repo, open the **Actions** tab, run
  **"Build Windows EXE"**, and download the exe from the run's **Artifacts**.
  Tagging a release (`git tag v1.0.0 && git push --tags`) also attaches the exe
  to a GitHub Release. Works on a free private repo.
* **Local Windows (or a VM).** Run **`scripts/build_windows.bat`** inside Windows.
  It installs an x64 Python if needed, fetches ffmpeg, builds the exe with
  PyInstaller, and drops it in `_winbuild_out\`. Heavy inputs (ffmpeg, the venv)
  are cached, so repeat builds are fast.

  Note: on an Apple-Silicon Parallels VM, Windows is ARM64. The script builds a
  proper **x64** exe (so it runs on a normal PC) by using x64 Python under
  emulation.

### Notes

* The bundled ffmpeg is a GPL static build (BtbN). Distribute accordingly.
* For personal / family use with content you're allowed to download.
