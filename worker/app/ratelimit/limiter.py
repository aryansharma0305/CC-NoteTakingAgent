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
