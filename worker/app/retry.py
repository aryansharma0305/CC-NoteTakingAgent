"""Backoff and dead-lettering.

Retries are messages parked in a TTL queue (vn.retry.<delay>s) that dead-letters
back into vn.work when the timer expires. A Retry-After longer than the default
delay picks the next queue that is at least that long.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import aio_pika
from sqlalchemy import update

from vidnotes_common.db import add_event, cas_transition
from vidnotes_common.errors import ErrorCode, PipelineError, RateLimited
from vidnotes_common.logs import get_logger
from vidnotes_common.messages import HDR_FIRST_ENQUEUED, StageMessage, retry_queue_name
from vidnotes_common.models import jobs
from vidnotes_common.status import STAGE_STATUS, JobStatus

from .context import WorkerContext

log = get_logger("worker.retry")


def pick_delay(attempt: int, delays: list[int], retry_after: float | None = None) -> int:
    """attempt is the attempt that just failed (1-based)."""
    base = delays[min(max(attempt, 1) - 1, len(delays) - 1)]
    want = max(base, retry_after or 0)
    for d in delays:
        if d >= want:
            return d
    return delays[-1]


def _message(msg: StageMessage, headers: dict | None = None) -> aio_pika.Message:
    return aio_pika.Message(
        body=msg.to_bytes(),
        content_type="application/json",
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        headers=headers or {HDR_FIRST_ENQUEUED: int(time.time())},
    )


async def publish_to_retry(ctx: WorkerContext, msg: StageMessage, delay: int, headers: dict | None = None) -> None:
    await ctx.channel.default_exchange.publish(_message(msg, headers), routing_key=retry_queue_name(delay))


async def defer(ctx: WorkerContext, msg: StageMessage, headers: dict | None = None) -> None:
    """Job is locked by another worker: come back later without spending an attempt."""
    delay = ctx.settings.lock_busy_delay_sec or ctx.settings.retry_delays[0]
    delay = pick_delay(1, ctx.settings.retry_delays, delay)
    await publish_to_retry(ctx, msg.model_copy(update={"deferred": True}), delay, headers)


async def handle_retryable(ctx: WorkerContext, msg: StageMessage, err: PipelineError, headers: dict | None) -> None:
    s = ctx.settings
    stage_status = STAGE_STATUS[msg.stage]
    if msg.attempt >= s.max_attempts:
        await dead_letter(ctx, msg, err, headers)
        return
    retry_after = err.retry_after if isinstance(err, RateLimited) else None
    delay = pick_delay(msg.attempt, s.retry_delays, retry_after)
    nxt = msg.retry()
    await publish_to_retry(ctx, nxt, delay, headers)
    until = datetime.now(UTC) + timedelta(seconds=delay)
    async with ctx.engine.begin() as conn:
        await conn.execute(
            update(jobs)
            .where(jobs.c.id == msg.job_id, jobs.c.status == str(stage_status))
            .values(waiting_until=until, retry_reason=err.message[:300], attempt=nxt.attempt)
        )
        await add_event(
            conn, msg.job_id,
            {"event": "retry_scheduled", "stage": str(msg.stage), "attempt": msg.attempt, "delay_sec": delay,
             "error": err.to_dict()},
            worker_id=ctx.worker_id, status=str(stage_status),
        )
    log.warning("retry_scheduled", job_id=str(msg.job_id), stage=str(msg.stage), attempt=msg.attempt,
                delay=delay, code=err.code, reason=err.message)


async def dead_letter(ctx: WorkerContext, msg: StageMessage, err: PipelineError, headers: dict | None) -> None:
    h = dict(headers or {})
    h.update({"x-vn-error-code": err.code, "x-vn-stage": str(msg.stage), "x-vn-attempts": msg.attempt})
    await ctx.dlx.publish(_message(msg, h), routing_key="")
    error = {
        "code": ErrorCode.RETRIES_EXHAUSTED,
        "message": f"Gave up after {msg.attempt} attempts. Last error: {err.message}",
        "stage": str(msg.stage),
        "last_error": err.to_dict(),
        "attempts": msg.attempt,
    }
    async with ctx.engine.begin() as conn:
        await cas_transition(
            conn, msg.job_id, JobStatus.DEAD_LETTERED, worker_id=ctx.worker_id,
            detail={"event": "dead_lettered", "error": error},
            error=error, waiting_until=None, retry_reason=None,
        )
    log.error("dead_lettered", job_id=str(msg.job_id), stage=str(msg.stage), code=err.code)
