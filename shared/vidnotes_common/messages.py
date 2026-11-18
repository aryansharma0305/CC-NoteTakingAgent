"""Queue message body and broker names (exchanges, queues, routing keys)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from .status import Stage

# Exchanges
JOBS_EXCHANGE = "vn.jobs"  # direct; routing key WORK_ROUTING_KEY -> vn.work
DLX_EXCHANGE = "vn.dlx"  # fanout -> vn.dlq

# Queues
WORK_QUEUE = "vn.work"
DLQ_QUEUE = "vn.dlq"
WORK_ROUTING_KEY = "work"

WORK_DELIVERY_LIMIT = 10

# Header carrying the time the job was first enqueued (epoch seconds).
HDR_FIRST_ENQUEUED = "x-first-enqueued-at"


def retry_queue_name(delay_sec: int) -> str:
    """Retry queues are named by their delay so changing RETRY_DELAYS never
    clashes with an already-declared queue that has a different TTL."""
    return f"vn.retry.{int(delay_sec)}s"


class StageMessage(BaseModel):
    """Body of every message on vn.work / vn.retry.* / vn.dlq (JSON, persistent)."""

    job_id: uuid.UUID
    stage: Stage
    attempt: int = Field(default=1, ge=1)
    enc_notion_token: str
    notion_parent_id: str | None = None
    trace_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    # True when this message re-enters a stage only because the per-job lock
    # was busy; such deferrals never consume the retry budget.
    deferred: bool = False

    def to_bytes(self) -> bytes:
        return self.model_dump_json().encode()

    @classmethod
    def from_bytes(cls, raw: bytes) -> StageMessage:
        return cls.model_validate_json(raw)

    def next(self, stage: Stage) -> StageMessage:
        return self.model_copy(update={"stage": stage, "attempt": 1, "deferred": False})

    def retry(self) -> StageMessage:
        return self.model_copy(update={"attempt": self.attempt + 1, "deferred": False})
