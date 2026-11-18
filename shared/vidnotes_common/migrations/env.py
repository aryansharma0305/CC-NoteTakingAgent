"""Alembic environment (async, asyncpg). Run via `python -m vidnotes_common.migrate`."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from vidnotes_common.config import get_settings
from vidnotes_common.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as conn:
        # Serialize concurrent migrators (e.g. two gateway replicas).
        await conn.exec_driver_sql("SELECT pg_advisory_lock(727274)")
        try:
            await conn.run_sync(_do_run)
            await conn.commit()
        finally:
            await conn.exec_driver_sql("SELECT pg_advisory_unlock(727274)")
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
