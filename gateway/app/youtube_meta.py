"""Submit-time metadata (duration, title, live flag) so >20 min videos are
rejected before any work is queued.

YouTube Data API v3 videos.list when YOUTUBE_API_KEY is set, otherwise a light
scrape of the watch page. Whole lookup is capped at METADATA_TIMEOUT_SEC (3 s);
on timeout or any error the job is still accepted and the transcript stage
enforces the limit.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from dataclasses import dataclass

import httpx

from vidnotes_common.config import Settings


@dataclass
class VideoMeta:
    video_id: str
    title: str | None
    duration_sec: int | None
    is_live: bool = False
    source: str = ""


_ISO = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def parse_iso_duration(s: str) -> int | None:
    m = _ISO.fullmatch(s or "")
    if not m or not any(m.groups()):
        return None
    d, h, mi, sec = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + sec


async def _via_data_api(video_id: str, key: str, http: httpx.AsyncClient) -> VideoMeta | None:
    r = await http.get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={"part": "contentDetails,snippet", "id": video_id, "key": key},
    )
    if r.status_code != 200:
        return None
    items = r.json().get("items") or []
    if not items:
        return None  # private/removed: let the pipeline classify it (DLQ demo)
    it = items[0]
    live = it.get("snippet", {}).get("liveBroadcastContent") in ("live", "upcoming")
    return VideoMeta(
        video_id,
        it.get("snippet", {}).get("title"),
        parse_iso_duration(it.get("contentDetails", {}).get("duration", "")),
        live,
        "youtube-data-api",
    )


_LEN = re.compile(r'"lengthSeconds":"(\d+)"')
_TITLE = re.compile(r'<meta name="title" content="([^"]*)"')
_VD_TITLE = re.compile(r'"videoDetails":\{.*?"title":"((?:[^"\\]|\\.)*)"', re.S)


def parse_watch_page(video_id: str, page: str) -> VideoMeta | None:
    m = _LEN.search(page)
    if not m:
        return None
    title = None
    t = _TITLE.search(page)
    if t:
        title = html.unescape(t.group(1))
    else:
        t2 = _VD_TITLE.search(page)
        if t2:
            try:
                title = json.loads(f'"{t2.group(1)}"')
            except ValueError:
                title = None
    live = '"isLive":true' in page or '"isUpcoming":true' in page
    duration = int(m.group(1)) or None
    return VideoMeta(video_id, title, duration, live, "watch-page")


async def _via_watch_page(video_id: str, http: httpx.AsyncClient) -> VideoMeta | None:
    r = await http.get(
        f"https://www.youtube.com/watch?v={video_id}",
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/126.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Cookie": "CONSENT=YES+1",
        },
        follow_redirects=True,
    )
    if r.status_code != 200:
        return None
    return parse_watch_page(video_id, r.text)


async def lookup(video_id: str, settings: Settings, http: httpx.AsyncClient) -> VideoMeta | None:
    async def _go() -> VideoMeta | None:
        if settings.youtube_api_key:
            meta = await _via_data_api(video_id, settings.youtube_api_key, http)
            if meta:
                return meta
        return await _via_watch_page(video_id, http)

    try:
        return await asyncio.wait_for(_go(), timeout=settings.metadata_timeout_sec)
    except Exception:  # noqa: BLE001 - timeout or network: accept, transcript stage enforces
        return None
