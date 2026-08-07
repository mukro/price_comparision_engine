# app/core/dynamic_pricing.py
from typing import Any, Dict, Optional

import psycopg2
from psycopg2.extras import RealDictCursor

from app.config import settings


def calculate_counter_price(
    merchant_cost: float,
    min_margin_pct: float,
    map_price: Optional[float],
    competitor_price: float,
    strategy: str = "undercut_by_fixed",
    strategy_value: float = 1.00  # Undercut by $1.00 or 1% depending on strategy
) -> Dict[str, Any]:
    """
    Calculates the safest, most competitive repricing strategy 
    without breaching merchant profit margins or MAP rules.
    """
    # 1. Calculate absolute floor price based on min margin
    margin_floor = merchant_cost * (1 + (min_margin_pct / 100.0))
    
    # 2. Hard floor is max of margin floor and MAP limit
    hard_floor = max(margin_floor, map_price) if map_price else margin_floor

    # 3. Calculate target price based on strategy
    if strategy == "undercut_by_fixed":
        target_price = competitor_price - strategy_value
    elif strategy == "undercut_by_pct":
        target_price = competitor_price * (1 - (strategy_value / 100.0))
    elif strategy == "match":
        target_price = competitor_price
    else:
        target_price = competitor_price - 0.01  # Default 1 cent undercut

    # 4. Enforce Floor Constraint
    if target_price < hard_floor:
        final_price = hard_floor
        floor_hit = True
        reason = f"Calculated price (${target_price:.2f}) was below safe margin/MAP floor. Capped at ${hard_floor:.2f}."
    else:
        final_price = target_price
        floor_hit = False
        reason = f"Successfully calculated optimal counter-price of ${final_price:.2f} against competitor's ${competitor_price:.2f}."

    return {
        "recommended_price": round(final_price, 2),
        "competitor_price": competitor_price,
        "hard_floor": round(hard_floor, 2),
        "floor_hit": floor_hit,
        "strategy_applied": strategy,
        "reason": reason
    }


def evaluate_merchant_rules_for_product(master_product_id: str, new_competitor_price: float) -> Optional[Dict[str, Any]]:
    """
    Fetches configured merchant rules for a product and computes counter-strategy.
    """
    conn = psycopg2.connect(settings.DATABASE_URL)
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    try:
        cursor.execute("""
            SELECT 
                mr.id AS rule_id,
                mr.merchant_id,
                mr.product_id,
                mr.merchant_cost,
                mr.min_margin_pct,
                mr.map_price,
                mr.strategy,
                mr.strategy_value,
                mr.webhook_url,
                mr.auto_apply_enabled
            FROM merchant_rules mr
            WHERE mr.product_id = %s::uuid 
                AND mr.is_active = TRUE;
        """, (master_product_id,))

        rules = cursor.fetchall()
        if not rules:
            return None

        # Process rules per merchant tracking this item
        evaluations = []
        for rule in rules:
            strategy_result = calculate_counter_price(
                merchant_cost=float(rule["merchant_cost"]),
                min_margin_pct=float(rule["min_margin_pct"]),
                map_price=float(rule["map_price"]) if rule["map_price"] else None,
                competitor_price=new_competitor_price,
                strategy=rule["strategy"],
                strategy_value=float(rule["strategy_value"])
            )

            evaluations.append({
                "rule_id": rule["rule_id"],
                "merchant_id": rule["merchant_id"],
                "webhook_url": rule["webhook_url"],
                "auto_apply": rule["auto_apply_enabled"],
                "strategy_result": strategy_result
            })

        return {"product_id": master_product_id, "merchant_evaluations": evaluations}

    finally:
        cursor.close()
        conn.close()

def check_repricing_circuit_breaker(
    merchant_id: str,
    product_id: str,
    target_price: float,
    max_daily_drop_pct: float = 10.0,
    conn = None
) -> Dict[str, Any]:
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("""
            SELECT old_price FROM price_audit_logs 
            WHERE merchant_id = %s AND product_id = %s::uuid
                AND created_at >= NOW() - INTERVAL '24 hours'
            ORDER BY created_at ASC LIMIT 1;
        """, (merchant_id, product_id))
        
        baseline = cursor.fetchone()
        if baseline:
            initial_price = float(baseline["old_price"])
            max_allowed_drop = initial_price * (1.0 - (max_daily_drop_pct / 100.0))
            if target_price < max_allowed_drop:
                return {
                    "tripped": True,
                    "safe_price": round(max_allowed_drop, 2),
                    "reason": f"Circuit breaker tripped: Price drop capped at {max_daily_drop_pct}% per 24h."
                }
        return {"tripped": False, "safe_price": target_price, "reason": "Within safe velocity limits."}
    finally:
        cursor.close()