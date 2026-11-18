"""API gateway. Accepts jobs and exposes status/results; never runs pipeline work."""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from vidnotes_common.config import get_settings
from vidnotes_common.crypto import TokenCipher
from vidnotes_common.db import make_engine
from vidnotes_common.logs import configure_logging, get_logger

from . import health, routes_admin, routes_debug, routes_jobs, routes_oauth
from .errors import install_handlers
from .publisher import Publisher
from .state import AppState

log = get_logger("gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    configure_logging(s.log_level)
    publisher = Publisher(s)
    await publisher.connect()
    state = AppState(
        settings=s,
        engine=make_engine(s.database_url, pool_size=10, max_overflow=10),
        publisher=publisher,
        redis=Redis.from_url(s.redis_url),
        http=httpx.AsyncClient(timeout=10),
        cipher=TokenCipher(s.token_enc_key),
    )
    app.state.vn = state
    log.info("gateway_started", base_url=s.gateway_base_url, max_duration_sec=s.max_duration_sec)
    try:
        yield
    finally:
        await publisher.close()
        await state.http.aclose()
        await state.redis.aclose()
        await state.engine.dispose()


def create_app(*, with_lifespan: bool = True) -> FastAPI:
    app = FastAPI(
        title="Video Note-Taking Agent API",
        version="1.0.0",
        description="Submit a YouTube video, poll its status, fetch the Notion page URL.",
        lifespan=lifespan if with_lifespan else None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^(chrome-extension://.*|http://localhost(:\d+)?)$",
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Location"],
    )
    install_handlers(app)
    app.include_router(routes_jobs.router, tags=["jobs"])
    app.include_router(health.router, tags=["health"])
    app.include_router(routes_oauth.router, tags=["oauth"])
    app.include_router(routes_admin.router, tags=["admin"])
    app.include_router(routes_debug.page_router)
    app.include_router(routes_debug.router, tags=["debug"])
    return app


app = create_app()
