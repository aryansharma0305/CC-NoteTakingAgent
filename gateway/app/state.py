"""Process-wide resources, created in the FastAPI lifespan."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from fastapi import Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from vidnotes_common.config import Settings
from vidnotes_common.crypto import TokenCipher

from .publisher import Publisher


@dataclass
class AppState:
    settings: Settings
    engine: AsyncEngine
    publisher: Publisher
    redis: Redis
    http: httpx.AsyncClient
    cipher: TokenCipher


def get_state(request: Request) -> AppState:
    return request.app.state.vn
