"""TRANSCRIBING (5 -> 25): metadata, Constraint A, timestamped transcript."""

from __future__ import annotations

import asyncio
from typing import Any

from vidnotes_common.errors import ErrorCode, FatalError, RetryableError
from vidnotes_common.youtube import fmt_ts

from .. import youtube as yt
from ..context import WorkerContext
from . import StageResult, maybe_fault_429


def check_info(info: dict[str, Any], max_duration: int) -> None:
    live = info.get("live_status")
    if info.get("is_live") or live in ("is_live", "is_upcoming", "post_live"):
        raise FatalError(ErrorCode.LIVE_STREAM)
    duration = int(info.get("duration") or 0)
    if duration > max_duration:
        raise_too_long(duration, max_duration)


def raise_too_long(duration: int, max_duration: int) -> None:
    raise FatalError(
        ErrorCode.VIDEO_TOO_LONG,
        f"This video is {round(duration / 60)} min; the limit is {max_duration // 60} min.",
        duration_sec=duration,
        limit_sec=max_duration,
    )


async def run(ctx: WorkerContext, job, msg) -> StageResult:
    s = ctx.settings
    vid = job.video_id
    proxy = s.yt_proxy_url or None
    title: str | None = job.video_title
    duration: int | None = job.duration_sec
    info: dict[str, Any] | None = None

    async def fetch_info() -> dict[str, Any]:
        maybe_fault_429(s, "youtube")
        await ctx.limiter.acquire("youtube")
        i = await asyncio.to_thread(yt.extract_info, vid, proxy)
        check_info(i, s.max_duration_sec)
        return i

    if duration is None:
        info = await fetch_info()
        title = info.get("title") or title
        duration = int(info.get("duration") or 0) or None
    if duration and duration > s.max_duration_sec:
        raise_too_long(duration, s.max_duration_sec)

    snippets: list[dict[str, Any]] | None = None
    lang, source, api_kind = "und", "", "other"
    maybe_fault_429(s, "youtube")
    await ctx.limiter.acquire("youtube")
    try:
        snippets, lang = await asyncio.to_thread(yt.transcript_via_api, vid, proxy)
        source = "youtube-transcript-api"
    except Exception as e:  # noqa: BLE001 - classified below, then fall back to yt-dlp
        api_kind = yt.classify_transcript_api_error(e)

    if snippets is None:
        if info is None:
            info = await fetch_info()  # also turns "private/unavailable" into VIDEO_UNAVAILABLE
            title = title or info.get("title")
        await ctx.limiter.acquire("youtube")
        res = await asyncio.to_thread(yt.transcript_via_ytdlp, info, proxy)
        if res:
            snippets, lang = res
            source = "yt-dlp"
        elif api_kind == "blocked":
            raise RetryableError(ErrorCode.YOUTUBE_BLOCKED)
        else:
            raise FatalError(ErrorCode.NO_TRANSCRIPT)

    segments = yt.merge_segments(snippets)
    if not segments:
        raise FatalError(ErrorCode.NO_TRANSCRIPT)
    if duration is None:
        duration = int(segments[-1]["end"]) + 1
        if duration > s.max_duration_sec:
            raise_too_long(duration, s.max_duration_sec)

    return StageResult(
        artifact={
            "segments": segments,
            "language": lang,
            "source": source,
            "duration_sec": duration,
            "chars": sum(len(x["text"]) for x in segments),
            "last_ts": fmt_ts(segments[-1]["end"]),
        },
        job_fields={"video_title": title, "duration_sec": duration},
    )
