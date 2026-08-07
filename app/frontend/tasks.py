# app/tasks.py
# app/tasks.py
import asyncio
import os

import psycopg2
import requests
import sendgrid
from psycopg2.extras import RealDictCursor
from scraper import scrape_product_page
from sendgrid.helpers.mail import Content, Email, Mail, To

from app.celery_app import celery_app
from app.config import settings
from app.core.dynamic_pricing import evaluate_merchant_rules_for_product
from app.db import redis_client
from app.workers.scraper_worker import process_incoming_scraped_payload
from extras.matcher import process_scraped_offer

SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
FROM_EMAIL = "alerts@yourpricecomparison.com"

@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def dispatch_merchant_repricing_webhooks(self, master_product_id: str, new_competitor_price: float):
    """
    Evaluates B2B merchant counter-strategies when a price shift is scraped,
    and posts automated price update webhooks to merchant endpoints.
    """
    eval_data = evaluate_merchant_rules_for_product(master_product_id, new_competitor_price)
    if not eval_data or "merchant_evaluations" not in eval_data:
        return

    for eval_item in eval_data["merchant_evaluations"]:
        if eval_item["auto_apply"] and eval_item["webhook_url"]:
            try:
                # Dispatch payload to merchant store integration endpoint
                webhook_payload = {
                    "event": "competitor_price_undercut_detected",
                    "product_id": master_product_id,
                    "competitor_price": new_competitor_price,
                    "recommended_price": eval_item["strategy_result"]["recommended_price"],
                    "floor_applied": eval_item["strategy_result"]["floor_hit"],
                    "reason": eval_item["strategy_result"]["reason"]
                }
                
                response = requests.post(
                    eval_item["webhook_url"], 
                    json=webhook_payload, 
                    timeout=5
                )
                print(f"[B2B REPRICER] Dispatched webhook to {eval_item['webhook_url']} | Status: {response.status_code}")
            except Exception as exc:
                print(f"[B2B REPRICER ERROR] Failed to dispatch webhook: {exc}")



# ==========================================
# 1. PRICE DROP NOTIFICATION TASK
# ==========================================

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def check_and_notify_price_drops(self, master_product_id: str, new_price: float):
    """
    Evaluates active user price alerts for a master product when a price updates.
    Performs atomic commits per email sent to prevent duplicate notifications
    if one email fails.
    """
    conn = psycopg2.connect(settings.DATABASE_URL)
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    try:
        # Fetch active price alerts where target_price is >= current new price
        cursor.execute("""
            SELECT 
                pa.id AS alert_id,
                pa.user_email,
                pa.target_price,
                p.title AS product_title,
                vo.product_url AS buy_url
            FROM price_alerts pa
            JOIN products p ON pa.product_id = p.id
            JOIN vendor_offers vo ON vo.product_id = p.id
            WHERE pa.product_id = %s::uuid 
                AND pa.is_active = TRUE 
                AND pa.target_price >= %s
            ORDER BY vo.current_price ASC
            LIMIT 1;
        """, (master_product_id, new_price))
        
        triggered_alerts = cursor.fetchall()

        for alert in triggered_alerts:
            try:
                # --- SEND NOTIFICATION ---
                # Example: send_email_via_sendgrid(alert["user_email"], alert["product_title"], new_price)
                print(f"[PRICE ALERT] Sending alert to {alert['user_email']} for {alert['product_title']} @ ${new_price}")

                # Deactivate trigger or log execution
                cursor.execute("""
                    UPDATE price_alerts 
                    SET is_active = FALSE, last_triggered_at = NOW() 
                    WHERE id = %s::uuid;
                """, (alert["alert_id"],))

                # ATOMIC COMMIT: Commit immediately per successful alert process
                conn.commit()

            except Exception as email_err:
                conn.rollback()  # Roll back only this failed alert step
                print(f"[PRICE ALERT ERROR] Failed notification for alert {alert['alert_id']}: {email_err}")

    except Exception as exc:
        conn.rollback()
        print(f"[TASK ERROR] check_and_notify_price_drops failed: {exc}")
        raise self.retry(exc=exc)
    finally:
        cursor.close()
        conn.close()


# ==========================================
# 2. PERIODIC SCRAPER & CLEANUP TASKS (BEAT)
# ==========================================

@celery_app.task
def run_catalog_scraper_job():
    """
    Triggered periodically by Celery Beat.
    Fetches active vendor URLs and dispatches background scraping workers.
    """
    print("[CELERY BEAT] Initiating scheduled full-catalog refresh...")
    conn = psycopg2.connect(settings.DATABASE_URL)
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    try:
        cursor.execute("""
            SELECT vo.vendor_id, vo.vendor_product_id, vo.product_url 
            FROM vendor_offers vo
            WHERE vo.in_stock = TRUE 
                OR vo.last_scraped_at < NOW() - INTERVAL '6 hours';
        """)
        offers_to_refresh = cursor.fetchall()
        print(f"[CELERY BEAT] Queuing {len(offers_to_refresh)} product URLs for scraping.")

        for offer in offers_to_refresh:
            scrape_single_vendor_product.delay(
                offer["vendor_id"], 
                offer["vendor_product_id"], 
                offer["product_url"]
            )
    except Exception as e:
        print(f"[CELERY BEAT ERROR] Catalog refresh job failed: {e}")
    finally:
        cursor.close()
        conn.close()


