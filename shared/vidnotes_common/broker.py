"""RabbitMQ topology, declared idempotently by both gateway and workers.

infra/rabbitmq/definitions.json declares the same static objects at broker
boot; the arguments here MUST match it exactly (RabbitMQ rejects a redeclare
with different arguments). Retry queues are only declared here, from
RETRY_DELAYS, so changing the env var needs no definitions edit.
"""

from __future__ import annotations

from collections.abc import Iterable

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractExchange

from .messages import (
    DLQ_QUEUE,
    DLX_EXCHANGE,
    JOBS_EXCHANGE,
    WORK_DELIVERY_LIMIT,
    WORK_QUEUE,
    WORK_ROUTING_KEY,
    retry_queue_name,
)

WORK_QUEUE_ARGS = {
    "x-queue-type": "quorum",
    "x-delivery-limit": WORK_DELIVERY_LIMIT,
    "x-dead-letter-exchange": DLX_EXCHANGE,
    "x-dead-letter-strategy": "at-least-once",
    "x-overflow": "reject-publish",
}

DLQ_ARGS = {
    "x-queue-type": "quorum",
    "x-delivery-limit": -1,  # never drop inspected DLQ messages
}


def retry_queue_args(delay_sec: int) -> dict:
    return {
        "x-queue-type": "quorum",
        "x-message-ttl": int(delay_sec) * 1000,
        "x-dead-letter-exchange": JOBS_EXCHANGE,
        "x-dead-letter-routing-key": WORK_ROUTING_KEY,
        "x-dead-letter-strategy": "at-least-once",
        "x-overflow": "reject-publish",
    }


async def declare_topology(
    channel: AbstractChannel, retry_delays: Iterable[int] = ()
) -> tuple[AbstractExchange, AbstractExchange]:
    jobs_x = await channel.declare_exchange(JOBS_EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True)
    dlx = await channel.declare_exchange(DLX_EXCHANGE, aio_pika.ExchangeType.FANOUT, durable=True)
    work = await channel.declare_queue(WORK_QUEUE, durable=True, arguments=WORK_QUEUE_ARGS)
    await work.bind(jobs_x, routing_key=WORK_ROUTING_KEY)
    dlq = await channel.declare_queue(DLQ_QUEUE, durable=True, arguments=DLQ_ARGS)
    await dlq.bind(dlx, routing_key="")
    for d in sorted(set(int(x) for x in retry_delays)):
        await channel.declare_queue(retry_queue_name(d), durable=True, arguments=retry_queue_args(d))
    return jobs_x, dlx
