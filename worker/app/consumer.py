"""AMQP consume loop: prefetch_count=1, manual ack after the stage is durable."""

from __future__ import annotations

import asyncio

from aio_pika.abc import AbstractIncomingMessage, AbstractQueue
from pydantic import ValidationError

from vidnotes_common.errors import ErrorCode, RetryableError
from vidnotes_common.logs import get_logger
from vidnotes_common.messages import WORK_DELIVERY_LIMIT, StageMessage

from . import dispatcher
from .context import WorkerContext
from .retry import dead_letter

log = get_logger("worker.consumer")


async def process(ctx: WorkerContext, message: AbstractIncomingMessage) -> None:
    headers = dict(message.headers or {})
    try:
        msg = StageMessage.from_bytes(message.body)
    except (ValidationError, ValueError):
        log.error("poison_message_rejected", body=message.body[:200].decode(errors="replace"))
        await message.reject(requeue=False)  # -> vn.dlx -> vn.dlq
        return
    # Quorum queues count redeliveries. A message that keeps killing workers is
    # dead-lettered here on its last allowed delivery so the job row is marked too.
    deliveries = int(headers.get("x-delivery-count") or 0)
    if deliveries >= WORK_DELIVERY_LIMIT - 1:
        log.error("poison_job_dead_lettered", job_id=str(msg.job_id), deliveries=deliveries)
        await dead_letter(ctx, msg, RetryableError(ErrorCode.UNEXPECTED_ERROR,
                                                   f"Workers crashed {deliveries} times on this stage"), headers)
        await message.ack()
        return
    hb = ctx.heartbeat
    if hb:
        await hb.busy(str(msg.job_id), str(msg.stage))
    try:
        await dispatcher.handle(ctx, msg, headers, redelivered=bool(message.redelivered))
    except Exception as e:  # noqa: BLE001 - infra failure (DB/broker down): let RabbitMQ redeliver
        log.exception("handler_failed_requeue", job_id=str(msg.job_id), stage=str(msg.stage),
                      redelivered=message.redelivered)
        if hb:
            await hb.idle(error=f"{type(e).__name__}: {e}")
        await asyncio.sleep(2)
        await message.nack(requeue=True)
        return
    if hb:
        await hb.idle()
    await message.ack()


async def consume(ctx: WorkerContext, queue: AbstractQueue) -> None:
    async with queue.iterator() as it:
        async for message in it:
            await process(ctx, message)
