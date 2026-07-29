# app/tasks.py
"""
All Celery task definitions live here. Each task is a thin wrapper around
`app/core/*` and `app/workers/*` -- keep business logic in those modules
and orchestration/retry policy here.
"""
import asyncio
import logging

import requests
from celery.utils.log import get_task_logger
from psycopg2.extras import RealDictCursor

from app.celery_app import celery_app
from app.core.dynamic_pricing import evaluate_merchant_rules_for_product
from app.core.telemetry import log_price_audit_event
from app.db_sync import get_conn
from app.workers.email_worker import send_price_drop_email
from app.workers.scraper_worker import scrape_vendor_product_page

logger = get_task_logger(__name__)
logging.getLogger("app.tasks")


# ==========================================
# 1. SCRAPING
# ==========================================

@celery_app.task(bind=True, max_retries=3, default_retry_delay=120)
def process_vendor_scrape(self, offer_task: dict):
    """
    Scrapes one vendor listing via Playwright and runs it through the
    entity-resolution matcher. This is the single real scrape task --
    it replaces the old mock (`scrape_single_vendor_product`) that
    inserted hardcoded fake data for every product regardless of URL.
    """
    from app.core.matcher import process_scraped_offer  # local import: keeps the ML model out of light tasks

    url = offer_task["product_url"]
    try:
        scraped = asyncio.run(
            scrape_vendor_product_page(
                url=url,
                title_selector=offer_task.get("title_selector", ".product-title"),
                price_selector=offer_task.get("price_selector", ".price"),
                proxy_url=offer_task.get("proxy_url"),
            )
        )
        if not scraped:
            logger.warning(f"No data extracted for {url}")
            return {"status": "failed", "url": url, "reason": "extraction_failed"}

        scraped["vendor_id"] = offer_task["vendor_id"]
        scraped["vendor_product_id"] = offer_task["vendor_product_id"]
        scraped["affiliate_url"] = offer_task.get("affiliate_url")
        scraped["brand"] = offer_task.get("brand")

        process_scraped_offer(scraped)
        return {"status": "success", "url": url, "price": scraped["price"]}

    except Exception as exc:
        logger.exception(f"Scrape task failed for {url}")
        raise self.retry(exc=exc)


@celery_app.task
def run_catalog_scraper_job():
    """Celery Beat: refresh any active vendor offer not scraped in 6+ hours."""
    logger.info("[BEAT] Starting full-catalog refresh...")
    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT vo.vendor_id, vo.vendor_product_id, vo.product_url, vo.affiliate_url,
                   v.title_selector, v.price_selector
            FROM vendor_offers vo
            JOIN vendors v ON vo.vendor_id = v.id
            WHERE v.is_active = TRUE
                AND (vo.last_scraped_at IS NULL OR vo.last_scraped_at < NOW() - INTERVAL '6 hours')
            ORDER BY vo.last_scraped_at ASC NULLS FIRST
            LIMIT 500;
            """
        )
        offers = cursor.fetchall()

    logger.info(f"[BEAT] Queuing {len(offers)} vendor offers for scraping.")
    for offer in offers:
        process_vendor_scrape.delay(
            {
                "vendor_id": str(offer["vendor_id"]),
                "vendor_product_id": offer["vendor_product_id"],
                "product_url": offer["product_url"],
                "affiliate_url": offer["affiliate_url"],
                "title_selector": offer["title_selector"],
                "price_selector": offer["price_selector"],
            }
        )


@celery_app.task
def run_priority_scraper_job():
    """Celery Beat: refresh flagged 'hot deal' offers every 15 minutes instead of every 6 hours."""
    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT vo.vendor_id, vo.vendor_product_id, vo.product_url, vo.affiliate_url,
                   v.title_selector, v.price_selector
            FROM vendor_offers vo
            JOIN vendors v ON vo.vendor_id = v.id
            WHERE v.is_active = TRUE AND vo.is_priority = TRUE
                AND (vo.last_scraped_at IS NULL OR vo.last_scraped_at < NOW() - INTERVAL '15 minutes')
            LIMIT 200;
            """
        )
        offers = cursor.fetchall()

    logger.info(f"[BEAT] Queuing {len(offers)} priority vendor offers for scraping.")
    for offer in offers:
        process_vendor_scrape.delay(
            {
                "vendor_id": str(offer["vendor_id"]),
                "vendor_product_id": offer["vendor_product_id"],
                "product_url": offer["product_url"],
                "affiliate_url": offer["affiliate_url"],
                "title_selector": offer["title_selector"],
                "price_selector": offer["price_selector"],
            }
        )


