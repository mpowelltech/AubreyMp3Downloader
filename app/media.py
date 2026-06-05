"""Fetch a video's thumbnail for the on-screen preview.

Purely cosmetic and always best-effort: any failure (no internet, odd image,
missing Pillow) returns ``None`` and the UI just shows its placeholder. Never
raises, so it can't break a download.
"""

from __future__ import annotations

import io
import ssl
import urllib.request
from typing import Optional


def _ssl_context() -> ssl.SSLContext:
    # Same reasoning as updater._ssl_context: verify against certifi so a fresh
    # Windows PC without cached root CAs doesn't fail HTTPS.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_SSL_CTX = _ssl_context()
_MAX_BYTES = 8 * 1024 * 1024  # never read more than 8 MB for a thumbnail


def fetch_image(url: Optional[str], box: int = 320):
    """Download ``url`` and return a square-cropped PIL image (<= ``box`` px), or None."""
    if not url or not str(url).startswith("http"):
        return None
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AubreysYT-MP3-Downloader"})
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as r:
            data = r.read(_MAX_BYTES + 1)
        if len(data) > _MAX_BYTES:
            return None
        img = Image.open(io.BytesIO(data))
        img.load()
        img = img.convert("RGB")
        return _square(img, box)
    except Exception:
        return None


def _square(img, box: int):
    """Centre-crop to a square, then downscale to ``box`` px."""
    from PIL import Image
    w, h = img.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    if side > box:
        img = img.resize((box, box), Image.LANCZOS)
    return img
