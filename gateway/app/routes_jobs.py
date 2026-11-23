"""The three required endpoints: POST /jobs, GET /jobs/{id}, GET /jobs/{id}/results."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import insert, select

from vidnotes_common.errors import ErrorCode
from vidnotes_common.messages import JobOptions, StageMessage
from vidnotes_common.models import job_events, jobs
from vidnotes_common.status import TERMINAL, JobStatus, Stage
from vidnotes_common.youtube import parse_video_id

from . import youtube_meta
from .errors import ApiError
from .state import AppState, get_state

router = APIRouter()


class SubmitJob(BaseModel):
    youtube_url: str
    notion_token: str | None = None
    notion_parent_id: str | None = None
    # Optional per-job preferences (detail level, frame count, study aids, transcript, language).
    options: JobOptions | None = None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _result(row) -> dict[str, Any] | None:
    if row.status in (JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNINGS) and row.notion_page_url:
        return {
            "notion_page_url": row.notion_page_url,
            "frames_requested": row.frames_requested or 0,
            "frames_published": row.frames_published or 0,
        }
    return None


def job_view(row, max_attempts: int) -> dict[str, Any]:
    return {
        "job_id": str(row.id),
        "status": row.status,
        "progress": row.progress,
        "video_id": row.video_id,
        "video_title": row.video_title,
        "duration_sec": row.duration_sec,
        "warnings": row.warnings or [],
        "error": row.error,
        "result": _result(row),
        "waiting_until": _iso(row.waiting_until),
        "retry_reason": row.retry_reason,
        "attempt": row.attempt,
        "max_attempts": max_attempts,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


async def load_job(st: AppState, job_id: str):
    try:
        jid = uuid.UUID(job_id)
    except ValueError:
        raise ApiError(404, ErrorCode.JOB_NOT_FOUND) from None
    async with st.engine.connect() as conn:  # pure SELECT: polling never mutates state
        row = (await conn.execute(select(jobs).where(jobs.c.id == jid))).first()
    if row is None:
        raise ApiError(404, ErrorCode.JOB_NOT_FOUND)
    return row


@router.post("/jobs", status_code=202)
async def submit_job(body: SubmitJob, st: AppState = Depends(get_state)):
    s = st.settings
    video_id = parse_video_id(body.youtube_url)
    if not video_id:
        raise ApiError(400, ErrorCode.INVALID_URL)
    token = (body.notion_token or "").strip()
    if not token:
        raise ApiError(401, ErrorCode.MISSING_NOTION_TOKEN)

    # Constraint A at submission time (3 s budget; on timeout the worker enforces it).
    meta = await youtube_meta.lookup(video_id, s, st.http)
    if meta and meta.is_live:
        raise ApiError(422, ErrorCode.LIVE_STREAM)
    if meta and meta.duration_sec and meta.duration_sec > s.max_duration_sec:
        raise ApiError(
            422,
            ErrorCode.VIDEO_TOO_LONG,
            f"This video is {round(meta.duration_sec / 60)} min; the limit is {s.max_duration_sec // 60} min.",
            duration_sec=meta.duration_sec,
            limit_sec=s.max_duration_sec,
            video_title=meta.title,
        )

    job_id = uuid.uuid4()
    msg = StageMessage(
        job_id=job_id,
        stage=Stage.TRANSCRIBE,
        enc_notion_token=st.cipher.encrypt(token),
        notion_parent_id=(body.notion_parent_id or "").strip() or None,
        options=body.options or JobOptions(),
    )
    canonical_url = f"https://www.youtube.com/watch?v={video_id}"
    # Insert and publish in one transaction: if the broker is down the row rolls back.
    try:
        async with st.engine.begin() as conn:
            await conn.execute(
                insert(jobs).values(
                    id=job_id,
                    video_id=video_id,
                    youtube_url=canonical_url,
                    video_title=meta.title if meta else None,
                    duration_sec=meta.duration_sec if meta else None,
                    status=JobStatus.QUEUED.value,
                    progress=0,
                    attempt=1,
                    notion_parent_id=msg.notion_parent_id,
                    trace_id=msg.trace_id,
                    warnings=[],
                )
            )
            await conn.execute(
                insert(job_events).values(
                    job_id=job_id, from_status=None, to_status=JobStatus.QUEUED.value,
                    detail={"event": "submitted", "metadata_source": meta.source if meta else None,
                            "options": msg.options.model_dump(exclude_none=True)},
                    worker_id="gateway",
                )
            )
            await st.publisher.publish(msg)
    except ApiError:
        raise
    except Exception as e:  # broker unreachable / confirm timeout
        raise ApiError(503, ErrorCode.BROKER_UNAVAILABLE, detail=type(e).__name__) from e

    return JSONResponse(
        status_code=202,
        headers={"Location": f"/jobs/{job_id}"},
        content={
            "job_id": str(job_id),
            "status": JobStatus.QUEUED.value,
            "video_id": video_id,
            "video_title": meta.title if meta else None,
            "duration_sec": meta.duration_sec if meta else None,
        },
    )


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, st: AppState = Depends(get_state)):
    row = await load_job(st, job_id)
    return job_view(row, st.settings.max_attempts)


@router.get("/jobs/{job_id}/results")
async def get_results(job_id: str, st: AppState = Depends(get_state)):
    row = await load_job(st, job_id)
    if JobStatus(row.status) not in TERMINAL:
        raise ApiError(409, ErrorCode.JOB_NOT_FINISHED, f"Job is {row.status}; results are available once it finishes.",
                       status=row.status, progress=row.progress)
    return {
        "status": row.status,
        "result": _result(row),
        "warnings": row.warnings or [],
        "error": row.error,
    }
