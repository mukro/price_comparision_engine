# Add to app/celery_app.py inside create_celery_app() function
# Replace the existing beat_schedule dictionary with this complete version

app.conf.beat_schedule = {
    # ── Core Scraping ──
    "scrape-active-vendors": {
        "task": "app.tasks.process_vendor_scrape",
        "schedule": 300.0,  # Every 5 minutes
        "options": {"queue": "default"},
    },

    # ── Notifications ──
    "price-drop-emails": {
        "task": "app.tasks.trigger_price_drop_emails",
        "schedule": 600.0,  # Every 10 minutes
        "options": {"queue": "default"},
    },
    "price-drop-push-notifications": {
        "task": "app.tasks.check_price_drops_and_queue_push",
        "schedule": 300.0,  # Every 5 minutes
        "options": {"queue": "high_priority"},
    },

    # ── Monetization ──
    "reset-sponsored-budgets": {
        "task": "app.tasks.reset_daily_sponsored_budgets",
        "schedule": 86400.0,  # Daily
        "options": {"queue": "default"},
    },
    "archive-old-clicks": {
        "task": "app.tasks.archive_old_clicks",
        "schedule": 604800.0,  # Weekly
        "options": {"queue": "default"},
    },

    # ── Credit Scoring (NEW) ──
    "credit-score-nightly-batch": {
        "task": "app.tasks_credit_scoring.recalculate_all_credit_scores",
        "schedule": 86400.0,  # Daily at 2:00 AM (set via crontab: "0 2 * * *")
        "options": {"queue": "default"},
    },

    # ── AutoBuy Trigger Scanner (NEW) ──
    "autobuy-trigger-scan": {
        "task": "app.tasks_autobuy.scan_auto_buy_triggers",
        "schedule": 300.0,  # Every 5 minutes
        "options": {"queue": "default"},
    },
}

# Agent schedules (ONLY when AGENTS_ENABLED=true)
if settings.AGENTS_ENABLED:
    app.conf.beat_schedule.update({
        "agent-entity-resolution": {
            "task": "app.tasks_agents.run_entity_resolution_pipeline",
            "schedule": 300.0,
            "options": {"queue": "default"},
        },
        "agent-selector-health": {
            "task": "app.tasks_agents.run_selector_health_check",
            "schedule": 3600.0,
            "options": {"queue": "default"},
        },
    })
