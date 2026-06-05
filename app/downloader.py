"""Thin wrapper around the yt-dlp binary: metadata fetch + audio download."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .paths import ffmpeg_location

_CREATE_NO_WINDOW = 0x08000000
_PCT = re.compile(r"(\d{1,3}(?:\.\d)?)%")

# Order matters: pick the real audio file before the .jpg thumbnail.
_AUDIO_GLOBS = ("audio.m4a", "audio.webm", "audio.opus", "audio.mp3",
                "audio.ogg", "audio.aac", "audio.wav", "audio.*")
_THUMB_GLOBS = ("audio.jpg", "audio.jpeg", "audio.png", "audio.webp")


@dataclass
class VideoInfo:
    title: str
    duration: float            # seconds (0 if unknown / live)
    thumbnail: Optional[str]   # url
    uploader: str = ""         # channel / author, for the preview line
    source: str = ""           # extractor key lowered, e.g. "youtube", "vimeo"
    webpage_url: str = ""       # canonical URL yt-dlp resolved


@dataclass
class SearchResult:
    """One hit from a name search — enough to show in the chooser."""
    title: str
    duration: float
    uploader: str
    url: str
    thumbnail: Optional[str]


@dataclass
class PlaylistInfo:
    title: str
    entries: list                # list[tuple[str, str]] -> (url, title)
    total: int                   # how many the playlist actually holds
    truncated: bool              # True if we capped ``entries`` below ``total``


class DownloadError(Exception):
    """A failure worded for a non-technical user."""


def _no_window() -> dict:
    return {"creationflags": _CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def _engine_args(deno: Optional[str]) -> list[str]:
    """Args that let yt-dlp solve YouTube's JS 'n' challenge.

    ``--remote-components ejs:github`` lets yt-dlp fetch fresh EJS solver
    scripts if its bundled ones are missing/stale; ``--js-runtimes deno:<path>``
    points it at our downloaded Deno when it isn't on PATH.
    """
    args = ["--remote-components", "ejs:github"]
    if deno:
        args += ["--js-runtimes", f"deno:{deno}"]
    return args


def fetch_info(ytdlp: str, url: str, deno: Optional[str] = None) -> VideoInfo:
    """Read title/duration/thumbnail for a single item without downloading it.

    Rejects live streams (they have no fixed length and would download forever)
    with a friendly message rather than letting the user start a doomed job.
    """
    try:
        proc = subprocess.run(
            [ytdlp, "-J", "--no-playlist", "--no-warnings",
             "--retries", "3", "--socket-timeout", "30", *_engine_args(deno), url],
            capture_output=True, text=True, timeout=150, **_no_window(),
        )
    except subprocess.TimeoutExpired:
        raise DownloadError("Timed out reading the link. Check your internet and try again.")
    except FileNotFoundError:
        raise DownloadError("The download engine isn't available yet. Please reopen the app.")
    if proc.returncode != 0:
        raise DownloadError(_friendly(proc.stderr))
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise DownloadError("Couldn't read that link. Is it correct and complete?")
    # A playlist URL slipped through (e.g. a bare /playlist link): tell the caller
    # so it can route to bulk import instead of trying to grab "one" video.
    if data.get("_type") == "playlist" or "entries" in data:
        raise DownloadError("__PLAYLIST__")  # sentinel; the UI catches and offers bulk import
    if _is_live(data):
        raise DownloadError(
            "That looks like a live stream, which has no fixed length and can't be "
            "saved as a song. Try a normal video instead.")
    return VideoInfo(
        title=(data.get("title") or "audio").strip() or "audio",
        duration=float(data.get("duration") or 0),
        thumbnail=data.get("thumbnail"),
        uploader=(data.get("uploader") or data.get("channel") or "").strip(),
        source=str(data.get("extractor_key") or data.get("extractor") or "").lower(),
        webpage_url=data.get("webpage_url") or url,
    )


def _is_live(data: dict) -> bool:
    if data.get("is_live"):
        return True
    return str(data.get("live_status") or "") in ("is_live", "is_upcoming", "post_live")


# --------------------------------------------------------------------------- #
# Search by name + playlist expansion (metadata only — no media downloaded)
# --------------------------------------------------------------------------- #

_URL_RE = re.compile(r"^\s*(https?://|www\.)\S+\s*$", re.I)
_VIDEO_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/)([\w-]{6,})")


def looks_like_url(text: str) -> bool:
    """True if ``text`` is a single link rather than words to search for."""
    t = (text or "").strip()
    if not t or " " in t or "\n" in t:
        return False
    return bool(_URL_RE.match(t)) or ("." in t and "/" in t)


def has_playlist(url: str) -> bool:
    """True if the link carries a playlist (a ``list=`` id or a /playlist path)."""
    u = (url or "").lower()
    return "list=" in u or "/playlist" in u


def has_single_video(url: str) -> bool:
    """True if the link points at one specific video (so 'just this one' is meaningful)."""
    return bool(_VIDEO_ID_RE.search(url or ""))


def _thumb_of(entry: dict) -> Optional[str]:
    thumbs = entry.get("thumbnails") or []
    if isinstance(thumbs, list) and thumbs:
        # last is usually the largest; fall back to any with a url
        for t in reversed(thumbs):
            if isinstance(t, dict) and t.get("url"):
                return t["url"]
    return entry.get("thumbnail")


def _entry_url(entry: dict) -> Optional[str]:
    u = entry.get("url") or entry.get("webpage_url")
    if u and str(u).startswith("http"):
        return u
    vid = entry.get("id")
    if vid and str(entry.get("ie_key") or "").lower().startswith("youtube"):
        return f"https://www.youtube.com/watch?v={vid}"
    # Only ever hand back a real http(s) link — never a scheme-less/relative ref
    # that would just fail later with a confusing error.
    return None


def search(ytdlp: str, query: str, deno: Optional[str] = None, n: int = 6) -> list:
    """Return up to ``n`` SearchResult hits for a plain-text query (YouTube).

    Uses a flat search (metadata only) so it's fast. Live results are dropped
    since they can't be saved. Returns ``[]`` when nothing matches.
    """
    query = (query or "").strip()
    if not query:
        return []
    n = max(1, min(n, 12))
    try:
        proc = subprocess.run(
            [ytdlp, "-J", "--flat-playlist", "--no-warnings",
             "--retries", "2", "--socket-timeout", "30", *_engine_args(deno),
             f"ytsearch{n}:{query}"],
            capture_output=True, text=True, timeout=90, **_no_window(),
        )
    except subprocess.TimeoutExpired:
        raise DownloadError("Search timed out. Check your internet and try again.")
    except FileNotFoundError:
        raise DownloadError("The download engine isn't available yet. Please reopen the app.")
    if proc.returncode != 0:
        raise DownloadError(_friendly(proc.stderr))
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise DownloadError("Couldn't run that search. Please try again.")
    out: list = []
    for e in (data.get("entries") or []):
        if not isinstance(e, dict):
            continue
        if str(e.get("live_status") or "") == "is_live":
            continue
        u = _entry_url(e)
        if not u:
            continue
        out.append(SearchResult(
            title=(e.get("title") or "Untitled").strip(),
            duration=float(e.get("duration") or 0),
            uploader=(e.get("uploader") or e.get("channel") or "").strip(),
            url=u,
            thumbnail=_thumb_of(e),
        ))
    return out


def fetch_playlist(ytdlp: str, url: str, deno: Optional[str] = None, limit: int = 60) -> PlaylistInfo:
    """Expand a playlist/mix link into a capped list of (url, title) pairs.

    Metadata only (flat) so it's quick even for long lists. Caps at ``limit`` so
    an enormous or endless 'Mix' can't flood the UI; the caller tells the user
    when it truncated.
    """
    try:
        proc = subprocess.run(
            [ytdlp, "-J", "--flat-playlist", "--no-warnings",
             "--retries", "2", "--socket-timeout", "30",
             "--playlist-end", str(limit), *_engine_args(deno), url],
            capture_output=True, text=True, timeout=180, **_no_window(),
        )
    except subprocess.TimeoutExpired:
        raise DownloadError("Timed out reading the playlist. Check your internet and try again.")
    except FileNotFoundError:
        raise DownloadError("The download engine isn't available yet. Please reopen the app.")
    if proc.returncode != 0:
        raise DownloadError(_friendly(proc.stderr))
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise DownloadError("Couldn't read that playlist. Is the link correct?")
    raw = data.get("entries") or []
    entries: list = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        if str(e.get("live_status") or "") == "is_live":
            continue
        title = (e.get("title") or "Untitled").strip()
        # Flat playlists list private/deleted items as placeholder titles — skip
        # them so we never add a row that can only ever fail.
        if title.lower() in ("[private video]", "[deleted video]", "[unavailable video]"):
            continue
        u = _entry_url(e)
        if not u:
            continue
        entries.append((u, title))
    if not entries:
        raise DownloadError("That playlist appears to be empty or unavailable.")
    total = int(data.get("playlist_count") or len(raw) or len(entries))
    return PlaylistInfo(
        title=(data.get("title") or "Playlist").strip(),
        entries=entries,
        total=max(total, len(entries)),
        truncated=len(entries) >= limit and total > len(entries),
    )


def download_audio(
    ytdlp: str,
    url: str,
    workdir: Path,
    deno: Optional[str] = None,
    on_progress: Optional[Callable[[float], None]] = None,
) -> tuple[Path, Optional[Path]]:
    """Download best audio (+ a jpg thumbnail) into ``workdir``.

    Returns ``(audio_file, thumbnail_file_or_None)``.
    """
    cmd = [
        ytdlp, url,
        "--no-playlist", "--no-warnings",
        "--retries", "5", "--fragment-retries", "10", "--socket-timeout", "30",
        "-f", "bestaudio/best",
        "-o", str(workdir / "audio.%(ext)s"),
        "--write-thumbnail", "--convert-thumbnails", "jpg",
        "--newline", "--progress-template", "dl:%(progress._percent_str)s",
        *_engine_args(deno),
    ]
    loc = ffmpeg_location()
    if loc:
        cmd += ["--ffmpeg-location", loc]

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, **_no_window(),
        )
    except FileNotFoundError:
        raise DownloadError("The YouTube engine isn't available yet. Please reopen the app.")

    assert proc.stdout is not None
    tail: list[str] = []
    # readline() (not "for line in proc.stdout") so progress streams in real time
    # on Windows instead of being block-buffered until the process exits.
    for raw in iter(proc.stdout.readline, ""):
        line = raw.strip()
        if line.startswith("dl:"):
            m = _PCT.search(line)
            if m and on_progress:
                on_progress(float(m.group(1)))
        elif line:
            tail.append(line)
            del tail[:-25]  # keep only the last 25 lines for error context
    proc.wait()
    if proc.returncode != 0:
        raise DownloadError(_friendly("\n".join(tail)))

    audio = _find_one(workdir, _AUDIO_GLOBS, exclude_suffixes={".jpg", ".jpeg", ".png", ".webp"})
    if audio is None:
        raise DownloadError("Couldn't find the downloaded audio. Please try again.")
    thumb = _find_one(workdir, _THUMB_GLOBS)
    return audio, thumb


_PARTIAL_SUFFIXES = {".part", ".ytdl", ".temp", ".tmp"}


def _find_one(d: Path, patterns, exclude_suffixes: set[str] | None = None) -> Optional[Path]:
    excl = set(_PARTIAL_SUFFIXES)
    if exclude_suffixes:
        excl |= exclude_suffixes
    for pat in patterns:
        for hit in sorted(d.glob(pat)):
            if hit.suffix.lower() in excl:
                continue  # skip half-written .part/.ytdl files
            if hit.is_file() and hit.stat().st_size > 0:
                return hit
    return None


def _friendly(stderr: str) -> str:
    """Map yt-dlp's stderr to a plain-language message."""
    s = (stderr or "").lower()
    if "private video" in s or "members-only" in s or "join this channel" in s:
        return "That video is private or members-only, so it can't be downloaded."
    if "sign in to confirm your age" in s or ("age" in s and "restricted" in s):
        return "That video is age-restricted and can't be downloaded here."
    if "sign in to confirm you" in s or "not a bot" in s:
        return ("The site is asking the app to sign in to prove it isn't a robot. "
                "Try a different link, or try again in a little while.")
    if any(t in s for t in ("challenge", "js runtime", "javascript runtime",
                            "no solutions", "ejs", "[jsc]", "nsig", "n-sig")):
        return ("Couldn't process this video. The helper that solves the site's "
                "protection isn't ready yet.\nCheck your internet connection and reopen "
                "the app so it can finish setting up.")
    if "video unavailable" in s or "this video is not available" in s or "no longer available" in s:
        return "That video is unavailable (it may be removed, private, or blocked in your country)."
    if "geo" in s and ("restrict" in s or "block" in s):
        return "That video is blocked in your country and can't be downloaded."
    if "unsupported url" in s:
        return ("That website isn't supported. This works with YouTube and many other "
                "video and music sites, but not every link.")
    if "is not a valid url" in s or "unable to download webpage" in s and "http" not in s:
        return "That doesn't look like a complete link. Copy the whole web address and try again."
    if "no video formats" in s or "requested format is not available" in s:
        return "That link has no downloadable audio. Try a different one."
    if any(t in s for t in ("failed to resolve", "getaddrinfo", "temporary failure",
                            "network is unreachable", "connection reset", "connection refused",
                            "timed out", "timeout", "[errno")):
        return "Couldn't reach the internet. Please check your connection and try again."
    if "certificate" in s:
        return "Secure-connection problem. Check the date/time on this PC and your internet, then retry."
    last = [ln for ln in (stderr or "").strip().splitlines() if ln.strip()
            and not ln.lower().startswith("warning")]
    msg = last[-1] if last else "Something went wrong while downloading."
    # Never surface a raw traceback / debug dump to a non-technical user.
    if len(msg) > 200 or "traceback" in msg.lower() or msg.lower().startswith("file \""):
        return "Something went wrong while downloading. Please try again."
    return msg
