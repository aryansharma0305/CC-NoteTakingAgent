"""Async engine factory and the compare-and-set status transition used everywhere."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from .models import job_events, jobs
from .status import JobStatus, allowed_from


def make_engine(database_url: str, **kw: Any) -> AsyncEngine:
    kw.setdefault("pool_pre_ping", True)
    kw.setdefault("pool_size", 5)
    kw.setdefault("max_overflow", 5)
    return create_async_engine(database_url, **kw)


async def cas_transition(
    conn: AsyncConnection,
    job_id: uuid.UUID,
    to: JobStatus,
    *,
    worker_id: str | None = None,
    detail: dict[str, Any] | None = None,
    allowed: list[JobStatus] | None = None,
    **fields: Any,
) -> bool:
    """UPDATE jobs SET status=:to ... WHERE id=:id AND status = ANY(:allowed_from).

    Returns False when zero rows matched, meaning another worker already moved
    the job (the current message is a duplicate). Inserts a job_events row on
    success. Must run inside the caller's transaction.
    """
    allowed = allowed if allowed is not None else allowed_from(to)
    current = (
        await conn.execute(select(jobs.c.status).where(jobs.c.id == job_id))
    ).scalar_one_or_none()
    values: dict[str, Any] = {"status": str(to), **fields}
    if worker_id is not None:
        values["worker_id"] = worker_id
    res = await conn.execute(
        update(jobs)
        .where(jobs.c.id == job_id, jobs.c.status.in_([str(s) for s in allowed]))
        .values(**values)
        .returning(jobs.c.id)
    )
    if res.first() is None:
        return False
    await conn.execute(
        insert(job_events).values(
            job_id=job_id, from_status=current, to_status=str(to), detail=detail, worker_id=worker_id
        )
    )
    return True


async def add_event(
    conn: AsyncConnection,
    job_id: uuid.UUID,
    detail: dict[str, Any],
    *,
    worker_id: str | None = None,
    status: str | None = None,
) -> None:
    await conn.execute(
        insert(job_events).values(
            job_id=job_id, from_status=status, to_status=status, detail=detail, worker_id=worker_id
        )
    )
