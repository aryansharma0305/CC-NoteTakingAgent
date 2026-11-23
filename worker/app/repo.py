"""Postgres access for the worker (SQLAlchemy Core on the async engine)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from vidnotes_common.models import job_artifacts, jobs


async def try_lock(conn: AsyncConnection, job_id: uuid.UUID) -> bool:
    """Session-level advisory lock: one worker per job at a time."""
    r = await conn.execute(text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": str(job_id)})
    return bool(r.scalar())


async def unlock(conn: AsyncConnection, job_id: uuid.UUID) -> None:
    await conn.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": str(job_id)})


async def load_job(engine: AsyncEngine, job_id: uuid.UUID) -> Row | None:
    async with engine.connect() as conn:
        return (await conn.execute(select(jobs).where(jobs.c.id == job_id))).first()


async def update_job(engine: AsyncEngine, job_id: uuid.UUID, **values: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(update(jobs).where(jobs.c.id == job_id).values(**values))


async def get_artifact(engine: AsyncEngine, job_id: uuid.UUID, stage: str) -> dict[str, Any] | None:
    async with engine.connect() as conn:
        r = await conn.execute(
            select(job_artifacts.c.data).where(job_artifacts.c.job_id == job_id, job_artifacts.c.stage == stage)
        )
        return r.scalar_one_or_none()


async def put_artifact(
    conn: AsyncConnection, job_id: uuid.UUID, stage: str, data: dict[str, Any], object_keys: list[str] | None = None
) -> None:
    """Idempotent upsert keyed on (job_id, stage)."""
    stmt = pg_insert(job_artifacts).values(job_id=job_id, stage=stage, data=data, object_keys=object_keys or [])
    stmt = stmt.on_conflict_do_update(
        index_elements=[job_artifacts.c.job_id, job_artifacts.c.stage],
        set_={"data": stmt.excluded.data, "object_keys": stmt.excluded.object_keys},
    )
    await conn.execute(stmt)


async def find_cached_artifact(
    engine: AsyncEngine, *, video_id: str, stage: str, cache_key: str, exclude_job: uuid.UUID
) -> tuple[uuid.UUID, dict[str, Any], Row] | None:
    """Newest artifact of `stage` from ANOTHER job on the same video whose data carries
    this cache_key. Returns (source job id, artifact data, source job row)."""
    q = (
        select(job_artifacts.c.job_id, job_artifacts.c.data, jobs)
        .join(jobs, jobs.c.id == job_artifacts.c.job_id)
        .where(
            jobs.c.video_id == video_id,
            jobs.c.id != exclude_job,
            job_artifacts.c.stage == stage,
            job_artifacts.c.data.contains({"cache_key": cache_key}),
        )
        .order_by(job_artifacts.c.created_at.desc())
        .limit(1)
    )
    async with engine.connect() as conn:
        r = (await conn.execute(q)).first()
    return (r.job_id, r.data, r) if r else None


class DBPublishStore:
    """notion_publisher.PublishStateStore backed by the jobs row."""

    def __init__(self, engine: AsyncEngine, job_id: uuid.UUID):
        self._engine = engine
        self._job_id = job_id

    async def mark_creating(self) -> None:
        await update_job(self._engine, self._job_id, publish_state="creating")

    async def mark_created(self, page_id: str, page_url: str, cursor: int) -> None:
        await update_job(
            self._engine, self._job_id,
            publish_state="created", notion_page_id=page_id, notion_page_url=page_url, publish_cursor=cursor,
        )

    async def set_cursor(self, cursor: int) -> None:
        await update_job(self._engine, self._job_id, publish_cursor=cursor)
