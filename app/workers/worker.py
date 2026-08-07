# Automatic Cache Invalidation on Scraper Updates
# When Celery finishes scraping new prices in matcher.py, invalidate the affected product's cache so users immediately see updated pricing:

import redis

# Synchronous Redis client for Celery worker
sync_redis = redis.Redis.from_url("redis://localhost:6379/0")

def invalidate_product_cache(product_id: str):
    """Call this inside matcher.py whenever a vendor_offer price is updated."""
    sync_redis.delete(f"grid:{product_id}")
    print(f"[CACHE INVALIDATED] grid:{product_id}")