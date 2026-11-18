"""initial schema: jobs, job_events, job_artifacts

Revision ID: 0001
Revises:
Create Date: 2026-11-18
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("video_id", sa.String(16), nullable=False),
        sa.Column("youtube_url", sa.Text, nullable=False),
        sa.Column("video_title", sa.Text),
        sa.Column("duration_sec", sa.Integer),
        sa.Column("status", sa.String(32), nullable=False, server_default="QUEUED"),
        sa.Column("progress", sa.Integer, nullable=False, server_default="0"),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
        sa.Column("waiting_until", sa.DateTime(timezone=True)),
        sa.Column("retry_reason", sa.Text),
        sa.Column("worker_id", sa.String(128)),
        sa.Column("notion_parent_id", sa.String(64)),
        sa.Column("notion_page_id", sa.String(64)),
        sa.Column("notion_page_url", sa.Text),
        sa.Column("publish_state", sa.String(16)),
        sa.Column("publish_cursor", sa.Integer, nullable=False, server_default="0"),
        sa.Column("frames_requested", sa.Integer),
        sa.Column("frames_published", sa.Integer),
        sa.Column("warnings", pg.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("error", pg.JSONB),
        sa.Column("dispatch_pending", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("trace_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_created_at", "jobs", ["created_at"])

    op.create_table(
        "job_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("job_id", pg.UUID(as_uuid=True), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_status", sa.String(32)),
        sa.Column("to_status", sa.String(32)),
        sa.Column("detail", pg.JSONB),
        sa.Column("worker_id", sa.String(128)),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_job_events_job_id", "job_events", ["job_id", "id"])

    op.create_table(
        "job_artifacts",
        sa.Column("job_id", pg.UUID(as_uuid=True), sa.ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("stage", sa.String(32), primary_key=True),
        sa.Column("data", pg.JSONB, nullable=False),
        sa.Column("object_keys", pg.ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("job_artifacts")
    op.drop_table("job_events")
    op.drop_table("jobs")
