# app/core/telemetry.py
from psycopg2.extras import RealDictCursor

from app.db_sync import get_conn


def log_price_audit_event(
    merchant_id: str,
    product_id: str,
    old_price: float,
    new_price: float,
    event_type: str,
    circuit_breaker: bool = False,
) -> None:
    """Logs every automated repricing action to build audit trails for B2B merchants."""
    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            INSERT INTO price_audit_logs (
                merchant_id, product_id, old_price, new_price, trigger_event, circuit_breaker_tripped
            ) VALUES (%s, %s::uuid, %s, %s, %s, %s);
            """,
            (merchant_id, product_id, old_price, new_price, event_type, circuit_breaker),
        )
        conn.commit()
