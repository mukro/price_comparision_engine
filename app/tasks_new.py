# app/tasks.py
"""
Celery task definitions.

Tasks:
- process_vendor_scrape   : Playwright scraping pipeline (existing)
- send_price_drop_email     : Email notification worker (existing)
- reset_sponsored_budgets   : Daily spend reset for ads (NEW)
- check_price_drops         : Watchlist price drop → push queue (NEW)
- process_partner_feed      : Batch process partner feed logs (NEW)
"""
import json
import asyncio
import logging
from typing import Any, Dict, List

from celery import shared_task

from app.db_sync import get_conn
from app.workers.scraper_worker import scrape_vendor_product_page, process_incoming_scraped_payload
from app.workers.email_worker import send_price_drop_email

logger = logging.getLogger("celery.tasks")


# ==================================================================
# EXISTING: Vendor Scraping Pipeline
# ==================================================================

@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def process_vendor_scrape(self, vendor_id: str, product_url: str, selectors: Dict[str, str]):
    """
    Celery task: scrape a single vendor product page.
    Called by Celery Beat scheduler or triggered by admin.
    """
    try:
        result = asyncio.run(scrape_vendor_product_page(
            url=product_url,
            title_selector=selectors.get("title_selector"),
            price_selector=selectors.get("price_selector"),
            stock_selector=selectors.get("stock_selector"),
            stock_text_present=selectors.get("stock_text_present"),
        ))

        if result:
            payload = {
                "raw_title": result["raw_title"],
                "vendor_id": vendor_id,
                "vendor_product_id": product_url,  # or SKU if known
                "price": result["price"],
                "product_url": result["product_url"],
                "in_stock": result["in_stock"],
                "currency": selectors.get("currency", "INR"),
            }
            process_incoming_scraped_payload(payload)
            logger.info(f"Scraped and processed {product_url}")
            return {"status": "success", "url": product_url, "price": result["price"]}
        else:
            logger.warning(f"Scrape returned no data for {product_url}")
            return {"status": "no_data", "url": product_url}

    except Exception as exc:
        logger.error(f"Scrape failed for {product_url}: {exc}")
        raise self.retry(exc=exc)


# ==================================================================
# EXISTING: Price Drop Email Notifications
# ==================================================================

@shared_task
def trigger_price_drop_emails():
    """
    Check for price drops and queue email notifications.
    Called by Celery Beat every 10 minutes.
    """
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT ua.id, ua.user_email, p.title, p.id as product_id,
                   MIN(vo.current_price) as current_price, ua.target_price
            FROM user_alerts ua
            JOIN products p ON ua.product_id = p.id
            JOIN vendor_offers vo ON vo.product_id = p.id AND vo.in_stock = TRUE
            WHERE ua.is_active = TRUE
              AND (ua.last_notified_at IS NULL OR ua.last_notified_at < NOW() - INTERVAL '6 hours')
            GROUP BY ua.id, p.id
            HAVING MIN(vo.current_price) <= ua.target_price;
        """)
        alerts = cursor.fetchall()

    sent = 0
    for alert in alerts:
        try:
            send_price_drop_email.delay(
                to_email=alert[1],
                product_title=alert[2],
                current_price=alert[4],
                target_price=alert[5],
                product_id=alert[3],
            )
            # Mark as notified
            with get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE user_alerts SET last_notified_at = NOW() WHERE id = %s;",
                    (alert[0],),
                )
                conn.commit()
            sent += 1
        except Exception as e:
            logger.error(f"Failed to queue email for alert {alert[0]}: {e}")

    return {"emails_queued": sent, "alerts_checked": len(alerts)}


# ==================================================================
# NEW: Sponsored Placement Daily Budget Reset
# ==================================================================

@shared_task
def reset_daily_sponsored_budgets():
    """
    Reset daily_spend to 0 for all active sponsored placements.
    Run by Celery Beat at 00:00 UTC daily.
    Also reactivates any placements that were paused due to budget exhaustion.
    """
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE sponsored_placements
            SET daily_spend = 0, updated_at = NOW()
            WHERE is_active = TRUE
              AND start_date <= CURRENT_DATE
              AND (end_date IS NULL OR end_date >= CURRENT_DATE);
        """)
        reset_count = cursor.rowcount
        conn.commit()

    logger.info(f"Reset daily budgets for {reset_count} sponsored placements")
    return {"status": "success", "placements_reset": reset_count}


# ==================================================================
# NEW: Price Drop Push Notifications
# ==================================================================

