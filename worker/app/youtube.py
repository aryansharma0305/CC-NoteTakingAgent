"""YouTube access: metadata + captions + stream URL via yt-dlp, transcripts via
youtube-transcript-api. All functions are blocking; call them in asyncio.to_thread."""

from __future__ import annotations

import re
from typing import Any

import httpx
import yt_dlp

from vidnotes_common.errors import ErrorCode, RateLimited, RetryableError

STREAM_FORMAT = "bv*[height<=720][ext=mp4]/bv*[height<=720]/b[height<=720]/b"
PREFERRED_LANGS = ["en", "en-US", "en-GB", "en-orig"]


def _ydl_opts(proxy: str | None, **extra: Any) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 15,
        "retries": 1,
    }
    if proxy:
        opts["proxy"] = proxy
    opts.update(extra)
    return opts


def classify_ytdlp_error(msg: str) -> RetryableError:
    low = msg.lower()
    if "429" in low or "too many requests" in low:
        return RateLimited("youtube", retry_after=None, message="Rate-limited by YouTube")
    if "not a bot" in low or "sign in to confirm" in low:
        return RetryableError(ErrorCode.YOUTUBE_BLOCKED, "YouTube is asking the server to sign in (bot check)")
    if any(k in low for k in ("private video", "unavailable", "removed", "not available", "does not exist",
                              "account associated", "terminated")):
        return RetryableError(ErrorCode.VIDEO_UNAVAILABLE)
    return RetryableError(ErrorCode.VIDEO_UNAVAILABLE, f"YouTube error: {msg[:200]}")


def extract_info(video_id: str, proxy: str | None = None, fmt: str | None = None) -> dict[str, Any]:
    opts = _ydl_opts(proxy)
    if fmt:
        opts["format"] = fmt
    else:
        # Metadata only: don't fail just because no format matches.
        opts["ignore_no_formats_error"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    except yt_dlp.utils.DownloadError as e:
        raise classify_ytdlp_error(str(e)) from e


def stream_url(video_id: str, proxy: str | None = None) -> tuple[str, dict[str, str]]:
    """Resolve a direct (progressive or video-only) stream URL; equivalent of yt-dlp -g -f ..."""
    info = extract_info(video_id, proxy, fmt=STREAM_FORMAT)
    fmts = info.get("requested_formats") or [info]
    for f in fmts:
        if f.get("url") and f.get("vcodec") not in (None, "none"):
            return f["url"], dict(f.get("http_headers") or {})
    if info.get("url"):
        return info["url"], dict(info.get("http_headers") or {})
    raise RetryableError(ErrorCode.STREAM_UNAVAILABLE)


# ---- transcripts -----------------------------------------------------------

def transcript_via_api(video_id: str, proxy: str | None = None) -> tuple[list[dict[str, Any]], str]:
    """youtube-transcript-api (v1.x instance API). Manual > generated > any language."""
    import youtube_transcript_api as yta

    kwargs: dict[str, Any] = {}
    if proxy:
        from youtube_transcript_api.proxies import GenericProxyConfig

        kwargs["proxy_config"] = GenericProxyConfig(http_url=proxy, https_url=proxy)
    api = yta.YouTubeTranscriptApi(**kwargs)
    tlist = api.list(video_id)
    transcript = None
    for finder in (tlist.find_manually_created_transcript, tlist.find_generated_transcript):
        try:
            transcript = finder(PREFERRED_LANGS)
            break
        except yta.NoTranscriptFound:
            continue
    if transcript is None:
        transcript = next(iter(tlist), None)
    if transcript is None:
        raise yta.NoTranscriptFound(video_id, PREFERRED_LANGS, tlist)
    fetched = transcript.fetch()
    snippets = [{"start": float(s.start), "duration": float(s.duration), "text": s.text} for s in fetched]
    return snippets, transcript.language_code


def classify_transcript_api_error(e: Exception) -> str:
    """'unavailable' | 'no_captions' | 'blocked' | 'other'."""
    name = type(e).__name__
    if name in ("VideoUnavailable", "VideoUnplayable", "AgeRestricted", "InvalidVideoId"):
        return "unavailable"
    if name in ("TranscriptsDisabled", "NoTranscriptFound"):
        return "no_captions"
    if name in ("RequestBlocked", "IpBlocked", "TooManyRequests", "YouTubeRequestFailed", "PoTokenRequired"):
        return "blocked"
    return "other"


def _pick_track(tracks: dict[str, list[dict]]) -> list[dict] | None:
    if not tracks:
        return None
    for lang in PREFERRED_LANGS:
        if lang in tracks:
            return tracks[lang]
    for lang, fmts in tracks.items():
        if lang.startswith("en"):
            return fmts
    orig = [k for k in tracks if k.endswith("-orig")]
    if orig:
        return tracks[orig[0]]
    return next(iter(tracks.values()))


def parse_json3(data: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for ev in data.get("events", []):
        segs = ev.get("segs")
        if not segs:
            continue
        text = "".join(s.get("utf8", "") for s in segs).replace("\n", " ").strip()
        if not text:
            continue
        out.append({"start": ev.get("tStartMs", 0) / 1000.0, "duration": ev.get("dDurationMs", 0) / 1000.0,
                    "text": text})
    return out


def transcript_via_ytdlp(info: dict[str, Any], proxy: str | None = None) -> tuple[list[dict[str, Any]], str] | None:
    """Fallback: caption tracks listed by yt-dlp (manual first, then auto), json3 format."""
    for source in ("subtitles", "automatic_captions"):
        tracks = info.get(source) or {}
        fmts = _pick_track({k: v for k, v in tracks.items() if k != "live_chat"})
        if not fmts:
            continue
        j3 = next((f for f in fmts if f.get("ext") == "json3"), None)
        if not j3:
            continue
        lang = next((k for k, v in tracks.items() if v is fmts), "und")
        with httpx.Client(timeout=20, proxy=proxy or None) as c:
            r = c.get(j3["url"])
        if r.status_code == 429:
            raise RateLimited("youtube", retry_after=None, message="Rate-limited by YouTube")
        r.raise_for_status()
        snippets = parse_json3(r.json())
        if snippets:
            return snippets, lang
    return None


_WS = re.compile(r"\s+")


def merge_segments(snippets: list[dict[str, Any]], window_sec: float = 30.0) -> list[dict[str, Any]]:
    """Normalise raw snippets into ~30-second segments: [{start, end, text}]."""
    segs: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for s in sorted(snippets, key=lambda x: x["start"]):
        text = _WS.sub(" ", s["text"].replace("\n", " ")).strip()
        if not text or text in ("[Music]", "[Applause]"):
            continue
        start = float(s["start"])
        end = start + float(s.get("duration") or 0)
        if cur is None or start - cur["start"] >= window_sec:
            cur = {"start": start, "end": end, "text": text}
            segs.append(cur)
        else:
            cur["text"] += " " + text
            cur["end"] = max(cur["end"], end)
    for seg in segs:
        seg["start"] = round(seg["start"], 2)
        seg["end"] = round(seg["end"], 2)
    return segs
