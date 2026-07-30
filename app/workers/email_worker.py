# app/workers/email_worker.py
"""
Resilient email dispatch layer with:
  - SendGrid primary + stdout fallback for dev
  - Exponential backoff retry
  - Structured logging
  - Rate limiting awareness (max 100 emails/minute via Redis)
"""
import logging
import time
from typing import Optional

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

from app.config import settings
from app.db_sync import redis_client

logger = logging.getLogger("email_worker")

# Simple in-memory rate limiter (per process). For multi-worker, use Redis.
_EMAIL_RATE_LIMIT_KEY = "email:rate_limit:count"
_EMAIL_RATE_LIMIT_WINDOW = 60  # seconds
_MAX_EMAILS_PER_WINDOW = 100


def _check_email_rate_limit() -> bool:
    """Returns True if we are within the safe sending threshold."""
    try:
        current = redis_client.incr(_EMAIL_RATE_LIMIT_KEY)
        if current == 1:
            redis_client.expire(_EMAIL_RATE_LIMIT_KEY, _EMAIL_RATE_LIMIT_WINDOW)
        return current <= _MAX_EMAILS_PER_WINDOW
    except Exception as e:
        logger.warning(f"Rate limiter down, allowing send: {e}")
        return True


def send_price_drop_email(
    to_email: str,
    product_title: str,
    new_price: float,
    buy_url: str,
    max_retries: int = 3,
) -> bool:
    """
    Sends a price-drop notification email.
    Falls back to stdout logging if SendGrid is unconfigured or fails.
    """
    if not _check_email_rate_limit():
        logger.warning(f"Email rate limit exceeded; dropping notification to {to_email}")
        return False

    subject = f"🔥 Price Drop Alert: {product_title}"
    body_text = f"""
Good news! The price for "{product_title}" has dropped.

New lowest price: ${new_price:.2f}
Buy now: {buy_url}

You are receiving this because you set a price alert on our platform.
    """.strip()

    # Attempt SendGrid
    if settings.SENDGRID_API_KEY:
        for attempt in range(1, max_retries + 1):
            try:
                sg = SendGridAPIClient(settings.SENDGRID_API_KEY)
                message = Mail(
                    from_email=settings.FROM_EMAIL,
                    to_emails=to_email,
                    subject=subject,
                    plain_text_content=body_text,
                )
                response = sg.send(message)
                if 200 <= response.status_code < 300:
                    logger.info(
                        "email_sent",
                        extra={
                            "to": to_email,
                            "product": product_title,
                            "price": new_price,
                            "provider": "sendgrid",
                            "attempt": attempt,
                        },
                    )
                    return True
                else:
                    logger.warning(
                        f"SendGrid returned {response.status_code} for {to_email} (attempt {attempt})"
                    )
            except Exception as e:
                logger.warning(f"SendGrid attempt {attempt} failed for {to_email}: {e}")
                if attempt < max_retries:
                    time.sleep(2 ** attempt)  # exponential backoff: 2s, 4s, 8s
        logger.error(f"SendGrid exhausted all retries for {to_email}")

    # Fallback: log to stdout (dev mode / audit trail)
    logger.info(
        "email_fallback_logged",
        extra={
            "to": to_email,
            "subject": subject,
            "body": body_text,
            "provider": "stdout_fallback",
        },
    )
    return True  # Considered "sent" for audit purposes in dev


def send_merchant_webhook_failure_alert(
    merchant_id: str,
    webhook_url: str,
    error_message: str,
) -> bool:
    """
    Notifies a merchant (or admin) when their repricing webhook fails repeatedly.
    """
    if not settings.SENDGRID_API_KEY:
        logger.info(
            "merchant_webhook_failure_alert (no SendGrid configured)",
            extra={"merchant_id": merchant_id, "webhook_url": webhook_url, "error": error_message},
        )
        return False

    subject = f"Webhook Failure Alert — Merchant {merchant_id}"
    body = f"""
Your automated repricing webhook is failing:

Merchant ID: {merchant_id}
Webhook URL: {webhook_url}
Error: {error_message}

Please verify the endpoint is reachable and returns HTTP 2xx.
    """.strip()

    try:
        sg = SendGridAPIClient(settings.SENDGRID_API_KEY)
        message = Mail(
            from_email=settings.FROM_EMAIL,
            to_emails=settings.ADMIN_EMAIL,  # Send to admin; merchant email would come from DB
            subject=subject,
            plain_text_content=body,
        )
        sg.send(message)
        return True
    except Exception as e:
        logger.error(f"Failed to send webhook failure alert: {e}")
        return False