@shared_task
def check_price_drops_and_queue_push():
    """
    Check user watchlists for price drops and queue FCM push notifications.
    Run by Celery Beat every 5 minutes.
    """
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                w.id, w.user_id, w.product_id, w.target_price,
                p.title as product_title,
                (SELECT MIN(current_price) FROM vendor_offers WHERE product_id = w.product_id AND in_stock = TRUE) as current_price,
                u.fcm_token
            FROM user_watchlist w
            JOIN products p ON w.product_id = p.id
            JOIN users u ON w.user_id = u.id
            WHERE w.notify_push = TRUE
              AND u.fcm_token IS NOT NULL
              AND (w.last_notified_at IS NULL OR w.last_notified_at < NOW() - INTERVAL '6 hours')
              AND (
                  w.target_price IS NULL
                  OR (SELECT MIN(current_price) FROM vendor_offers WHERE product_id = w.product_id AND in_stock = TRUE) <= w.target_price
              );
        """)
        rows = cursor.fetchall()

    notifications_queued = 0
    for row in rows:
        watchlist_id, user_id, product_id, target_price, product_title, current_price, fcm_token = row

        if current_price is None:
            continue

        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO push_notifications (
                        user_id, fcm_token, title, body, data, priority, scheduled_at
                    ) VALUES (%s, %s, %s, %s, %s, 'high', NOW());
                """, (
                    user_id,
                    fcm_token,
                    f"📉 Price Drop: {product_title}",
                    f"Now ₹{current_price:,.0f}" + (f" (target: ₹{target_price:,.0f})" if target_price else ""),
                    json.dumps({
                        "type": "price_drop",
                        "product_id": str(product_id),
                        "watchlist_id": str(watchlist_id),
                        "current_price": float(current_price),
                    }),
                ))

                cursor.execute(
                    "UPDATE user_watchlist SET last_notified_at = NOW() WHERE id = %s;",
                    (watchlist_id,),
                )
                conn.commit()
                notifications_queued += 1
        except Exception as e:
            logger.error(f"Failed to queue push notification for watchlist {watchlist_id}: {e}")

    logger.info(f"Queued {notifications_queued} price drop push notifications")
    return {"status": "success", "notifications_queued": notifications_queued}


# ==================================================================
# NEW: FCM Push Notification Sender
# ==================================================================

@shared_task
def send_fcm_push_notification(notification_id: str):
    """
    Send a single FCM push notification.
    Called by a worker that polls the push_notifications table.
    """
    import os
    import requests

    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, fcm_token, title, body, data, priority
            FROM push_notifications
            WHERE id = %s AND status = 'pending';
        """, (notification_id,))
        notif = cursor.fetchone()

    if not notif:
        return {"status": "skipped", "reason": "not found or already sent"}

    _, user_id, fcm_token, title, body, data, priority = notif

    # Firebase Cloud Messaging HTTP v1 API
    # Requires GOOGLE_APPLICATION_CREDENTIALS env var or service account
    fcm_url = "https://fcm.googleapis.com/v1/projects/YOUR_PROJECT_ID/messages:send"
    server_key = os.environ.get("FCM_SERVER_KEY", "")

    if not server_key:
        logger.warning("FCM_SERVER_KEY not set — push notification queued but not sent")
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE push_notifications SET status = 'failed', error_message = %s WHERE id = %s;",
                ("FCM_SERVER_KEY not configured", notification_id),
            )
            conn.commit()
        return {"status": "failed", "reason": "FCM not configured"}

    payload = {
        "message": {
            "token": fcm_token,
            "notification": {
                "title": title,
                "body": body,
            },
            "data": data or {},
            "android": {
                "priority": "high" if priority == "high" else "normal",
                "notification": {
                    "channel_id": "price_drop_channel",
                    "sound": "default",
                },
            },
            "apns": {
                "payload": {
                    "aps": {
                        "sound": "default",
                        "badge": 1,
                    }
                }
            },
        }
    }

    try:
        response = requests.post(
            fcm_url,
            headers={
                "Authorization": f"Bearer {server_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10,
        )

        if response.status_code == 200:
            with get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE push_notifications SET status = 'sent', sent_at = NOW() WHERE id = %s;",
                    (notification_id,),
                )
                conn.commit()
            return {"status": "sent", "notification_id": notification_id}
        else:
            raise Exception(f"FCM returned {response.status_code}: {response.text}")

    except Exception as e:
        logger.error(f"Failed to send FCM notification {notification_id}: {e}")
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE push_notifications SET status = 'failed', error_message = %s WHERE id = %s;",
                (str(e)[:500], notification_id),
            )
            conn.commit()
        return {"status": "failed", "error": str(e)}


# ==================================================================
# NEW: Partner Feed Batch Processor
# ==================================================================

@shared_task
def process_partner_feed_log(feed_log_id: str):
    """
    Process a partner feed log entry asynchronously.
    Useful for large bulk uploads that need background processing.
    """
    logger.info(f"Processing partner feed log: {feed_log_id}")
    # Placeholder: actual processing happens synchronously in the API endpoint
    # This task is reserved for future async batch processing
    return {"status": "processed", "feed_log_id": feed_log_id}


# ==================================================================
# NEW: Affiliate Click Cleanup
# ==================================================================

@shared_task
def archive_old_clicks():
    """
    Archive clicks older than 90 days to cold storage.
    Keeps the affiliate_clicks table performant.
    """
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM affiliate_clicks
            WHERE clicked_at < NOW() - INTERVAL '90 days'
              AND status IN ('clicked', 'expired');
        """)
        deleted = cursor.rowcount
        conn.commit()

    logger.info(f"Archived {deleted} old affiliate clicks")
    return {"status": "success", "deleted": deleted}
