# app/api/merchant.py
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.admin import require_admin
from app.core.dynamic_pricing import evaluate_merchant_rules_for_product
from app.db_sync import get_conn

# NOTE: protected with require_admin for now (any authenticated admin can
# configure any merchant's rules). Before opening this up to merchants
# directly, add a merchant-scoped JWT claim + row-level check so a
# merchant can only read/write rules for their own merchant_id -- as-is,
# an authenticated caller could see or edit another merchant's cost/margin
# data.
router = APIRouter(prefix="/api/v1/merchant", tags=["B2B Merchant Pricing Engine"])


class MerchantRuleSchema(BaseModel):
    merchant_id: str
    product_id: str
    merchant_cost: float = Field(gt=0)
    min_margin_pct: float = Field(default=15.0, ge=0)
    map_price: Optional[float] = Field(default=None, ge=0)
    strategy: str = Field(default="undercut_by_fixed")  # 'undercut_by_fixed', 'undercut_by_pct', 'match'
    strategy_value: float = Field(default=1.00)
    webhook_url: Optional[str] = None
    auto_apply_enabled: bool = False


@router.post("/rules", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_admin)])
def create_or_update_merchant_rule(payload: MerchantRuleSchema):
    """Configures merchant floor rules, MAP guidelines, and repricing strategy for a product."""
    with get_conn() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO merchant_rules (
                    merchant_id, product_id, merchant_cost, min_margin_pct,
                    map_price, strategy, strategy_value, webhook_url, auto_apply_enabled
                ) VALUES (%s, %s::uuid, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (merchant_id, product_id) DO UPDATE SET
                    merchant_cost = EXCLUDED.merchant_cost,
                    min_margin_pct = EXCLUDED.min_margin_pct,
                    map_price = EXCLUDED.map_price,
                    strategy = EXCLUDED.strategy,
                    strategy_value = EXCLUDED.strategy_value,
                    webhook_url = EXCLUDED.webhook_url,
                    auto_apply_enabled = EXCLUDED.auto_apply_enabled,
                    updated_at = NOW();
                """,
                (
                    payload.merchant_id, payload.product_id, payload.merchant_cost,
                    payload.min_margin_pct, payload.map_price, payload.strategy,
                    payload.strategy_value, payload.webhook_url, payload.auto_apply_enabled,
                ),
            )
            conn.commit()
            return {"status": "success", "message": "Merchant dynamic pricing rule configured successfully."}
        except Exception as e:
            conn.rollback()
            raise HTTPException(status_code=500, detail=f"Database execution failed: {e}")


@router.get("/evaluate/{product_id}", dependencies=[Depends(require_admin)])
def simulate_counter_strategy(product_id: str, competitor_price: float):
    """Simulates counter-strategy pricing for a given competitor price. Reveals merchant cost/margin -- admin only."""
    evaluations = evaluate_merchant_rules_for_product(product_id, competitor_price)
    if not evaluations:
        raise HTTPException(status_code=404, detail="No active merchant rules found for this product.")
    return {"status": "success", "data": evaluations}
