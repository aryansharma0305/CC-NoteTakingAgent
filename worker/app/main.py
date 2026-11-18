"""Worker entry point: `python -m app.main`."""

from __future__ import annotations

import asyncio
import signal
import socket

import aio_pika
from redis.asyncio import Redis

from vidnotes_common.broker import declare_topology
from vidnotes_common.config import get_settings
from vidnotes_common.crypto import TokenCipher
from vidnotes_common.db import make_engine
from vidnotes_common.logs import configure_logging, get_logger
from vidnotes_common.messages import WORK_QUEUE

from .consumer import consume
from .context import WorkerContext
from .llm import make_llm
from .ratelimit import Limiter
from .storage import Storage

log = get_logger("worker")


async def _connect_with_retry(url: str, name: str):
    for attempt in range(30):
        try:
            # heartbeat=30: a hung (not dead) worker is detected in ~60 s.
            return await aio_pika.connect_robust(url, heartbeat=30, client_properties={"connection_name": name})
        except Exception as e:  # noqa: BLE001
            log.warning("rabbitmq_connect_retry", attempt=attempt, error=str(e))
            await asyncio.sleep(2)
    raise RuntimeError("could not connect to RabbitMQ")


async def main() -> None:
    s = get_settings()
    configure_logging(s.log_level)
    worker_id = socket.gethostname()

    engine = make_engine(s.database_url)
    connection = await _connect_with_retry(s.rabbitmq_url, f"worker-{worker_id}")
    channel = await connection.channel(publisher_confirms=True)
    await channel.set_qos(prefetch_count=1)
    jobs_x, dlx = await declare_topology(channel, s.retry_delays)
    queue = await channel.get_queue(WORK_QUEUE)

    redis = Redis.from_url(s.redis_url)
    storage = Storage(s.s3_endpoint, s.s3_access_key, s.s3_secret_key, s.s3_bucket)
    await storage.ensure_bucket()
    llm = make_llm(s)

    ctx = WorkerContext(
        settings=s, engine=engine, channel=channel, jobs_exchange=jobs_x, dlx=dlx,
        limiter=Limiter(redis, s), storage=storage, llm=llm, cipher=TokenCipher(s.token_enc_key),
        worker_id=worker_id,
    )
    log.info("worker_started", worker_id=worker_id, llm=f"{s.llm_provider}:{s.llm_model}",
             retry_delays=s.retry_delays, max_attempts=s.max_attempts)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows dev runs
            pass

    task = asyncio.create_task(consume(ctx, queue))
    stop_task = asyncio.create_task(stop.wait())
    await asyncio.wait({task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    log.info("worker_stopping", worker_id=worker_id)
    task.cancel()
    # Closing without acking returns any in-flight message to the queue.
    await connection.close()
    await llm.aclose()
    await redis.aclose()
    await engine.dispose()
    if task.done() and not task.cancelled() and task.exception():
        raise task.exception()  # type: ignore[misc]


if __name__ == "__main__":
    asyncio.run(main())
