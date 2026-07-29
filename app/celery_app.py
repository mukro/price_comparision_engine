# app/celery_app.py
from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery_app = Celery(
    "price_comparison_workers",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=300,
    task_soft_time_limit=240,
    worker_prefetch_multiplier=1,
)

celery_app.conf.beat_schedule = {
    # Full catalog refresh every 6 hours: anything not scraped in 6h+.
    "trigger-full-catalog-scrape-every-6-hours": {
        "task": "app.tasks.run_catalog_scraper_job",
        "schedule": crontab(minute=0, hour="*/6"),
    },
    # Hot-deal / high-priority items refreshed much more often.
    "trigger-priority-deals-scrape-every-15-min": {
        "task": "app.tasks.run_priority_scraper_job",
        "schedule": 900.0,
    },
    # Daily cleanup of stale pending-review matches.
    "cleanup-stale-pending-matches-daily": {
        "task": "app.tasks.cleanup_stale_pending_matches",
        "schedule": crontab(hour=2, minute=0),
    },
}
