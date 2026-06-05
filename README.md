# Aubrey's YT-MP3 Downloader

A tiny Windows app that turns a YouTube link into an MP3 — for importing songs
into the [Yoto](https://yotoplay.com) player app. Paste a link, trim the start
and end if you want, edit the title, and save. No installing, no command line.

---

## For the person using it (Windows)

1. Double-click **`Aubreys-YT-MP3-Downloader.exe`**.
   - The first time, Windows may show a blue **"Windows protected your PC"**
     box. Click **More info → Run anyway**. (This is normal for small apps that
     aren't signed by a big company — it's safe.)
   - The very first launch downloads a small helper in the background, so give
     it a few seconds and make sure you're online.
2. **Paste** a YouTube link into the box.
3. Click **Fetch info** — the title and length fill in.
4. (Optional) Set a **Start** / **End** time, or use **Skip first / Skip last**
   to chop off an intro or outro.
5. (Optional) Edit the **Title** — this is what shows up in the Yoto app.
6. Click **Download MP3** and choose where to save it.
7. Upload the MP3 to a Make Your Own card at
   [my.yotoplay.com/my-cards](https://my.yotoplay.com/my-cards) or in the Yoto app.

> Yoto limits: up to 100 tracks per card, max 100 MB / 60 minutes per track.

---

## For developers

### Run from source (Mac/Windows/Linux)

Needs Python 3.12+, plus `yt-dlp` and `ffmpeg` on your PATH (e.g.
`brew install yt-dlp ffmpeg`).

```bash
python -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

In dev the app uses the `yt-dlp`/`ffmpeg` already on your PATH. In a packaged
Windows build it bundles `ffmpeg.exe` and downloads/auto-updates `yt-dlp.exe`
into `%LOCALAPPDATA%\AubreysYT-MP3-Downloader\`.

### Smoke-test the pipeline (no GUI)

```bash
python scripts/smoke.py            # downloads a ~19s clip, trims 2–10s -> smoke_out.mp3
```

### Build the Windows .exe

You **cannot** build a Windows exe on a Mac (PyInstaller doesn't cross-compile).
Two options:

- **GitHub Actions (recommended):** push the repo, open the **Actions** tab, run
  **"Build Windows EXE"** (`workflow_dispatch`), and download the exe from the
  run's **Artifacts**. Tagging a release (`git tag v1.0.0 && git push --tags`)
  also attaches the exe to a GitHub Release. Works on a free private repo.
- **Local Windows (or a VM):** download a static `ffmpeg.exe` into `build/`,
  then `pip install -r requirements.txt pyinstaller && pyinstaller AubreyMp3.spec`.
  The exe lands in `dist/`.

### Notes

- The bundled ffmpeg is a GPL static build (BtbN). Distribute accordingly.
- For personal/family use with content you're allowed to download.
