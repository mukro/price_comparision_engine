# app/core/dynamic_pricing.py
from typing import Any, Dict, Optional

from psycopg2.extras import RealDictCursor

from app.db_sync import get_conn


def calculate_counter_price(
    merchant_cost: float,
    min_margin_pct: float,
    map_price: Optional[float],
    competitor_price: float,
    strategy: str = "undercut_by_fixed",
    strategy_value: float = 1.00,
) -> Dict[str, Any]:
    """
    Calculates the safest, most competitive repricing strategy without
    breaching merchant profit margins or MAP rules.
    """
    margin_floor = merchant_cost * (1 + (min_margin_pct / 100.0))
    hard_floor = max(margin_floor, map_price) if map_price else margin_floor

    if strategy == "undercut_by_fixed":
        target_price = competitor_price - strategy_value
    elif strategy == "undercut_by_pct":
        target_price = competitor_price * (1 - (strategy_value / 100.0))
    elif strategy == "match":
        target_price = competitor_price
    else:
        target_price = competitor_price - 0.01

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
        "reason": reason,
    }


def check_repricing_circuit_breaker(
    merchant_id: str,
    product_id: str,
    target_price: float,
    max_daily_drop_pct: float = 10.0,
    conn=None,
) -> Dict[str, Any]:
    """
    Caps how fast an auto-repricer is allowed to drop a merchant's price in
    a rolling 24h window, using the earliest logged price in that window as
    the baseline. Prevents a bad competitor scrape / bug from crashing a
    merchant's price to zero.
    """
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute(
        """
        SELECT old_price FROM price_audit_logs
        WHERE merchant_id = %s AND product_id = %s::uuid
            AND created_at >= NOW() - INTERVAL '24 hours'
        ORDER BY created_at ASC LIMIT 1;
        """,
        (merchant_id, product_id),
    )
    baseline = cursor.fetchone()
    if baseline:
        initial_price = float(baseline["old_price"])
        max_allowed_drop = initial_price * (1.0 - (max_daily_drop_pct / 100.0))
        if target_price < max_allowed_drop:
            return {
                "tripped": True,
                "safe_price": round(max_allowed_drop, 2),
                "reason": f"Circuit breaker tripped: price drop capped at {max_daily_drop_pct}% per 24h.",
            }
    return {"tripped": False, "safe_price": target_price, "reason": "Within safe velocity limits."}


def evaluate_merchant_rules_for_product(
    master_product_id: str, new_competitor_price: float
) -> Optional[Dict[str, Any]]:
    """
    Fetches configured merchant rules for a product, computes the
    counter-strategy for each merchant tracking it, and applies the
    circuit breaker as a final safety check before returning a
    recommendation.
    """
    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        cursor.execute(
            """
            SELECT
                mr.id AS rule_id, mr.merchant_id, mr.product_id, mr.merchant_cost,
                mr.min_margin_pct, mr.map_price, mr.strategy, mr.strategy_value,
                mr.webhook_url, mr.auto_apply_enabled
            FROM merchant_rules mr
            WHERE mr.product_id = %s::uuid AND mr.is_active = TRUE;
            """,
            (master_product_id,),
        )
        rules = cursor.fetchall()
        if not rules:
            return None

        evaluations = []
        for rule in rules:
            strategy_result = calculate_counter_price(
                merchant_cost=float(rule["merchant_cost"]),
                min_margin_pct=float(rule["min_margin_pct"]),
                map_price=float(rule["map_price"]) if rule["map_price"] else None,
                competitor_price=new_competitor_price,
                strategy=rule["strategy"],
                strategy_value=float(rule["strategy_value"]),
            )

            breaker = check_repricing_circuit_breaker(
                merchant_id=rule["merchant_id"],
                product_id=master_product_id,
                target_price=strategy_result["recommended_price"],
                conn=conn,
            )
            if breaker["tripped"]:
                strategy_result["recommended_price"] = breaker["safe_price"]
                strategy_result["reason"] += f" {breaker['reason']}"

            evaluations.append(
                {
                    "rule_id": rule["rule_id"],
                    "merchant_id": rule["merchant_id"],
                    "webhook_url": rule["webhook_url"],
                    "auto_apply": rule["auto_apply_enabled"],
                    "circuit_breaker_tripped": breaker["tripped"],
                    "strategy_result": strategy_result,
                }
            )

        return {"product_id": master_product_id, "merchant_evaluations": evaluations}
