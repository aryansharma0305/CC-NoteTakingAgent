"""Everything a stage needs, built once per worker process."""

from __future__ import annotations

import socket
from dataclasses import dataclass

from aio_pika.abc import AbstractChannel, AbstractExchange
from sqlalchemy.ext.asyncio import AsyncEngine

from vidnotes_common.config import Settings
from vidnotes_common.crypto import TokenCipher

from .heartbeat import Heartbeat
from .llm import LLMClient
from .ratelimit import Limiter
from .storage import Storage


@dataclass
class WorkerContext:
    settings: Settings
    engine: AsyncEngine
    channel: AbstractChannel
    jobs_exchange: AbstractExchange
    dlx: AbstractExchange
    limiter: Limiter
    storage: Storage
    llm: LLMClient
    cipher: TokenCipher
    worker_id: str = socket.gethostname()
    heartbeat: Heartbeat | None = None  # live status for the /debug panel