@celery_app.task(bind=True, max_retries=2, default_retry_delay=30)
def scrape_single_vendor_product(self, vendor_id: str, vendor_product_id: str, product_url: str):
    """Worker task to scrape a single product URL and run matching engine."""
    try:
        # Simulate scraper payload output
        scraped_payload = {
            "raw_title": "Sony WH-1000XM5 Wireless Headphones",
            "brand": "Sony",
            "vendor_id": vendor_id,
            "vendor_product_id": vendor_product_id,
            "price": 348.00,
            "product_url": product_url
        }

        # Process through product matching engine
        matched_product_id = process_incoming_scraped_payload(scraped_payload)

        # Invalidate API cache in Redis for this product grid
        if matched_product_id:
            redis_client.delete(f"cache:grid:{matched_product_id}")

    except Exception as e:
        raise self.retry(exc=e)


@celery_app.task
def run_priority_scraper_job():
    """Scheduled job for high-priority/hot deal items."""
    print("[CELERY BEAT] Running priority deal price refresh...")


@celery_app.task
def cleanup_stale_pending_matches():
    """Maintenance job to clear stale pending review matches older than 30 days."""
    conn = psycopg2.connect(settings.DATABASE_URL)
    cursor = conn.cursor()
    try:
        cursor.execute("""
            DELETE FROM vendor_offers 
            WHERE match_status = 'pending_review' 
            AND last_scraped_at < NOW() - INTERVAL '30 days';
        """)
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[MAINTENANCE ERROR] Cleanup failed: {e}")
    finally:
        cursor.close()
        conn.close()

@celery_app.task(bind=True, max_retries=3, default_retry_delay=120)
def process_vendor_scrape(self, offer_task: dict):
    """
    Executes a scrape for a single vendor listing, matches the entity,
    and logs price updates to PostgreSQL.
    """
    url = offer_task["product_url"]
    proxy = offer_task.get("proxy_url")
    
    try:
        # 1. Scrape the live vendor page via Playwright
        import asyncio
        scraped_data = asyncio.run(scrape_product_page(url, proxy))
        
        if "error" in scraped_data:
            raise Exception(f"Scraper error: {scraped_data['error']}")
            
        # 2. Attach vendor context to scraped payload
        scraped_data["vendor_id"] = offer_task["vendor_id"]
        scraped_data["vendor_product_id"] = offer_task["vendor_product_id"]
        
        # 3. Process offer through Entity Matcher & DB Trigger
        process_scraped_offer(scraped_data)
        
        return {"status": "success", "url": url, "price": scraped_data["price"]}
        
    except Exception as exc:
        # Retry up to 3 times on temporary network or proxy failures
        raise self.retry(exc=exc)


@celery_app.task
def dispatch_scheduled_updates():
    """
    Cron task executed by Celery Beat: Finds vendor offers last updated > 6 hours ago
    and queues them into Redis for scraping.
    """
    conn = psycopg2.connect(settings.DATABASE_URL)
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Fetch vendor offers where last_scraped_at is older than 6 hours
        cursor.execute("""
            SELECT vo.id AS offer_id, vo.vendor_id, vo.vendor_product_id, vo.product_url
            FROM vendor_offers vo
            JOIN vendors v ON vo.vendor_id = v.id
            WHERE v.is_active = TRUE 
                AND (vo.last_scraped_at IS NULL OR vo.last_scraped_at < NOW() - INTERVAL '6 hours')
            ORDER BY vo.last_scraped_at ASC NULLS FIRST
            LIMIT 500;
        """)
        
        stale_offers = cursor.fetchall()
        print(f"[Scheduler] Found {len(stale_offers)} stale vendor offers to refresh.")
        
        for offer in stale_offers:
            task_payload = {
                "offer_id": str(offer["offer_id"]),
                "vendor_id": str(offer["vendor_id"]),
                "vendor_product_id": offer["vendor_product_id"],
                "product_url": offer["product_url"]
            }
            # Dispatch task asynchronously to Redis queue
            process_vendor_scrape.delay(task_payload)
            
    finally:
        cursor.close()
        conn.close()

def send_email_notification(to_email: str, product_title: str, new_price: float, buy_url: str):
    """Helper to dispatch HTML emails via SendGrid."""
    if not SENDGRID_API_KEY:
        print(f"[MOCK EMAIL SENT] To: {to_email} | {product_title} dropped to ${new_price:.2f}")
        return

    sg = sendgrid.SendGridAPIClient(api_key=SENDGRID_API_KEY)
    subject = f"Price Drop Alert: {product_title} is now ${new_price:.2f}!"
    
    html_content = f"""
    <div style="font-family: sans-serif; padding: 20px;">
        <h2>Good news! An item on your watchlist dropped in price.</h2>
        <p><strong>{product_title}</strong> is now available for <strong>${new_price:.2f}</strong>.</p>
        <p><a href="{buy_url}" style="background-color: #10B981; color: white; padding: 10px 18px; text-decoration: none; border-radius: 5px; display: inline-block;">View Deal & Buy</a></p>
    </div>
    """
    
    mail = Mail(
        from_email=Email(FROM_EMAIL),
        to_emails=To(to_email),
        subject=subject,
        html_content=Content("text/html", html_content)
    )
    sg.client.mail.send.post(request_body=mail.get())

@app.task(bind=True, max_retries=3, default_retry_delay=60)
def scrape_offer(self, offer_id: str, target_url: str, proxy_url: str):
    try:
        # Import scraper inside task to avoid event loop conflicts
        from scraper import scrape_product_page
        data = asyncio.run(scrape_product_page(target_url, proxy_url))
        
        if "error" in data:
            raise Exception(data["error"])
            
        # Update database with new price
        save_price_to_db(offer_id, data["price"], data["in_stock"])
        return data
        
    except Exception as exc:
        # Retry with exponential backoff on proxy errors/failures
        raise self.retry(exc=exc)

def save_price_to_db(offer_id: str, price: float, in_stock: bool):
    # Place PostgreSQL / ORM update execution here
    print(f"Updated Offer {offer_id}: ${price} (In Stock: {in_stock})")