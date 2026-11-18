"""GET /healthz: confirms gateway, Postgres, RabbitMQ, Redis and the S3 frame store are reachable."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .state import AppState, get_state

router = APIRouter()


CHECK_TIMEOUT_SEC = 3.0


async def _check(coro) -> str:
    try:
        await asyncio.wait_for(coro, CHECK_TIMEOUT_SEC)
        return "ok"
    except Exception as e:  # noqa: BLE001
        return f"error: {type(e).__name__}: {str(e)[:120]}"


async def _pg(st: AppState) -> None:
    async with st.engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def _s3(st: AppState) -> None:
    # Any S3 answer (even 403 for an unsigned request) proves the store is up.
    r = await st.http.get(f"{st.settings.s3_endpoint.rstrip('/')}/", timeout=3)
    if r.status_code >= 500:
        raise RuntimeError(f"S3 endpoint returned {r.status_code}")


@router.get("/healthz")
async def healthz(st: AppState = Depends(get_state)):
    pg, mq, rd, s3 = await asyncio.gather(
        _check(_pg(st)), _check(st.publisher.ping()), _check(st.redis.ping()), _check(_s3(st))
    )
    body = {"gateway": "ok", "postgres": pg, "rabbitmq": mq, "redis": rd, "s3": s3}
    failing = [k for k, v in body.items() if v != "ok"]
    if failing:
        body["failing"] = failing
        return JSONResponse(status_code=503, content=body)
    return body
