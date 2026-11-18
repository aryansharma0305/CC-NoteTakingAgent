"""SQLAlchemy models: jobs, job_events, job_artifacts.

The API reads only `jobs`. Stage outputs live in `job_artifacts` keyed by
(job_id, stage) so a re-run is an idempotent upsert. `job_events` is the audit
trail used for the demo timeline and recovery-time measurement.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    video_id: Mapped[str] = mapped_column(String(16), nullable=False)
    youtube_url: Mapped[str] = mapped_column(Text, nullable=False)
    video_title: Mapped[str | None] = mapped_column(Text)
    duration_sec: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="QUEUED")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    waiting_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_reason: Mapped[str | None] = mapped_column(Text)
    worker_id: Mapped[str | None] = mapped_column(String(128))
    notion_parent_id: Mapped[str | None] = mapped_column(String(64))
    notion_page_id: Mapped[str | None] = mapped_column(String(64))
    notion_page_url: Mapped[str | None] = mapped_column(Text)
    publish_state: Mapped[str | None] = mapped_column(String(16))  # null | creating | created | done
    publish_cursor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    frames_requested: Mapped[int | None] = mapped_column(Integer)
    frames_published: Mapped[int | None] = mapped_column(Integer)
    warnings: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # True between "stage committed" and "next-stage message confirmed by broker".
    dispatch_pending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_jobs_status", "status"),
        Index("ix_jobs_created_at", "created_at"),
    )


class JobEvent(Base):
    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    worker_id: Mapped[str | None] = mapped_column(String(128))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_job_events_job_id", "job_id", "id"),)


class JobArtifact(Base):
    __tablename__ = "job_artifacts"

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    stage: Mapped[str] = mapped_column(String(32), primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    object_keys: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


jobs = Job.__table__
job_events = JobEvent.__table__
job_artifacts = JobArtifact.__table__
