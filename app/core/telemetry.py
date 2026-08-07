# app/core/telemetry.py
import psycopg2

from app.config import settings


def log_price_audit_event(
    merchant_id: str,
    product_id: str,
    old_price: float,
    new_price: float,
    event_type: str,
    circuit_breaker: bool = False
):
    """
    Logs every automated repricing action to build audit trails for B2B merchants.
    """
    conn = psycopg2.connect(settings.DATABASE_URL)
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO price_audit_logs (
                merchant_id, product_id, old_price, new_price, trigger_event, circuit_breaker_tripped
            ) VALUES (%s, %s::uuid, %s, %s, %s, %s);
        """, (merchant_id, product_id, old_price, new_price, event_type, circuit_breaker))
        conn.commit()
    finally:
        cursor.close()
        conn.close()