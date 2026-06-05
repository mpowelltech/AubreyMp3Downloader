#!/bin/bash
# Double-click to run the app from source on macOS — no exe build needed.
# Great for quick UI iteration. Uses the project's .venv plus Deno/ffmpeg on PATH.
cd "$(dirname "$0")/.." || exit 1

if [ ! -x ".venv/bin/python" ]; then
  echo "No .venv found. Create it first:"
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt 'yt-dlp[default]'"
  read -r -p "Press return to close…" _ ; exit 1
fi

# Prefer the venv's up-to-date yt-dlp (with the EJS challenge solver) over any
# older system one, while keeping Homebrew's deno + ffmpeg on PATH.
export PATH="$PWD/.venv/bin:$PATH"

if [ ! -x ".venv/bin/yt-dlp" ]; then
  echo "Installing yt-dlp (one-time)…"
  .venv/bin/pip install -q "yt-dlp[default]"
fi
command -v deno   >/dev/null || echo "Note: 'deno' not found (brew install deno) — some videos may fail."
command -v ffmpeg >/dev/null || echo "Note: 'ffmpeg' not found (brew install ffmpeg)."

exec .venv/bin/python run.py
