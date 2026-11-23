"""Queue message body and broker names (exchanges, queues, routing keys)."""

from __future__ import annotations

import re
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


DETAIL_WORDS = {"brief": 0.5, "standard": 1.0, "detailed": 1.7}
MAX_FRAMES_CAP = 12


class JobOptions(BaseModel):
    """Per-job preferences chosen in the extension. Every field is optional so
    old clients (and old queued messages) keep the server defaults."""

    model_config = ConfigDict(extra="ignore")

    detail: Literal["brief", "standard", "detailed"] = "standard"
    max_frames: int | None = Field(default=None, ge=0, le=MAX_FRAMES_CAP)
    study_aids: bool | None = None  # key takeaways, glossary, review questions
    include_transcript: bool | None = None  # collapsible full transcript on the page
    language: str | None = Field(default=None, max_length=40)  # None = the video's language

    @field_validator("language", mode="before")
    @classmethod
    def _lang(cls, v):
        if v is None:
            return None
        v = re.sub(r"[^\w\s()-]", "", str(v)).strip()
        return v or None

    def words_factor(self) -> float:
        return DETAIL_WORDS.get(self.detail, 1.0)


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
    options: JobOptions = Field(default_factory=JobOptions)

    def to_bytes(self) -> bytes:
        return self.model_dump_json().encode()

    @classmethod
    def from_bytes(cls, raw: bytes) -> StageMessage:
        return cls.model_validate_json(raw)

    def next(self, stage: Stage) -> StageMessage:
        return self.model_copy(update={"stage": stage, "attempt": 1, "deferred": False})

    def retry(self) -> StageMessage:
        return self.model_copy(update={"attempt": self.attempt + 1, "deferred": False})
