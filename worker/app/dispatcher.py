"""Handle one stage message: lock -> dedupe -> claim -> run -> commit -> publish next.

The caller acks only after handle() returns, so a crash anywhere in here leaves
the message unacked and RabbitMQ redelivers it to another worker. Every branch
below is written so that redelivery is harmless:

* A per-job Postgres advisory lock means only one worker touches a job at once.
  If the lock is busy the message is parked briefly (no attempt consumed).
* Status only moves forward via compare-and-set. A message for a stage the job
  has already passed is a duplicate and is dropped -- unless the job is flagged
  dispatch_pending, which means the previous worker committed the stage but
  died before the next-stage message was confirmed; then we re-publish it.
* A stage whose artifact already exists is not run again (completion check
  reads job_artifacts, not just status).
"""

from __future__ import annotations

import time
from typing import Any

import aio_pika
from sqlalchemy import update

from vidnotes_common.db import add_event, cas_transition
from vidnotes_common.errors import ErrorCode, FatalError, PipelineError, RetryableError
from vidnotes_common.logs import get_logger
from vidnotes_common.messages import HDR_FIRST_ENQUEUED, WORK_ROUTING_KEY, StageMessage
from vidnotes_common.models import jobs
from vidnotes_common.status import (
    STAGE_STATUS,
    STATUS_RANK,
    TERMINAL,
    JobStatus,
    Stage,
    next_stage,
    stage_start_progress,
)

from . import repo
from .context import WorkerContext
from .retry import defer, handle_retryable
from .stages import ARTIFACT_NAME, StageResult
from .stages import frames as frames_stage
from .stages import publish as publish_stage
from .stages import summarize as summarize_stage
from .stages import transcribe as transcribe_stage

log = get_logger("worker.dispatch")

RUNNERS = {
    Stage.TRANSCRIBE: transcribe_stage.run,
    Stage.SUMMARIZE: summarize_stage.run,
    Stage.EXTRACT_FRAMES: frames_stage.run,
    Stage.PUBLISH: publish_stage.run,
}


async def handle(ctx: WorkerContext, msg: StageMessage, headers: dict[str, Any] | None = None) -> None:
    async with ctx.engine.connect() as raw:
        lockconn = await raw.execution_options(isolation_level="AUTOCOMMIT")
        if not await repo.try_lock(lockconn, msg.job_id):
            log.info("job_locked_deferring", job_id=str(msg.job_id), stage=str(msg.stage))
            await defer(ctx, msg, headers)
            return
        try:
            await _handle_locked(ctx, msg, headers)
        finally:
            await repo.unlock(lockconn, msg.job_id)


