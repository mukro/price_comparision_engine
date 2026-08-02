# app/celery_app.py
"""
Celery application factory.

Workers:
- default      : General purpose tasks (scraping, emails, notifications)
- high_priority: Urgent tasks (push notifications, real-time updates)

Beat Schedule:
- Every 5 min  : Scrape vendors, check price drops
- Every 10 min : Send price drop emails
- Daily 00:00  : Reset sponsored ad budgets
- Weekly       : Archive old affiliate clicks
"""
import os
from celery import Celery
from celery.signals import worker_ready

# ------------------------------------------------------------------
# App Factory
# ------------------------------------------------------------------

def create_celery_app() -> Celery:
    """Create and configure the Celery application."""
    app = Celery(
        "price_comparison_engine",
        broker=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        backend=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        include=[
            "app.tasks",
            "app.workers.scraper_worker",
            "app.workers.email_worker",
        ],
    )

    # ------------------------------------------------------------------
    # Task Serialization
    # ------------------------------------------------------------------
    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        task_track_started=True,
        task_time_limit=300,           # 5 min hard limit per task
        task_soft_time_limit=240,      # 4 min soft limit
        worker_prefetch_multiplier=1,  # Fair task distribution
        worker_max_tasks_per_child=50, # Restart worker after 50 tasks (memory leak prevention)
    )

    # ------------------------------------------------------------------
    # Beat Schedule (Periodic Tasks)
    # ------------------------------------------------------------------
    app.conf.beat_schedule = {
        # --- Scraping Pipeline (existing) ---
        "scrape-active-vendors": {
            "task": "app.tasks.process_vendor_scrape",
            "schedule": 300.0,  # 5 minutes
            "options": {"queue": "default"},
        },

        # --- Email Alerts (existing) ---
        "price-drop-emails": {
            "task": "app.tasks.trigger_price_drop_emails",
            "schedule": 600.0,  # 10 minutes
            "options": {"queue": "default"},
        },

        # --- Push Notifications (NEW) ---
        "price-drop-push-notifications": {
            "task": "app.tasks.check_price_drops_and_queue_push",
            "schedule": 300.0,  # 5 minutes
            "options": {"queue": "high_priority"},
        },

        # --- Sponsored Ads Daily Reset (NEW) ---
        "reset-sponsored-budgets": {
            "task": "app.tasks.reset_daily_sponsored_budgets",
            "schedule": 86400.0,  # 24 hours
            "options": {"queue": "default"},
        },

        # --- Affiliate Click Cleanup (NEW) ---
        "archive-old-clicks": {
            "task": "app.tasks.archive_old_clicks",
            "schedule": 604800.0,  # 7 days
            "options": {"queue": "default"},
        },
    }

    # ------------------------------------------------------------------
    # Task Routes (send specific tasks to specific queues)
    # ------------------------------------------------------------------
    app.conf.task_routes = {
        "app.tasks.send_fcm_push_notification": {"queue": "high_priority"},
        "app.tasks.process_vendor_scrape": {"queue": "default"},
        "app.tasks.trigger_price_drop_emails": {"queue": "default"},
        "app.tasks.check_price_drops_and_queue_push": {"queue": "high_priority"},
        "app.tasks.reset_daily_sponsored_budgets": {"queue": "default"},
        "app.tasks.archive_old_clicks": {"queue": "default"},
    }

    # ------------------------------------------------------------------
    # Result Backend Expiry
    # ------------------------------------------------------------------
    app.conf.result_expires = 3600  # 1 hour
    app.conf.result_extended = True

    return app


# ------------------------------------------------------------------
# Singleton Instance
# ------------------------------------------------------------------

celery_app = create_celery_app()


# ------------------------------------------------------------------
# Signals
# ------------------------------------------------------------------

@worker_ready.connect
def on_worker_ready(**kwargs):
    """Log when a Celery worker starts."""
    import logging
    logger = logging.getLogger("celery.worker")
    logger.info("Celery worker ready — tasks: %s", list(celery_app.tasks.keys()))
