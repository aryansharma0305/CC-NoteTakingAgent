"""YouTube URL parsing shared by the gateway (validation) and the worker."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com",
          "www.youtube-nocookie.com"}


def parse_video_id(url: str) -> str | None:
    """Return the 11-char video id for watch?v=, youtu.be/, shorts/, embed/, live/ URLs."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if _ID_RE.match(url):
        return url
    if "://" not in url:
        url = "https://" + url
    try:
        p = urlparse(url)
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    candidate: str | None = None
    if host in ("youtu.be", "www.youtu.be"):
        candidate = p.path.lstrip("/").split("/")[0]
    elif host in _HOSTS:
        if p.path == "/watch":
            candidate = (parse_qs(p.query).get("v") or [None])[0]
        else:
            parts = [x for x in p.path.split("/") if x]
            if len(parts) >= 2 and parts[0] in ("shorts", "embed", "v", "live", "e"):
                candidate = parts[1]
    if candidate and _ID_RE.match(candidate):
        return candidate
    return None


def watch_url(video_id: str, t: int | None = None) -> str:
    base = f"https://www.youtube.com/watch?v={video_id}"
    return f"{base}&t={int(t)}s" if t is not None else base


def fmt_ts(seconds: float | int) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
