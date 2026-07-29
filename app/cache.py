# app/cache.py
"""
Async Redis JSON cache helpers for FastAPI request handlers.

There used to be two competing versions of this module (`app/cache.py` and
`app/core/cache.py`), and the latter awaited a *synchronous* redis client
by mistake. This is now the single source of truth -- `app/core/cache.py`
has been removed.
"""
import json
from typing import Optional

from app import db


async def get_cached_json(key: str) -> Optional[dict]:
    """Retrieves and deserializes JSON from Redis if available."""
    if db.redis_client is None:
        return None
    data = await db.redis_client.get(key)
    return json.loads(data) if data else None


async def set_cached_json(key: str, data, ttl_seconds: int = 900) -> None:
    """Stores data as JSON in Redis with a Time-To-Live (default 15 mins)."""
    if db.redis_client is not None:
        await db.redis_client.set(key, json.dumps(data, default=str), ex=ttl_seconds)


async def invalidate_cache_pattern(pattern: str) -> None:
    """Invalidates all cached keys matching a glob pattern."""
    if db.redis_client is None:
        return
    keys = await db.redis_client.keys(pattern)
    if keys:
        await db.redis_client.delete(*keys)


async def invalidate_product_cache(product_id: str) -> None:
    await invalidate_cache_pattern(f"grid:{product_id}")
    await invalidate_cache_pattern(f"cache:insights:*:{product_id}")
