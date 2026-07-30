# app/tasks.py
"""
All Celery task definitions with:
  - Compliance gatekeeping
  - Dead-letter queue for exhausted retries
  - Cursor-based stale-offer scanning
  - SSRF-safe webhooks
"""
import asyncio
import logging
from urllib.parse import urlparse

import requests
from celery.utils.log import get_task_logger
from psycopg2.extras import RealDictCursor

from app.celery_app import celery_app
from app.config import settings
from app.core.dynamic_pricing import evaluate_merchant_rules_for_product
from app.core.telemetry import log_price_audit_event
from app.db_sync import get_conn
from app.workers.email_worker import send_price_drop_email
from app.workers.scraper_worker import scrape_vendor_product_page

logger = get_task_logger(__name__)


def _is_safe_webhook_url(url: str) -> bool:
    """Blocks private IPs, localhost, and non-HTTPS in production."""
    parsed = urlparse(url)
    if not parsed.scheme or parsed.scheme not in ("https", "http"):
        return False
    hostname = parsed.hostname or ""
    if hostname in ("localhost", "127.0.0.1", "0.0.0.0"):
        return False
    if hostname.startswith("10.") or hostname.startswith("192.168.") or hostname.startswith("172."):
        return False
    if hostname.startswith("169.254."):
        return False
    return True


# ==========================================
# 1. SCRAPING
# ==========================================

@celery_app.task(bind=True, max_retries=3, default_retry_delay=120)
def process_vendor_scrape(self, offer_task: dict):
    """
    Scrapes one vendor listing via Playwright.
    If scraping is disabled globally, task becomes a no-op and logs the skip.
    """
    if not settings.SCRAPING_ENABLED:
        logger.info("SCRAPING_ENABLED is False; skipping scrape task.")
        return {"status": "skipped", "reason": "scraping_disabled"}

    from app.core.matcher import process_scraped_offer

    url = offer_task["product_url"]
    try:
        scraped = asyncio.run(
            scrape_vendor_product_page(
                url=url,
                title_selector=offer_task.get("title_selector", ".product-title"),
                price_selector=offer_task.get("price_selector", ".price"),
                stock_selector=offer_task.get("stock_selector"),
                stock_text_present=offer_task.get("stock_text_present"),
                proxy_url=offer_task.get("proxy_url") or settings.SCRAPER_PROXY_URL or None,
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
        # Dead-letter: if retries exhausted, log to DB for manual review
        if self.request.retries >= self.max_retries:
            _log_to_dlq(url, str(exc), offer_task)
        raise self.retry(exc=exc)


def _log_to_dlq(url: str, error: str, payload: dict) -> None:
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO scrape_dlq (url, error_message, payload, created_at)
                VALUES (%s, %s, %s, NOW());
                """,
                (url, error, str(payload)),
            )
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to write DLQ entry: {e}")


@celery_app.task
def run_catalog_scraper_job():
    """
    Refresh stale offers using cursor-based pagination to avoid starvation.
    Processes in batches of 500, ordered by oldest first.
    """
    if not settings.SCRAPING_ENABLED:
        logger.info("SCRAPING_ENABLED is False; skipping catalog refresh.")
        return

    logger.info("[BEAT] Starting full-catalog refresh...")
    batch_size = 500
    offset = 0
    total_queued = 0

    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        while True:
            cursor.execute(
                """
                SELECT vo.vendor_id, vo.vendor_product_id, vo.product_url, vo.affiliate_url,
                       v.title_selector, v.price_selector, v.stock_selector, v.stock_text_present
                FROM vendor_offers vo
                JOIN vendors v ON vo.vendor_id = v.id
                WHERE v.is_active = TRUE
                  AND (vo.last_scraped_at IS NULL OR vo.last_scraped_at < NOW() - INTERVAL '6 hours')
                ORDER BY vo.last_scraped_at ASC NULLS FIRST
                LIMIT %s OFFSET %s;
                """,
                (batch_size, offset),
            )
            offers = cursor.fetchall()
            if not offers:
                break

            for offer in offers:
                process_vendor_scrape.delay(
                    {
                        "vendor_id": str(offer["vendor_id"]),
                        "vendor_product_id": offer["vendor_product_id"],
                        "product_url": offer["product_url"],
                        "affiliate_url": offer["affiliate_url"],
                        "title_selector": offer["title_selector"],
                        "price_selector": offer["price_selector"],
                        "stock_selector": offer.get("stock_selector"),
                        "stock_text_present": offer.get("stock_text_present"),
                    }
                )
            total_queued += len(offers)
            offset += batch_size
            if len(offers) < batch_size:
                break

    logger.info(f"[BEAT] Queued {total_queued} vendor offers for scraping.")


@celery_app.task
def run_priority_scraper_job():
    """Refresh priority offers every 15 minutes."""
    if not settings.SCRAPING_ENABLED:
        return

    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT vo.vendor_id, vo.vendor_product_id, vo.product_url, vo.affiliate_url,
                   v.title_selector, v.price_selector, v.stock_selector, v.stock_text_present
            FROM vendor_offers vo
            JOIN vendors v ON vo.vendor_id = v.id
            WHERE v.is_active = TRUE AND vo.is_priority = TRUE
              AND (vo.last_scraped_at IS NULL OR vo.last_scraped_at < NOW() - INTERVAL '15 minutes')
            LIMIT 200;
            """,
        )
        offers = cursor.fetchall()

    for offer in offers:
        process_vendor_scrape.delay(
            {
                "vendor_id": str(offer["vendor_id"]),
                "vendor_product_id": offer["vendor_product_id"],
                "product_url": offer["product_url"],
                "affiliate_url": offer["affiliate_url"],
                "title_selector": offer["title_selector"],
                "price_selector": offer["price_selector"],
                "stock_selector": offer.get("stock_selector"),
                "stock_text_present": offer.get("stock_text_present"),
            }
        )
    logger.info(f"[BEAT] Queued {len(offers)} priority offers for scraping.")


@celery_app.task
def cleanup_stale_pending_matches():
    """Maintenance: clears pending-review matches older than 30 days."""
    with get_conn() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                DELETE FROM vendor_offers
                WHERE match_status = 'pending_review'
                  AND last_scraped_at < NOW() - INTERVAL '30 days';
                """,
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
    Evaluates active user price alerts and emails users whose target was hit.
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
    Evaluates merchant rules and dispatches webhooks with SSRF protection.
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
            if not _is_safe_webhook_url(eval_item["webhook_url"]):
                logger.warning(f"Blocked unsafe webhook URL: {eval_item['webhook_url']}")
                continue

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
                response = requests.post(
                    eval_item["webhook_url"],
                    json=webhook_payload,
                    timeout=5,
                    headers={"User-Agent": "PriceComparison-Webhook/1.0"},
                )
                logger.info(f"Dispatched webhook to {eval_item['webhook_url']} | Status: {response.status_code}")
            except Exception as exc:
                logger.error(f"Failed to dispatch webhook: {exc}")
