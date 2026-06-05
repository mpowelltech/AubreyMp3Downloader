"""Keep a current yt-dlp binary available, auto-updating on launch.

YouTube changes frequently break a frozen copy of yt-dlp, so we never bake it
into the exe.  Instead:

  * Windows: download ``yt-dlp.exe`` into the per-user cache on first run, then
    run ``yt-dlp --update`` in the background on every later launch.
  * dev (mac/linux): just use the ``yt-dlp`` already on PATH (brew/pip managed).
"""

from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

from .paths import cache_dir, is_frozen

GITHUB_LATEST_API = "https://api.github.com/repos/mpowelltech/AubreyMp3Downloader/releases/latest"
YTDLP_WIN_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
# Deno is the JavaScript runtime yt-dlp uses to solve YouTube's "n" challenge.
# Without it, many videos fail with "This video is not available". We download
# it once (like yt-dlp) rather than bundling it, to keep the exe small.
DENO_WIN_URL = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip"

def _ssl_context() -> ssl.SSLContext:
    """Verify HTTPS against certifi's CA bundle instead of the OS store.

    A fresh Windows PC may not have the needed root CA cached, and Python's
    OpenSSL (unlike the OS/browsers) won't fetch missing roots on demand, which
    causes CERTIFICATE_VERIFY_FAILED. certifi makes our downloads independent of
    the machine's cert store.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_SSL_CTX = _ssl_context()

# Stop a console window flashing up when we shell out on Windows.
_CREATE_NO_WINDOW = 0x08000000


def _no_window() -> dict:
    return {"creationflags": _CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def _bin_path() -> Path:
    return cache_dir() / "yt-dlp.exe"


def ensure_ytdlp(status: Optional[Callable[[str], None]] = None) -> str:
    """Return a path/command for yt-dlp, downloading it on first Windows run.

    ``status`` is an optional callback for user-facing progress messages.
    Raises on a failed first-run download (e.g. no internet and no cache).
    """
    if sys.platform != "win32":
        return "yt-dlp"  # dev: assume installed via brew/pip

    exe = _bin_path()
    if not exe.exists():
        if status:
            status("First run: downloading the YouTube engine…")
        _download(YTDLP_WIN_URL, exe)
    return str(exe)


def _download(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(".part")
    req = urllib.request.Request(url, headers={"User-Agent": "AubreysYT-MP3-Downloader"})
    with urllib.request.urlopen(req, timeout=120, context=_SSL_CTX) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    tmp.replace(dest)


def ensure_deno(status: Optional[Callable[[str], None]] = None) -> Optional[str]:
    """Return a path/command for Deno, downloading it on first Windows run.

    On dev machines we just use whatever ``deno`` is on PATH (None if absent,
    in which case yt-dlp falls back to any other runtime it can find).
    """
    if sys.platform != "win32":
        return shutil.which("deno")

    exe = cache_dir() / "deno.exe"
    if not exe.exists():
        if status:
            status("First run: downloading the YouTube challenge solver…")
        _download_zip_member(DENO_WIN_URL, "deno.exe", exe)
    return str(exe)


def _download_zip_member(url: str, member_suffix: str, dest: Path) -> None:
    """Download a .zip and extract the single member ending in ``member_suffix``."""
    tmpzip = dest.parent / (dest.name + ".zip.part")
    req = urllib.request.Request(url, headers={"User-Agent": "AubreysYT-MP3-Downloader"})
    try:
        with urllib.request.urlopen(req, timeout=300, context=_SSL_CTX) as r, open(tmpzip, "wb") as f:
            shutil.copyfileobj(r, f, length=1 << 20)
        with zipfile.ZipFile(tmpzip) as z:
            name = next((n for n in z.namelist() if n.lower().endswith(member_suffix.lower())), None)
            if not name:
                raise RuntimeError(f"{member_suffix} not found in {url}")
            tmp = dest.with_suffix(".part")
            with z.open(name) as src, open(tmp, "wb") as out:
                shutil.copyfileobj(src, out, length=1 << 20)
            tmp.replace(dest)
    finally:
        try:
            tmpzip.unlink()
        except OSError:
            pass


def update_in_background(ytdlp_cmd: str) -> None:
    """Fire-and-forget ``yt-dlp --update``; swallow every error (e.g. offline).

    No-op on dev machines so we never disturb a brew/pip-managed yt-dlp.
    """
    if sys.platform != "win32":
        return

    def _run() -> None:
        try:
            subprocess.run(
                [ytdlp_cmd, "--update"],
                timeout=90,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **_no_window(),
            )
        except Exception:
            pass  # any failure just leaves the cached copy in place

    threading.Thread(target=_run, daemon=True).start()


# --------------------------------------------------------------------------- #
# App self-update (the .exe itself)
# --------------------------------------------------------------------------- #

def _ver_tuple(v: str) -> tuple:
    v = (v or "").strip().lstrip("vV").split("-")[0].split("+")[0]
    out = []
    for part in v.split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    return tuple(out) or (0,)


def check_for_app_update(current_version: str) -> Optional[dict]:
    """Return ``{'tag', 'url'}`` if GitHub has a newer release exe, else None.

    Only meaningful for the frozen Windows exe (that's the only thing we can
    replace in place). Never raises — any problem just returns None (no nag).
    """
    if not (is_frozen() and sys.platform == "win32"):
        return None
    try:
        req = urllib.request.Request(
            GITHUB_LATEST_API,
            headers={"User-Agent": "AubreysYT-MP3-Downloader",
                     "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as r:
            data = json.loads(r.read().decode("utf-8"))
        tag = data.get("tag_name") or ""
        if _ver_tuple(tag) <= _ver_tuple(current_version):
            return None
        url = next((a.get("browser_download_url") for a in data.get("assets", [])
                    if str(a.get("name", "")).lower().endswith(".exe")), None)
        return {"tag": tag, "url": url} if url else None
    except Exception:
        return None


def download_and_relaunch(asset_url: str, status: Optional[Callable[[str], None]] = None) -> None:
    """Download the new exe beside the current one, then spawn a helper that
    waits for this process to exit, swaps the exe in place, and relaunches it.
    The caller must exit the app right after this returns.
    """
    current = Path(sys.executable)
    new = current.with_name(current.stem + ".update.exe")
    if status:
        status("Downloading update…")
    _download(asset_url, new)
    _spawn_replacer(current, new)


def _spawn_replacer(current: Path, new: Path) -> None:
    pid = os.getpid()
    bat = cache_dir() / "apply_update.bat"
    # ping (not timeout) for the delay — timeout needs a console we won't have.
    script = (
        "@echo off\r\n"
        ":wait\r\n"
        f'tasklist /FI "PID eq {pid}" 2>NUL | find "{pid}" >NUL\r\n'
        "if not errorlevel 1 (\r\n"
        "  ping -n 2 127.0.0.1 >NUL\r\n"
        "  goto wait\r\n"
        ")\r\n"
        f'move /Y "{new}" "{current}" >NUL\r\n'
        f'start "" "{current}"\r\n'
        'del "%~f0"\r\n'
    )
    bat.write_text(script, encoding="utf-8")
    subprocess.Popen(
        ["cmd", "/c", str(bat)],
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        close_fds=True,
    )
