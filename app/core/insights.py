# app/core/insights.py
import json
from typing import Any, Dict, List

from psycopg2.extras import RealDictCursor

from app.db_sync import get_conn, redis_client

CACHE_TTL_SECONDS = 3600


def calculate_buy_timing_recommendation(product_id: str) -> Dict[str, Any]:
    """
    Analyzes true historical price data over the past 90 days to determine
    whether a user should buy now or wait.
    """
    cache_key = f"cache:insights:recommendation:{product_id}"
    cached_data = redis_client.get(cache_key)
    if cached_data:
        return json.loads(cached_data)

    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT
                MIN(ph.price) AS min_price_90d,
                AVG(ph.price) AS avg_price_90d,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ph.price) AS median_price_90d,
                MAX(ph.recorded_at) AS last_recorded
            FROM price_history ph
            JOIN vendor_offers vo ON ph.offer_id = vo.id
            WHERE vo.product_id = %s::uuid
              AND ph.recorded_at >= NOW() - INTERVAL '90 days';
            """,
            (product_id,),
        )
        stats = cursor.fetchone()

        cursor.execute(
            """
            SELECT MIN(vo.current_price) AS current_price
            FROM vendor_offers vo
            WHERE vo.product_id = %s::uuid AND vo.in_stock = TRUE AND vo.match_status = 'matched';
            """,
            (product_id,),
        )
        current_row = cursor.fetchone()

    if not stats or stats["min_price_90d"] is None:
        return {"recommendation": "NEUTRAL", "reason": "Insufficient price data available."}

    current = float(current_row["current_price"]) if current_row and current_row["current_price"] else None
    avg = float(stats["avg_price_90d"]) if stats["avg_price_90d"] else current
    median = float(stats["median_price_90d"]) if stats["median_price_90d"] else current
    min_90d = float(stats["min_price_90d"])

    if current is None:
        return {"recommendation": "NEUTRAL", "reason": "Product currently out of stock everywhere."}

    if current <= min_90d:
        action, confidence = "BUY_NOW", "HIGH"
        reason = f"Current price (${current:.2f}) matches or beats the lowest price recorded in the past 90 days!"
    elif current < avg * 0.95:
        action, confidence = "BUY_NOW", "MEDIUM"
        reason = f"Price is {((avg - current) / avg * 100):.1f}% below the 90-day average price of ${avg:.2f}."
    else:
        action, confidence = "WAIT", "MEDIUM"
        reason = f"Price is above historical lows. Consider waiting — median prices hover around ${median:.2f}."

    result = {
        "product_id": product_id,
        "action": action,
        "confidence": confidence,
        "current_lowest_price": current,
        "historical_avg_price": round(avg, 2),
        "historical_median_price": round(median, 2),
        "historical_min_price": min_90d,
        "recommendation_reason": reason,
    }

    redis_client.setex(cache_key, CACHE_TTL_SECONDS, json.dumps(result))
    return result


def find_feature_equivalent_alternatives(product_id: str, limit: int = 3) -> List[Dict[str, Any]]:
    """
    Uses pgvector cosine distance to find cross-brand products with 75%+
    vector spec similarity that cost less than the current product.
    """
    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        cursor.execute(
            """
            SELECT p.id, p.title, p.brand, p.title_embedding, MIN(vo.current_price) AS current_price
            FROM products p
            JOIN vendor_offers vo ON vo.product_id = p.id
            WHERE p.id = %s::uuid AND vo.in_stock = TRUE
            GROUP BY p.id;
            """,
            (product_id,),
        )
        target = cursor.fetchone()
        if not target or not target["current_price"]:
            return []

        target_price = float(target["current_price"])

        cursor.execute(
            """
            SELECT
                p.id AS alternative_id, p.title, p.brand,
                MIN(vo.current_price) AS alternative_price,
                1 - (p.title_embedding <=> %s::vector) AS spec_similarity
            FROM products p
            JOIN vendor_offers vo ON vo.product_id = p.id
            WHERE p.id != %s::uuid
              AND vo.in_stock = TRUE
              AND (1 - (p.title_embedding <=> %s::vector)) >= 0.75
            GROUP BY p.id
            HAVING MIN(vo.current_price) < %s
            ORDER BY spec_similarity DESC
            LIMIT %s;
            """,
            (target["title_embedding"], product_id, target["title_embedding"], target_price, limit),
        )
        alternatives = cursor.fetchall()

    results = []
    for alt in alternatives:
        alt_price = float(alt["alternative_price"])
        savings = target_price - alt_price
        savings_pct = (savings / target_price) * 100
        results.append({
            "product_id": alt["alternative_id"],
            "title": alt["title"],
            "brand": alt["brand"],
            "price": alt_price,
            "similarity_score": round(float(alt["spec_similarity"]), 2),
            "potential_savings": round(savings, 2),
            "savings_percentage": round(savings_pct, 1),
        })
    return results


def hybrid_product_search(search_text: str, query_vector: List[float], limit: int = 5):
    """RRF combining pgvector + full-text search."""
    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            WITH vector_ranks AS (
                SELECT id, ROW_NUMBER() OVER (ORDER BY title_embedding <=> %s::vector) AS rank
                FROM products LIMIT 50
            ),
            fts_ranks AS (
                SELECT id, ROW_NUMBER() OVER (ORDER BY ts_rank(text_search_tsv, plainto_tsquery('english', %s)) DESC) AS rank
                FROM products
                WHERE text_search_tsv @@ plainto_tsquery('english', %s)
                LIMIT 50
            )
            SELECT
                p.id, p.title, p.brand,
                COALESCE(1.0 / (60 + vr.rank), 0.0) + COALESCE(1.0 / (60 + fr.rank), 0.0) AS rrf_score
            FROM products p
            LEFT JOIN vector_ranks vr ON p.id = vr.id
            LEFT JOIN fts_ranks fr ON p.id = fr.id
            WHERE vr.rank IS NOT NULL OR fr.rank IS NOT NULL
            ORDER BY rrf_score DESC
            LIMIT %s;
            """,
            (query_vector, search_text, search_text, limit),
        )
        return cursor.fetchall()
