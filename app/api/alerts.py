# app/api/alerts.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr, Field

from app.db import get_db_pool

router = APIRouter(prefix="/api/v1/alerts", tags=["Price Drop Alerts"])


class CreateAlertSchema(BaseModel):
    email: EmailStr
    product_id: str
    target_price: float = Field(..., gt=0, description="Target price threshold in USD")


@router.post("")
async def create_price_alert(payload: CreateAlertSchema):
    """Subscribe a user to receive an email when a product drops below target_price."""
    pool = get_db_pool()
    async with pool.acquire() as conn:
        product = await conn.fetchrow("SELECT id, title FROM products WHERE id = $1::uuid;", payload.product_id)
        if not product:
            raise HTTPException(status_code=404, detail="Product not found")

        alert_row = await conn.fetchrow(
            """
            INSERT INTO user_alerts (user_email, product_id, target_price, is_active)
            VALUES ($1, $2::uuid, $3, TRUE)
            ON CONFLICT (user_email, product_id)
            DO UPDATE SET target_price = EXCLUDED.target_price, is_active = TRUE
            RETURNING id;
            """,
            payload.email, payload.product_id, payload.target_price,
        )

    return {
        "status": "success",
        "message": f"Alert set for '{product['title']}' at ${payload.target_price:.2f}",
        "alert_id": str(alert_row["id"]),
    }


@router.delete("/{alert_id}")
async def cancel_price_alert(alert_id: str):
    """Unsubscribe from a price drop alert."""
    pool = get_db_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("UPDATE user_alerts SET is_active = FALSE WHERE id = $1::uuid;", alert_id)

    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"status": "success", "message": "Alert deactivated"}
