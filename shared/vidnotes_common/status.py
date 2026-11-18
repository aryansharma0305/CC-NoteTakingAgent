"""Job status enum, pipeline stages, allowed transitions and progress ranges.

Status only ever moves forward. A stage's status means "this stage is the
current one": TRANSCRIBING is set when the job is claimed, and on success the
worker moves the job straight to the *next* stage's status (the spec's
"TRANSCRIBING -> SUMMARIZING: transcript obtained"), then publishes the next
stage message.
"""

from __future__ import annotations

from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    TRANSCRIBING = "TRANSCRIBING"
    SUMMARIZING = "SUMMARIZING"
    EXTRACTING_FRAMES = "EXTRACTING_FRAMES"
    PUBLISHING = "PUBLISHING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_WARNINGS = "COMPLETED_WITH_WARNINGS"
    FAILED = "FAILED"
    DEAD_LETTERED = "DEAD_LETTERED"


class Stage(StrEnum):
    TRANSCRIBE = "TRANSCRIBE"
    SUMMARIZE = "SUMMARIZE"
    EXTRACT_FRAMES = "EXTRACT_FRAMES"
    PUBLISH = "PUBLISH"


STAGE_ORDER: list[Stage] = [Stage.TRANSCRIBE, Stage.SUMMARIZE, Stage.EXTRACT_FRAMES, Stage.PUBLISH]

STAGE_STATUS: dict[Stage, JobStatus] = {
    Stage.TRANSCRIBE: JobStatus.TRANSCRIBING,
    Stage.SUMMARIZE: JobStatus.SUMMARIZING,
    Stage.EXTRACT_FRAMES: JobStatus.EXTRACTING_FRAMES,
    Stage.PUBLISH: JobStatus.PUBLISHING,
}

STATUS_STAGE: dict[JobStatus, Stage] = {v: k for k, v in STAGE_STATUS.items()}

TERMINAL: frozenset[JobStatus] = frozenset(
    {
        JobStatus.COMPLETED,
        JobStatus.COMPLETED_WITH_WARNINGS,
        JobStatus.FAILED,
        JobStatus.DEAD_LETTERED,
    }
)

ACTIVE: frozenset[JobStatus] = frozenset(STAGE_STATUS.values())

# Linear order used for "has the job already passed this stage?" checks.
STATUS_RANK: dict[JobStatus, int] = {
    JobStatus.QUEUED: 0,
    JobStatus.TRANSCRIBING: 1,
    JobStatus.SUMMARIZING: 2,
    JobStatus.EXTRACTING_FRAMES: 3,
    JobStatus.PUBLISHING: 4,
    JobStatus.COMPLETED: 5,
    JobStatus.COMPLETED_WITH_WARNINGS: 5,
    JobStatus.FAILED: 5,
    JobStatus.DEAD_LETTERED: 5,
}

# Progress (percent) at the start and end of each stage.
PROGRESS_RANGE: dict[JobStatus, tuple[int, int]] = {
    JobStatus.QUEUED: (0, 0),
    JobStatus.TRANSCRIBING: (5, 25),
    JobStatus.SUMMARIZING: (25, 55),
    JobStatus.EXTRACTING_FRAMES: (55, 80),
    JobStatus.PUBLISHING: (80, 100),
}

# Allowed transitions. Any non-terminal status may also go to FAILED or
# DEAD_LETTERED; DEAD_LETTERED may be requeued by an admin into an active stage.
ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.TRANSCRIBING}),
    JobStatus.TRANSCRIBING: frozenset({JobStatus.SUMMARIZING}),
    JobStatus.SUMMARIZING: frozenset({JobStatus.EXTRACTING_FRAMES}),
    JobStatus.EXTRACTING_FRAMES: frozenset({JobStatus.PUBLISHING}),
    JobStatus.PUBLISHING: frozenset({JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNINGS}),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.COMPLETED_WITH_WARNINGS: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.DEAD_LETTERED: ACTIVE,
}
for _s in [JobStatus.QUEUED, *ACTIVE]:
    ALLOWED_TRANSITIONS[_s] = ALLOWED_TRANSITIONS[_s] | {JobStatus.FAILED, JobStatus.DEAD_LETTERED}


def can_transition(frm: JobStatus, to: JobStatus) -> bool:
    return to in ALLOWED_TRANSITIONS.get(frm, frozenset())


def allowed_from(to: JobStatus) -> list[JobStatus]:
    """Every status from which `to` is reachable (used by the CAS update)."""
    return [s for s, targets in ALLOWED_TRANSITIONS.items() if to in targets]


def next_stage(stage: Stage) -> Stage | None:
    i = STAGE_ORDER.index(stage)
    return STAGE_ORDER[i + 1] if i + 1 < len(STAGE_ORDER) else None


def stage_start_progress(stage: Stage) -> int:
    return PROGRESS_RANGE[STAGE_STATUS[stage]][0]


def stage_progress(stage: Stage, fraction: float) -> int:
    lo, hi = PROGRESS_RANGE[STAGE_STATUS[stage]]
    fraction = min(max(fraction, 0.0), 1.0)
    return int(round(lo + (hi - lo) * fraction))


def is_terminal(status: JobStatus | str) -> bool:
    return JobStatus(status) in TERMINAL
