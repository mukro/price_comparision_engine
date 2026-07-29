# app/db.py
"""
Async database + cache layer, used by the FastAPI app (request handlers only).

Celery tasks and core/* business logic run synchronously and use
`app/db_sync.py` instead -- keeping the two worlds separate avoids the
mixed sync/async connection bugs from the previous version.

IMPORTANT: other modules should do `from app import db` and read
`db.db_pool` / `db.redis_client` *at call time*, not
`from app.db import db_pool` at import time -- the latter captures `None`
forever, since these globals are only assigned once `init_db_pool()` /
`init_redis()` run during FastAPI startup.
"""
from typing import Optional

import asyncpg
import redis.asyncio as aioredis

from app.config import settings

db_pool: Optional[asyncpg.Pool] = None
redis_client: Optional[aioredis.Redis] = None


async def init_db_pool() -> None:
    global db_pool
    db_pool = await asyncpg.create_pool(settings.ASYNC_DATABASE_URL, min_size=2, max_size=10)


async def close_db_pool() -> None:
    global db_pool
    if db_pool:
        await db_pool.close()
        db_pool = None


async def init_redis() -> None:
    global redis_client
    redis_client = await aioredis.from_url(
        settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
    )


async def close_redis() -> None:
    global redis_client
    if redis_client:
        await redis_client.close()
        redis_client = None


def get_db_pool() -> asyncpg.Pool:
    """FastAPI dependency-friendly accessor. Raises clearly if called before startup."""
    if db_pool is None:
        raise RuntimeError("Database pool not initialized -- app startup hasn't run yet.")
    return db_pool


def get_redis() -> aioredis.Redis:
    if redis_client is None:
        raise RuntimeError("Redis client not initialized -- app startup hasn't run yet.")
    return redis_client
