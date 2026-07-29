# app/workers/email_worker.py
import logging

import sendgrid
from sendgrid.helpers.mail import Content, Email, Mail, To

from app.config import settings

logger = logging.getLogger("email_worker")


def render_price_drop_html(product_title: str, new_price: float, buy_url: str) -> str:
    """Generates the responsive HTML body for price-drop alert emails."""
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f4f5; margin: 0; padding: 20px; }}
            .card {{ max-width: 550px; margin: 0 auto; background: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1); }}
            .header {{ background-color: #0f172a; padding: 24px; text-align: center; color: #ffffff; }}
            .content {{ padding: 32px 24px; color: #334155; }}
            .price-tag {{ font-size: 32px; font-weight: bold; color: #059669; margin: 16px 0; }}
            .cta-button {{ display: inline-block; background-color: #2563eb; color: #ffffff !important; font-weight: bold; padding: 14px 28px; text-decoration: none; border-radius: 6px; margin-top: 16px; }}
            .footer {{ padding: 20px; text-align: center; font-size: 12px; color: #94a3b8; background-color: #f8fafc; }}
        </style>
    </head>
    <body>
        <div class="card">
            <div class="header">
                <h1 style="margin:0; font-size: 20px;">Price Drop Notification</h1>
            </div>
            <div class="content">
                <p>Great news! An item on your price watchlist just reached your target threshold:</p>
                <h3 style="margin-bottom: 8px; color: #0f172a;">{product_title}</h3>
                <div class="price-tag">${new_price:.2f}</div>
                <p>Click the button below to view the offer directly on the store page:</p>
                <div style="text-align: center;">
                    <a href="{buy_url}" class="cta-button" target="_blank">Buy Now</a>
                </div>
            </div>
            <div class="footer">
                You are receiving this because you created an alert on Price Comparison Platform.<br>
                To manage your alerts, please visit your account dashboard.
            </div>
        </div>
    </body>
    </html>
    """


def send_price_drop_email(to_email: str, product_title: str, new_price: float, buy_url: str) -> bool:
    """Dispatches a transactional price-drop email via SendGrid, or logs a mock in dev when no API key is set."""
    if not settings.SENDGRID_API_KEY:
        logger.info(f"[DEV MOCK EMAIL] To: {to_email} | Product: '{product_title}' @ ${new_price:.2f}")
        return True

    try:
        html_body = render_price_drop_html(product_title, new_price, buy_url)
        sg = sendgrid.SendGridAPIClient(api_key=settings.SENDGRID_API_KEY)

        message = Mail(
            from_email=Email(settings.FROM_EMAIL),
            to_emails=To(to_email),
            subject=f"Price Alert: {product_title} is down to ${new_price:.2f}!",
            html_content=Content("text/html", html_body),
        )

        response = sg.client.mail.send.post(request_body=message.get())
        if response.status_code in (200, 201, 202):
            logger.info(f"Dispatched price drop alert email to {to_email}.")
            return True
        logger.error(f"SendGrid API returned status code {response.status_code}.")
        return False

    except Exception as e:
        logger.error(f"Failed to send email via SendGrid to {to_email}: {e}")
        return False
