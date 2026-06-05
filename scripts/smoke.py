"""Headless pipeline smoke test (no GUI): fetch -> download -> trim -> mp3.

    python scripts/smoke.py [youtube_url] [out.mp3]

Defaults to "Me at the zoo" (the first YouTube video, ~19s) and trims to 2-10s.
Lets us validate the yt-dlp + ffmpeg pipeline on a Mac without the GUI.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.audio import make_mp3                            # noqa: E402
from app.downloader import download_audio, fetch_info     # noqa: E402
from app.updater import ensure_deno, ensure_ytdlp         # noqa: E402


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("smoke_out.mp3")

    ytdlp = ensure_ytdlp(status=print)
    deno = ensure_deno(status=print)
    print(f"yt-dlp: {ytdlp}\ndeno:   {deno}")
    info = fetch_info(ytdlp, url, deno)
    print(f"Title: {info.title!r}  Duration: {info.duration}s  Thumb: {bool(info.thumbnail)}")

    with tempfile.TemporaryDirectory() as tmp:
        audio, thumb = download_audio(
            ytdlp, url, Path(tmp), deno=deno,
            on_progress=lambda p: print(f"\rDownloading… {p:5.1f}%", end=""),
        )
        print()
        print(f"Audio: {audio.name}  Thumb: {thumb.name if thumb else None}")
        make_mp3(
            audio, out, title=info.title, start=2, end=10, cover=thumb,
            total_seconds=8,
            on_progress=lambda f: print(f"\rConverting… {f * 100:5.1f}%", end=""),
        )
        print()

    print(f"Wrote {out} ({out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
