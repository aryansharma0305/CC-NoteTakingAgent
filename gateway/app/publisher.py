"""Gateway-side RabbitMQ access: publish stage messages (with publisher confirms),
inspect queue depths, and pull a job's message back out of the DLQ."""

from __future__ import annotations

import asyncio
import time
import uuid

import aio_pika
from aio_pika.abc import AbstractRobustConnection

from vidnotes_common.broker import declare_topology
from vidnotes_common.config import Settings
from vidnotes_common.messages import (
    DLQ_QUEUE,
    HDR_FIRST_ENQUEUED,
    WORK_QUEUE,
    WORK_ROUTING_KEY,
    StageMessage,
    retry_queue_name,
)


class Publisher:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.connection: AbstractRobustConnection | None = None
        self._channel = None
        self._exchange = None
        self._lock = asyncio.Lock()

    async def connect(self, attempts: int = 30) -> None:
        last: Exception | None = None
        for _ in range(attempts):
            try:
                self.connection = await aio_pika.connect_robust(
                    self.settings.rabbitmq_url, heartbeat=30, client_properties={"connection_name": "gateway"}
                )
                self._channel = await self.connection.channel(publisher_confirms=True)
                self._exchange, _ = await declare_topology(self._channel, self.settings.retry_delays)
                return
            except Exception as e:  # noqa: BLE001
                last = e
                await asyncio.sleep(2)
        raise RuntimeError(f"could not connect to RabbitMQ: {last}")

    async def close(self) -> None:
        if self.connection:
            await self.connection.close()

    @property
    def ready(self) -> bool:
        return bool(self.connection and not self.connection.is_closed and self._exchange)

    async def publish(self, msg: StageMessage, headers: dict | None = None) -> None:
        if not self.ready:
            raise RuntimeError("broker not connected")
        h = dict(headers or {})
        h.setdefault(HDR_FIRST_ENQUEUED, int(time.time()))
        async with self._lock:
            await self._exchange.publish(
                aio_pika.Message(
                    body=msg.to_bytes(),
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    headers=h,
                ),
                routing_key=WORK_ROUTING_KEY,
                timeout=5,
            )

    async def ping(self) -> None:
        if not self.ready:
            raise RuntimeError("not connected")
        ch = await self.connection.channel()
        try:
            await ch.get_queue(WORK_QUEUE, ensure=True)
        finally:
            await ch.close()

    async def queue_depths(self) -> dict[str, int]:
        out: dict[str, int] = {}
        ch = await self.connection.channel()
        try:
            names = [WORK_QUEUE, DLQ_QUEUE] + [retry_queue_name(d) for d in self.settings.retry_delays]
            for name in names:
                try:
                    q = await ch.declare_queue(name, passive=True)
                    out[name] = q.declaration_result.message_count
                except Exception:  # noqa: BLE001 - queue may not exist yet
                    out[name] = -1
                    await ch.close()
                    ch = await self.connection.channel()
        finally:
            if not ch.is_closed:
                await ch.close()
        return out

    async def take_from_dlq(self, job_id: uuid.UUID, scan_limit: int = 1000):
        """Find the job's message in vn.dlq.

        Returns (StageMessage, headers, ack, give_back) or None. Call ack() once the
        message has been re-published, or give_back() to leave it in the DLQ.
        Messages that don't match are returned to the queue immediately."""
        ch = await self.connection.channel()
        q = await ch.get_queue(DLQ_QUEUE)
        held = []
        found = None
        try:
            for _ in range(scan_limit):
                m = await q.get(no_ack=False, fail=False, timeout=5)
                if m is None:
                    break
                try:
                    sm = StageMessage.from_bytes(m.body)
                except ValueError:
                    held.append(m)
                    continue
                if found is None and sm.job_id == job_id:
                    found = (sm, dict(m.headers or {}), m)
                else:
                    held.append(m)
            for m in held:
                await m.nack(requeue=True)
        except Exception:
            await ch.close()
            raise
        if found is None:
            await ch.close()
            return None
        sm, headers, m = found

        async def ack() -> None:
            await m.ack()
            await ch.close()

        async def give_back() -> None:
            await m.nack(requeue=True)
            await ch.close()

        return sm, headers, ack, give_back