async def publish_next(ctx: WorkerContext, msg: StageMessage, stage: Stage, headers: dict[str, Any] | None) -> None:
    h = {HDR_FIRST_ENQUEUED: (headers or {}).get(HDR_FIRST_ENQUEUED, int(time.time()))}
    await ctx.jobs_exchange.publish(
        aio_pika.Message(
            body=msg.next(stage).to_bytes(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            headers=h,
        ),
        routing_key=WORK_ROUTING_KEY,
    )


async def _handle_locked(ctx: WorkerContext, msg: StageMessage, headers: dict[str, Any] | None) -> None:
    job = await repo.load_job(ctx.engine, msg.job_id)
    if job is None:
        log.warning("unknown_job_dropped", job_id=str(msg.job_id))
        return
    status = JobStatus(job.status)
    stage_status = STAGE_STATUS[msg.stage]

    if status in TERMINAL:
        log.info("duplicate_terminal_dropped", job_id=str(job.id), stage=str(msg.stage), status=str(status))
        return
    if STATUS_RANK[status] > STATUS_RANK[stage_status]:
        nxt = next_stage(msg.stage)
        if job.dispatch_pending and nxt is not None and STAGE_STATUS[nxt] == status:
            await publish_next(ctx, msg, nxt, headers)
            await repo.update_job(ctx.engine, job.id, dispatch_pending=False)
            log.warning("redispatched_next_stage", job_id=str(job.id), stage=str(nxt))
        else:
            log.info("duplicate_stage_dropped", job_id=str(job.id), stage=str(msg.stage), status=str(status))
        return
    claimable = {stage_status} | ({JobStatus.QUEUED} if msg.stage == Stage.TRANSCRIBE else set())
    if status not in claimable:
        log.warning("out_of_order_dropped", job_id=str(job.id), stage=str(msg.stage), status=str(status))
        return

    # ---- claim ------------------------------------------------------------
    claim_fields = dict(attempt=msg.attempt, waiting_until=None, retry_reason=None, dispatch_pending=False)
    detail = {"event": "claimed", "stage": str(msg.stage), "attempt": msg.attempt, "deferred": msg.deferred}
    async with ctx.engine.begin() as conn:
        if status == JobStatus.QUEUED:
            ok = await cas_transition(
                conn, job.id, JobStatus.TRANSCRIBING, worker_id=ctx.worker_id, allowed=[JobStatus.QUEUED],
                detail=detail, progress=stage_start_progress(Stage.TRANSCRIBE), **claim_fields,
            )
            if not ok:
                return
        else:
            await conn.execute(
                update(jobs).where(jobs.c.id == job.id, jobs.c.status == str(stage_status))
                .values(worker_id=ctx.worker_id, **claim_fields)
            )
            await add_event(conn, job.id, detail, worker_id=ctx.worker_id, status=str(stage_status))
    log.info("stage_started", job_id=str(job.id), stage=str(msg.stage), attempt=msg.attempt)

    # ---- run (or skip if the output is already durable) ----------------------
    art_name = ARTIFACT_NAME.get(str(msg.stage))
    started = time.monotonic()
    try:
        if art_name and await repo.get_artifact(ctx.engine, job.id, art_name) is not None:
            log.info("stage_output_exists_skipping", job_id=str(job.id), stage=str(msg.stage))
            result = None
        else:
            result = await RUNNERS[msg.stage](ctx, job, msg)
    except FatalError as e:
        await fail(ctx, msg, e)
        return
    except RetryableError as e:
        await handle_retryable(ctx, msg, e, headers)
        return
    except PipelineError as e:  # pragma: no cover - base class used directly
        await handle_retryable(ctx, msg, RetryableError(e.code, e.message), headers)
        return
    except Exception as e:  # noqa: BLE001 - unknown bug: retry, then DLQ
        log.exception("stage_crashed", job_id=str(job.id), stage=str(msg.stage))
        await handle_retryable(
            ctx, msg, RetryableError(ErrorCode.UNEXPECTED_ERROR, f"{type(e).__name__}: {e}"[:300]), headers
        )
        return

    await complete(ctx, msg, result, headers, elapsed=time.monotonic() - started)


async def complete(
    ctx: WorkerContext, msg: StageMessage, result: StageResult | None, headers: dict[str, Any] | None,
    *, elapsed: float = 0.0,
) -> None:
    stage_status = STAGE_STATUS[msg.stage]
    fields = dict(result.job_fields) if result else {}
    detail = {"event": "stage_done", "stage": str(msg.stage), "seconds": round(elapsed, 2)}

    if result is not None and result.final_status is not None:
        async with ctx.engine.begin() as conn:
            await cas_transition(
                conn, msg.job_id, result.final_status, worker_id=ctx.worker_id, allowed=[stage_status],
                detail={**detail, "warnings": len(fields.get("warnings", []))},
                progress=100, waiting_until=None, retry_reason=None, **fields,
            )
        log.info("job_completed", job_id=str(msg.job_id), status=str(result.final_status))
        return

    nxt = next_stage(msg.stage)
    assert nxt is not None, "only the publish stage may finish a job"
    async with ctx.engine.begin() as conn:
        if result is not None and result.artifact is not None:
            await repo.put_artifact(conn, msg.job_id, ARTIFACT_NAME[str(msg.stage)], result.artifact,
                                    result.object_keys)
        ok = await cas_transition(
            conn, msg.job_id, STAGE_STATUS[nxt], worker_id=ctx.worker_id, allowed=[stage_status], detail=detail,
            progress=stage_start_progress(nxt), dispatch_pending=True, waiting_until=None, retry_reason=None,
            **fields,
        )
    if not ok:
        log.warning("advance_lost_race", job_id=str(msg.job_id), stage=str(msg.stage))
        return
    await publish_next(ctx, msg, nxt, headers)  # publisher confirms: returns once the broker has it
    await repo.update_job(ctx.engine, msg.job_id, dispatch_pending=False)
    log.info("stage_completed", job_id=str(msg.job_id), stage=str(msg.stage), next=str(nxt),
             seconds=round(elapsed, 2))


async def fail(ctx: WorkerContext, msg: StageMessage, err: FatalError) -> None:
    error = {**err.to_dict(), "stage": str(msg.stage)}
    async with ctx.engine.begin() as conn:
        await cas_transition(
            conn, msg.job_id, JobStatus.FAILED, worker_id=ctx.worker_id,
            detail={"event": "failed", "error": error}, error=error, waiting_until=None, retry_reason=None,
        )
    log.error("job_failed", job_id=str(msg.job_id), stage=str(msg.stage), code=err.code)