@celery_app.task
def cleanup_stale_pending_matches():
    """Maintenance job: clears pending-review matches older than 30 days."""
    with get_conn() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                DELETE FROM vendor_offers
                WHERE match_status = 'pending_review'
                    AND last_scraped_at < NOW() - INTERVAL '30 days';
                """
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Cleanup failed: {e}")


# ==========================================
# 2. PRICE DROP NOTIFICATIONS
# ==========================================

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def check_and_notify_price_drops(self, master_product_id: str, new_price: float):
    """
    Evaluates active user price alerts (table: user_alerts) for a product
    when its price updates, and emails everyone whose target has been hit.
    """
    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cursor.execute(
                """
                SELECT
                    ua.id AS alert_id, ua.user_email, ua.target_price,
                    p.title AS product_title,
                    (SELECT COALESCE(vo.affiliate_url, vo.product_url)
                     FROM vendor_offers vo
                     WHERE vo.product_id = p.id AND vo.in_stock = TRUE
                     ORDER BY vo.current_price ASC LIMIT 1) AS buy_url
                FROM user_alerts ua
                JOIN products p ON ua.product_id = p.id
                WHERE ua.product_id = %s::uuid
                    AND ua.is_active = TRUE
                    AND ua.target_price >= %s;
                """,
                (master_product_id, new_price),
            )
            triggered_alerts = cursor.fetchall()

            for alert in triggered_alerts:
                try:
                    sent = send_price_drop_email(
                        to_email=alert["user_email"],
                        product_title=alert["product_title"],
                        new_price=new_price,
                        buy_url=alert["buy_url"] or "",
                    )
                    if sent:
                        cursor.execute(
                            "INSERT INTO alert_logs (alert_id, triggered_price) VALUES (%s, %s);",
                            (alert["alert_id"], new_price),
                        )
                        cursor.execute(
                            """
                            UPDATE user_alerts
                            SET is_active = FALSE, last_triggered_at = NOW()
                            WHERE id = %s;
                            """,
                            (alert["alert_id"],),
                        )
                    conn.commit()
                except Exception as email_err:
                    conn.rollback()
                    logger.error(f"Failed notification for alert {alert['alert_id']}: {email_err}")

        except Exception as exc:
            conn.rollback()
            logger.error(f"check_and_notify_price_drops failed: {exc}")
            raise self.retry(exc=exc)


# ==========================================
# 3. B2B DYNAMIC REPRICING
# ==========================================

@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def dispatch_merchant_repricing_webhooks(self, master_product_id: str, new_competitor_price: float):
    """
    Evaluates B2B merchant counter-strategies when a competitor price
    shifts, logs every decision to the audit trail, and posts automated
    price-update webhooks for merchants with auto-apply enabled.
    """
    eval_data = evaluate_merchant_rules_for_product(master_product_id, new_competitor_price)
    if not eval_data or "merchant_evaluations" not in eval_data:
        return

    for eval_item in eval_data["merchant_evaluations"]:
        result = eval_item["strategy_result"]

        log_price_audit_event(
            merchant_id=eval_item["merchant_id"],
            product_id=master_product_id,
            old_price=new_competitor_price,
            new_price=result["recommended_price"],
            event_type="competitor_price_undercut_detected",
            circuit_breaker=eval_item["circuit_breaker_tripped"],
        )

        if eval_item["auto_apply"] and eval_item["webhook_url"]:
            try:
                webhook_payload = {
                    "event": "competitor_price_undercut_detected",
                    "product_id": master_product_id,
                    "competitor_price": new_competitor_price,
                    "recommended_price": result["recommended_price"],
                    "floor_applied": result["floor_hit"],
                    "circuit_breaker_tripped": eval_item["circuit_breaker_tripped"],
                    "reason": result["reason"],
                }
                response = requests.post(eval_item["webhook_url"], json=webhook_payload, timeout=5)
                logger.info(f"Dispatched webhook to {eval_item['webhook_url']} | Status: {response.status_code}")
            except Exception as exc:
                logger.error(f"Failed to dispatch webhook: {exc}")
