"""EXTRACTING_FRAMES (55 -> 80): real frames only, straight from the stream.

yt-dlp resolves a direct stream URL once; ffmpeg seeks with -ss before -i (HTTP
range requests, no full download) and grabs FRAME_BEST_OF frames one second
apart; the sharpest is kept. Frames are stored at deterministic S3 keys, so a
re-run after a crash skips frames already captured.

Graceful degradation: per-timestamp failures are recorded and the stage always
advances. Only a failure to resolve the stream at all is retried, and once the
retry budget is spent it degrades to a text-only page instead of the DLQ.
"""

from __future__ import annotations

import asyncio
import glob
import hashlib
import io
import os
import random
import subprocess
import tempfile
import uuid
from typing import Any

from PIL import Image, ImageFilter, ImageStat

from vidnotes_common.errors import ErrorCode, PipelineError, RetryableError
from vidnotes_common.status import Stage, stage_progress

from .. import repo
from .. import youtube as yt
from ..context import WorkerContext
from ..storage import frame_key
from . import StageResult, maybe_fault_429


def sharpness(img: Image.Image) -> float:
    """Variance of an edge map (Laplacian-variance stand-in, no numpy needed)."""
    g = img.convert("L")
    if g.width > 640:
        g = g.resize((640, int(g.height * 640 / g.width)))
    return ImageStat.Stat(g.filter(ImageFilter.FIND_EDGES)).var[0]


def grab_frame(url: str, headers: dict[str, str], t: int, *, best_of: int, timeout: float,
               max_width: int, quality: int) -> bytes:
    """Blocking: run ffmpeg, return the sharpest of `best_of` frames as JPEG bytes."""
    with tempfile.TemporaryDirectory(prefix="vn_frame_") as tmp:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
        if headers:
            cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
        cmd += ["-ss", str(max(0, t)), "-i", url, "-frames:v", str(max(1, best_of))]
        if best_of > 1:
            cmd += ["-vf", "fps=1"]
        cmd += ["-q:v", "2", os.path.join(tmp, "f_%02d.jpg")]
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        files = sorted(glob.glob(os.path.join(tmp, "f_*.jpg")))
        if not files:
            err = proc.stderr.decode(errors="replace")[-300:]
            raise RuntimeError(f"ffmpeg produced no frame (exit {proc.returncode}): {err}")
        images = [Image.open(f) for f in files]
        best = max(images, key=sharpness).convert("RGB")
        if best.width > max_width:
            best = best.resize((max_width, int(best.height * max_width / best.width)), Image.LANCZOS)
        buf = io.BytesIO()
        best.save(buf, "JPEG", quality=quality, optimize=True)
        for im in images:
            im.close()
        return buf.getvalue()


async def run(ctx: WorkerContext, job, msg) -> StageResult:
    s = ctx.settings
    job_id = str(job.id)
    synthesis = await repo.get_artifact(ctx.engine, job.id, "synthesis") or {}
    moments: list[dict[str, Any]] = list(synthesis.get("visual_moments") or [])

    results: list[dict[str, Any]] = []
    todo: list[dict[str, Any]] = []
    for m in moments:
        base = {k: m.get(k) for k in ("timestamp_sec", "caption", "reason", "section_index")}
        if not (m.get("caption") or "").strip():
            results.append({**base, "status": "failed", "error": ErrorCode.FRAME_MISSING_CAPTION})
        else:
            todo.append(base)

    stream: tuple[str, dict[str, str]] | None = None
    stream_error: str | None = None
    # Frames already in S3 (from a crashed earlier attempt) don't need the stream.
    cached = {m["timestamp_sec"]: await ctx.storage.exists(frame_key(job_id, m["timestamp_sec"])) for m in todo}
    # Summary reused from an earlier job on the same video: its frames are the same real
    # captures at the same timestamps, so copy those bytes into this job's own keys.
    reuse: dict[int, str] = {}
    src_job = synthesis.get("cached_from")
    if src_job:
        try:
            src_frames = await repo.get_artifact(ctx.engine, uuid.UUID(src_job), "frames") or {}
        except ValueError:
            src_frames = {}
        reuse = {int(f["timestamp_sec"]): f["object_key"] for f in src_frames.get("frames", [])
                 if f.get("status") == "ok" and f.get("object_key", "").startswith(f"jobs/{src_job}/")}
    if todo and not all(cached[m["timestamp_sec"]] or int(m["timestamp_sec"]) in reuse for m in todo):
        try:
            maybe_fault_429(s, "youtube")
            await ctx.limiter.acquire("youtube")
            stream = await asyncio.to_thread(yt.stream_url, job.video_id, s.yt_proxy_url or None)
        except PipelineError as e:
            if isinstance(e, RetryableError) and msg.attempt < s.max_attempts:
                raise
            stream_error = e.message  # budget spent: degrade to text-only

    sem = asyncio.Semaphore(max(1, s.frame_parallelism))
    done = 0

    async def one(m: dict[str, Any]) -> dict[str, Any]:
        nonlocal done
        t = int(m["timestamp_sec"])
        key = frame_key(job_id, t)
        async with sem:
            try:
                copied = None
                if cached.get(m["timestamp_sec"]):
                    data = await ctx.storage.get(key)
                elif t in reuse:
                    data = await ctx.storage.get(reuse[t])
                    await ctx.storage.put(key, data)
                    copied = reuse[t]
                else:
                    if stream is None:
                        raise RuntimeError(stream_error or "stream unavailable")
                    if s.fault_inject_frame_fail and random.random() < s.fault_inject_frame_fail:
                        raise RuntimeError("injected frame failure (FAULT_INJECT_FRAME_FAIL)")
                    data = await asyncio.to_thread(
                        grab_frame, stream[0], stream[1], t,
                        best_of=s.frame_best_of, timeout=s.frame_timeout_sec,
                        max_width=s.frame_max_width, quality=s.frame_jpeg_quality,
                    )
                    await ctx.storage.put(key, data)
                out = {**m, "status": "ok", "object_key": key, "sha256": hashlib.sha256(data).hexdigest(),
                       "bytes": len(data)}
                if copied:
                    out["copied_from"] = copied
            except Exception as e:  # noqa: BLE001 - every per-frame failure degrades gracefully
                out = {**m, "status": "failed", "error": ErrorCode.FRAME_EXTRACTION_FAILED,
                       "detail": str(e)[:300]}
            done += 1
            await repo.update_job(ctx.engine, job.id,
                                  progress=stage_progress(Stage.EXTRACT_FRAMES, done / max(1, len(todo))))
            return out

    results.extend(await asyncio.gather(*(one(m) for m in todo)))
    results.sort(key=lambda r: r["timestamp_sec"])
    ok = [r for r in results if r["status"] == "ok"]
    n_copied = sum(1 for r in ok if r.get("copied_from"))
    return StageResult(
        note={"cached_from": src_job, "frames_reused": n_copied} if n_copied else {},
        artifact={"frames": results, "stream_error": stream_error, "requested": len(moments), "ok": len(ok)},
        object_keys=[r["object_key"] for r in ok],
    )
