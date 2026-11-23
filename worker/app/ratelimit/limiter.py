"""Global rate limiter: Redis token buckets driven by one atomic Lua script.

Layer 1 (proactive): every external call first takes a token here, so the
combined call rate of all workers stays under each provider's limit and a burst
never produces a 429. Layer 2 (reactive): a 429 that still happens is raised as
RateLimited and goes through the retry queues.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from vidnotes_common.config import Settings
from vidnotes_common.errors import RateLimited

LUA = (Path(__file__).parent / "bucket.lua").read_text()


@dataclass(frozen=True)
class BucketSpec:
    key: str
    capacity: float
    rate_per_sec: float
    service: str


def bucket_specs(s: Settings) -> dict[str, BucketSpec]:
    rpm = max(1, s.llm_rpm)
    tpm = max(1000, s.llm_tpm)
    return {
        # Small burst so a queue of jobs is spread out instead of firing together.
        "llm_rpm": BucketSpec("rl:llm:rpm", max(1.0, min(3.0, rpm / 10)), rpm / 60.0, "llm"),
        "llm_tpm": BucketSpec("rl:llm:tpm", max(tpm / 6.0, 1.0), tpm / 60.0, "llm"),
        "notion": BucketSpec("rl:notion", max(1.0, math.ceil(s.notion_rps)), s.notion_rps, "notion"),
        "youtube": BucketSpec("rl:youtube", max(1.0, math.ceil(s.youtube_rps)), s.youtube_rps, "youtube"),
    }


class Limiter:
    def __init__(self, redis, settings: Settings):
        self._redis = redis
        self._script = redis.register_script(LUA)
        self._specs = bucket_specs(settings)
        self._max_wait = settings.rl_max_wait_sec

    async def try_take(self, name: str, tokens: float = 1, key_suffix: str = "") -> tuple[bool, int]:
        spec = self._specs[name]
        key = f"{spec.key}:{key_suffix}" if key_suffix else spec.key
        allowed, wait_ms = await self._script(keys=[key], args=[spec.capacity, spec.rate_per_sec, tokens])
        return bool(int(allowed)), int(wait_ms)

    async def acquire(self, name: str, tokens: float = 1, key_suffix: str = "") -> None:
        """Block (with jitter) until a token is granted, up to RL_MAX_WAIT_SEC.

        Raises RateLimited when the wait would exceed the budget so the job is
        parked in a retry queue instead of holding a worker forever.
        """
        spec = self._specs[name]
        deadline = time.monotonic() + self._max_wait
        while True:
            ok, wait_ms = await self.try_take(name, tokens, key_suffix)
            if ok:
                return
            wait = wait_ms / 1000.0 * (1.0 + random.random() * 0.2) + 0.01
            if time.monotonic() + wait > deadline:
                raise RateLimited(spec.service, retry_after=wait,
                                  message=f"Waiting for {spec.service} capacity (global rate limit)")
            await asyncio.sleep(wait)


SEM_LUA = (Path(__file__).parent / "semaphore.lua").read_text()
LEASE_MS = 15_000  # a SIGKILLed holder's slot frees itself within this (refreshed every 5 s)
WAITER_TTL_MS = 10_000
POLL_SEC = 1.0


class ConcurrencyGate:
    """Global cap on in-flight calls to one service (LLM_CONCURRENCY), fair (FIFO)
    across workers and safe against crashes: the holder refreshes its lease in the
    background, so a killed worker's slot expires within LEASE_MS.

        async with gate.slot(on_wait=...):
            await llm.complete_json(...)
    """

    def __init__(self, redis, name: str, limit: int, max_wait_sec: float):
        self._redis = redis
        self._script = redis.register_script(SEM_LUA)
        self._keys = [f"sem:{name}:holders", f"sem:{name}:queue", f"sem:{name}:seen", f"sem:{name}:ticket"]
        self.name = name
        self.limit = limit
        self.max_wait = max_wait_sec

    async def _call(self, op: str, member: str) -> tuple[bool, int, int]:
        granted, pos, held = await self._script(
            keys=self._keys, args=[op, member, self.limit, LEASE_MS, WAITER_TTL_MS]
        )
        return bool(int(granted)), int(pos), int(held)

    async def status(self) -> dict[str, int]:
        held = await self._redis.zcard(self._keys[0])
        waiting = await self._redis.zcard(self._keys[1])
        return {"limit": self.limit, "in_flight": int(held), "waiting": int(waiting)}

    def slot(self, on_wait=None):
        return _Slot(self, on_wait)


class _Slot:
    def __init__(self, gate: ConcurrencyGate, on_wait):
        self.gate = gate
        self.on_wait = on_wait  # async callback(position) while queued (called on changes)
        self.member = f"{uuid.uuid4().hex}"
        self._refresher: asyncio.Task | None = None
        self.waited_sec = 0.0

    async def __aenter__(self):
        g = self.gate
        if g.limit <= 0:
            return self
        start = time.monotonic()
        last_pos = None
        while True:
            granted, pos, _ = await g._call("acquire", self.member)
            if granted:
                break
            if pos != last_pos and self.on_wait is not None:
                last_pos = pos
                try:
                    await self.on_wait(pos)
                except Exception:  # noqa: BLE001 - status updates must never block the call
                    pass
            if time.monotonic() - start > g.max_wait:
                await g._call("release", self.member)
                raise RateLimited(g.name, retry_after=None,
                                  message=f"Waiting for the {g.name.upper()} (busy with other jobs)")
            await asyncio.sleep(POLL_SEC * (0.8 + random.random() * 0.4))
        self.waited_sec = time.monotonic() - start
        self._refresher = asyncio.create_task(self._refresh())
        return self

    async def _refresh(self):
        while True:
            await asyncio.sleep(LEASE_MS / 3000)
            try:
                await self.gate._call("refresh", self.member)
            except Exception:  # noqa: BLE001 - next refresh retries
                pass

    async def __aexit__(self, *exc):
        if self.gate.limit <= 0:
            return False
        if self._refresher:
            self._refresher.cancel()
        try:
            await self.gate._call("release", self.member)
        except Exception:  # noqa: BLE001 - the lease expires on its own
            pass
        return False
