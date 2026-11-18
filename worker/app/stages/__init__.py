"""Pipeline stages. Each reads inputs from Postgres/S3 and returns a StageResult;
the dispatcher saves the artifact and advances the job in one transaction."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from vidnotes_common.errors import RateLimited
from vidnotes_common.status import JobStatus


@dataclass
class StageResult:
    artifact: dict[str, Any] | None = None
    object_keys: list[str] = field(default_factory=list)
    job_fields: dict[str, Any] = field(default_factory=dict)
    final_status: JobStatus | None = None  # set only by the publish stage


# Name of the job_artifacts row each stage produces.
ARTIFACT_NAME = {
    "TRANSCRIBE": "transcript",
    "SUMMARIZE": "synthesis",
    "EXTRACT_FRAMES": "frames",
}


def maybe_fault_429(settings, service: str) -> None:
    rate = settings.fault_429_rates().get(service, 0.0)
    if rate and random.random() < rate:
        raise RateLimited(service, retry_after=None, message=f"Rate-limited by {service.capitalize()} (injected fault)")
