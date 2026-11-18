"""Debug panel: GET /debug serves a single HTML page; its data comes from the
admin-token-protected endpoints below (plus the existing /admin/jobs routes)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import desc, func, select

from vidnotes_common.errors import ErrorCode
from vidnotes_common.models import jobs

from .errors import ApiError
from .health import _check, _pg, _s3
from .routes_admin import require_admin
from .routes_jobs import job_view
from .state import AppState, get_state

WORKER_KEY_PREFIX = "vn:worker:"  # written by worker/app/heartbeat.py
PAGE = Path(__file__).parent / "static" / "debug.html"

# Non-secret settings worth seeing while debugging. Never add keys, tokens or secrets here.
SAFE_SETTINGS = [
    "llm_provider", "llm_model", "llm_base_url", "llm_rpm", "llm_tpm", "llm_max_output_tokens",
    "llm_timeout_sec", "max_duration_sec", "max_frames", "min_frames", "notes_words", "segment_window_sec",
    "chunk_tokens", "frame_parallelism", "frame_best_of", "frame_timeout_sec", "max_attempts", "retry_delays",
    "youtube_rps", "notion_rps", "notion_version", "fault_inject_429", "fault_inject_frame_fail", "log_level",
]

page_router = APIRouter()
router = APIRouter(prefix="/admin/debug", dependencies=[Depends(require_admin)])


@page_router.get("/debug", response_class=HTMLResponse, include_in_schema=False)
async def debug_page() -> HTMLResponse:
    return HTMLResponse(PAGE.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


async def _workers(st: AppState) -> list[dict]:
    keys = [k async for k in st.redis.scan_iter(match=f"{WORKER_KEY_PREFIX}*", count=100)]
    if not keys:
        return []
    out = []
    for raw in await st.redis.mget(keys):
        if raw:
            try:
                out.append(json.loads(raw))
            except ValueError:
                continue
    return sorted(out, key=lambda w: w.get("worker_id", ""))


@router.get("/overview")
async def overview(limit: int = 30, st: AppState = Depends(get_state)):
    s = st.settings

    async def queues():
        return await st.publisher.queue_depths()

    async def safe(coro, default):
        try:
            return await asyncio.wait_for(coro, 3)
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {str(e)[:120]}"} if default is None else default

    async def counts():
        async with st.engine.connect() as conn:
            rows = (await conn.execute(select(jobs.c.status, func.count()).group_by(jobs.c.status))).all()
        return {r[0]: r[1] for r in rows}

    async def recent():
        q = select(jobs).order_by(desc(jobs.c.created_at)).limit(min(max(limit, 1), 200))
        async with st.engine.connect() as conn:
            rows = (await conn.execute(q)).all()
        return [{**job_view(r, s.max_attempts), "worker_id": r.worker_id, "publish_state": r.publish_state}
                for r in rows]

    workers, queue_depths, status_counts, recent_jobs, pg, mq, rd, s3 = await asyncio.gather(
        safe(_workers(st), []), safe(queues(), None), safe(counts(), {}), safe(recent(), []),
        _check(_pg(st)), _check(st.publisher.ping()), _check(st.redis.ping()), _check(_s3(st)),
    )
    return {
        "now": time.time(),
        "health": {"postgres": pg, "rabbitmq": mq, "redis": rd, "s3": s3},
        "workers": workers,
        "queues": queue_depths,
        "status_counts": status_counts,
        "jobs": recent_jobs,
        "settings": {k: getattr(s, k, None) for k in SAFE_SETTINGS},
    }


_minio = None


def _s3_client(st: AppState):
    global _minio
    if _minio is None:
        from minio import Minio  # imported lazily: only the debug panel needs it

        u = urlparse(st.settings.s3_endpoint if "://" in st.settings.s3_endpoint
                     else f"http://{st.settings.s3_endpoint}")
        _minio = Minio(u.netloc, access_key=st.settings.s3_access_key, secret_key=st.settings.s3_secret_key,
                       secure=u.scheme == "https")
    return _minio


@router.get("/frame")
async def frame(key: str = Query(..., max_length=300), st: AppState = Depends(get_state)):
    """Return one stored frame JPEG (keys look like jobs/{job_id}/frame_{t}.jpg)."""
    if not key.startswith("jobs/") or ".." in key:
        raise ApiError(400, ErrorCode.INVALID_REQUEST, "Invalid frame key.")

    def _get() -> bytes:
        resp = _s3_client(st).get_object(st.settings.s3_bucket, key)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    try:
        data = await asyncio.to_thread(_get)
    except Exception as e:  # noqa: BLE001
        raise ApiError(404, "FRAME_NOT_FOUND", f"Frame not found: {type(e).__name__}") from e
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})
