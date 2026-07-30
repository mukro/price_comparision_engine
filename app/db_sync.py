# app/db_sync.py
"""
Synchronous, pooled database + cache access for Celery tasks and core logic.
Adds connection timeouts and health-checks.
"""
import logging

import psycopg2
import redis
from psycopg2 import pool as pg_pool

from app.config import settings

logger = logging.getLogger("db_sync")

_pg_pool = pg_pool.ThreadedConnectionPool(
    minconn=2,
    maxconn=20,
    dsn=settings.DATABASE_URL,
    connect_timeout=10,
    options="-c statement_timeout=30000",  # 30s query timeout
)

redis_client = redis.Redis.from_url(
    settings.REDIS_URL,
    decode_responses=True,
    socket_connect_timeout=5,
    socket_timeout=5,
    health_check_interval=30,
)


class get_conn:
    """
    Context manager that borrows a pooled psycopg2 connection and always
    returns it to the pool, rolling back on error.
    """

    def __enter__(self) -> psycopg2.extensions.connection:
        self.conn = _pg_pool.getconn()
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            try:
                self.conn.rollback()
            except Exception:
                pass
        _pg_pool.putconn(self.conn)


def invalidate_grid_cache(product_id: str) -> None:
    """Purges the cached API response from Redis when a price is updated."""
    try:
        redis_client.delete(f"grid:{product_id}")
    except Exception as e:
        logger.warning(f"Failed to invalidate cache for product {product_id}: {e}")
