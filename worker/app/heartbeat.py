"""Publishes this worker's live status to Redis for the /debug panel.

Key: vn:worker:{worker_id} (JSON, expires after TTL_SEC so dead workers disappear)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from redis.asyncio import Redis

from vidnotes_common.logs import get_logger

log = get_logger("worker.heartbeat")

KEY_PREFIX = "vn:worker:"
INTERVAL_SEC = 5
TTL_SEC = 20


class Heartbeat:
    def __init__(self, redis: Redis, worker_id: str, info: dict[str, Any]):
        self.redis = redis
        self.key = f"{KEY_PREFIX}{worker_id}"
        self.status: dict[str, Any] = {
            "worker_id": worker_id,
            "started_at": time.time(),
            "state": "idle",
            "job_id": None,
            "stage": None,
            "stage_started_at": None,
            "handled": 0,
            "failed": 0,
            "last_error": None,
            "last_job_id": None,
            **info,
        }

    async def publish(self) -> None:
        self.status["last_seen"] = time.time()
        try:
            await self.redis.set(self.key, json.dumps(self.status, default=str), ex=TTL_SEC)
        except Exception as e:  # noqa: BLE001 - debug info must never break the worker
            log.warning("heartbeat_failed", error=str(e))

    async def busy(self, job_id: str, stage: str) -> None:
        self.status.update(state="busy", job_id=job_id, stage=stage, stage_started_at=time.time())
        await self.publish()

    async def idle(self, error: str | None = None) -> None:
        self.status["handled"] += 1
        if error:
            self.status["failed"] += 1
            self.status["last_error"] = {"at": time.time(), "job_id": self.status["job_id"],
                                         "stage": self.status["stage"], "message": error[:500]}
        self.status.update(state="idle", last_job_id=self.status["job_id"], job_id=None, stage=None,
                           stage_started_at=None)
        await self.publish()

    async def run(self) -> None:
        while True:
            await self.publish()
            await asyncio.sleep(INTERVAL_SEC)

    async def stop(self) -> None:
        try:
            await self.redis.delete(self.key)
        except Exception:  # noqa: BLE001
            pass
