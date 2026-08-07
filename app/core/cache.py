import json
from typing import Optional

from app.db import redis_client


async def get_cached_json(key: str) -> Optional[dict]:
    if not redis_client:
        return None
    data = await redis_client.get(key)
    return json.loads(data) if data else None

async def set_cached_json(key: str, data: dict, ttl_seconds: int = 900):
    if redis_client:
        await redis_client.set(key, json.dumps(data, default=str), ex=ttl_seconds)

async def invalidate_product_cache(product_id: str):
    if redis_client:
        await redis_client.delete(f"grid:{product_id}")