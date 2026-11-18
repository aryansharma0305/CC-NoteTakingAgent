"""Demo/ops endpoints, protected by the X-Admin-Token header."""

from __future__ import annotations

import hmac
import uuid

from fastapi import APIRouter, Depends, Header
from sqlalchemy import desc, select

from vidnotes_common.db import cas_transition
from vidnotes_common.errors import ErrorCode
from vidnotes_common.models import job_artifacts, job_events, jobs
from vidnotes_common.status import STAGE_STATUS, JobStatus, stage_start_progress

from .errors import ApiError
from .routes_jobs import job_view, load_job
from .state import AppState, get_state


def require_admin(x_admin_token: str = Header(default=""), st: AppState = Depends(get_state)) -> None:
    if not hmac.compare_digest(x_admin_token, st.settings.admin_token):
        raise ApiError(401, ErrorCode.UNAUTHORIZED)


router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


@router.get("/jobs")
async def list_jobs(limit: int = 50, status: str | None = None, st: AppState = Depends(get_state)):
    q = select(jobs).order_by(desc(jobs.c.created_at)).limit(min(max(limit, 1), 500))
    if status:
        q = q.where(jobs.c.status == status.upper())
    async with st.engine.connect() as conn:
        rows = (await conn.execute(q)).all()
    return {"jobs": [{**job_view(r, st.settings.max_attempts), "worker_id": r.worker_id,
                      "notion_page_id": r.notion_page_id, "publish_state": r.publish_state} for r in rows]}


@router.get("/jobs/{job_id}")
async def job_detail(job_id: str, st: AppState = Depends(get_state)):
    row = await load_job(st, job_id)
    async with st.engine.connect() as conn:
        events = (
            await conn.execute(select(job_events).where(job_events.c.job_id == row.id).order_by(job_events.c.id))
        ).all()
    return {
        **job_view(row, st.settings.max_attempts),
        "worker_id": row.worker_id,
        "notion_page_id": row.notion_page_id,
        "publish_state": row.publish_state,
        "publish_cursor": row.publish_cursor,
        "events": [
            {"at": e.at.isoformat(), "from": e.from_status, "to": e.to_status, "worker_id": e.worker_id,
             "detail": e.detail}
            for e in events
        ],
    }


@router.get("/jobs/{job_id}/artifacts/{stage}")
async def job_artifact(job_id: str, stage: str, st: AppState = Depends(get_state)):
    """Stage outputs (transcript | synthesis | frames) for debugging and the constraint checker."""
    row = await load_job(st, job_id)
    async with st.engine.connect() as conn:
        data = (
            await conn.execute(
                select(job_artifacts.c.data, job_artifacts.c.object_keys)
                .where(job_artifacts.c.job_id == row.id, job_artifacts.c.stage == stage)
            )
        ).first()
    if data is None:
        raise ApiError(404, "ARTIFACT_NOT_FOUND", f"No '{stage}' artifact for this job.")
    return {"job_id": str(row.id), "stage": stage, "data": data.data, "object_keys": list(data.object_keys or [])}


@router.get("/dlq")
async def dlq(st: AppState = Depends(get_state)):
    async with st.engine.connect() as conn:
        rows = (
            await conn.execute(
                select(jobs).where(jobs.c.status == JobStatus.DEAD_LETTERED.value).order_by(desc(jobs.c.updated_at))
                .limit(100)
            )
        ).all()
    return {
        "queues": await st.publisher.queue_depths(),
        "dead_lettered_jobs": [{**job_view(r, st.settings.max_attempts), "worker_id": r.worker_id} for r in rows],
    }


@router.post("/dlq/{job_id}/requeue")
async def requeue(job_id: str, st: AppState = Depends(get_state)):
    row = await load_job(st, job_id)
    if row.status != JobStatus.DEAD_LETTERED.value:
        raise ApiError(409, ErrorCode.NOT_IN_DLQ, f"Job is {row.status}, not DEAD_LETTERED.")
    found = await st.publisher.take_from_dlq(uuid.UUID(str(row.id)))
    if found is None:
        raise ApiError(404, ErrorCode.NOT_IN_DLQ, "No message for this job in vn.dlq.")
    msg, headers, ack, give_back = found
    to = STAGE_STATUS[msg.stage]
    try:
        async with st.engine.begin() as conn:
            ok = await cas_transition(
                conn, row.id, to, worker_id="admin", allowed=[JobStatus.DEAD_LETTERED],
                detail={"event": "requeued_from_dlq", "stage": str(msg.stage)},
                attempt=1, error=None, waiting_until=None, retry_reason=None, dispatch_pending=False,
                progress=stage_start_progress(msg.stage),
            )
            if not ok:
                raise ApiError(409, ErrorCode.NOT_IN_DLQ)
            fresh = msg.model_copy(update={"attempt": 1, "deferred": False})
            headers = {k: v for k, v in headers.items() if not str(k).startswith("x-vn-") and k != "x-death"}
            await st.publisher.publish(fresh, headers)
    except Exception:
        await give_back()
        raise
    await ack()
    return {"job_id": str(row.id), "status": to.value, "stage": str(msg.stage)}
