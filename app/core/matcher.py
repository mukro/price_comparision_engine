# app/core/matcher.py
"""
Core Entity Resolution Pipeline with:
  - Lazy model loading
  - Better spec extraction (regex + fallback)
  - Confidence threshold with admin feedback awareness
  - Price-drop alert only on actual decreases
  - Cache invalidation
"""
import logging
import re
from functools import lru_cache
from typing import Optional

from psycopg2.extras import Json, RealDictCursor
from rapidfuzz import fuzz

from app.db_sync import get_conn, invalidate_grid_cache
from app.core.telemetry_metrics import MATCHING_CONFIDENCE_HISTOGRAM

logger = logging.getLogger("matcher")


@lru_cache(maxsize=1)
def _get_model():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer("all-MiniLM-L6-v2")


def get_embedding(text: str) -> list[float]:
    return _get_model().encode(text, convert_to_numpy=True).tolist()


def extract_specifications(title: str) -> dict:
    """
    Extracts key product attributes using regex heuristics.
    Quick-commerce specific: handles pack sizes, weights, volumes.
    """
    t = title.lower()

    # Storage: 128GB, 1TB, etc.
    storage_match = re.search(r"\b(\d+\s?(gb|tb|mb))\b", t)
    # Weight/volume: 500g, 1kg, 250ml, 2L, 1.5L
    weight_match = re.search(r"\b(\d+(?:\.\d+)?\s?(kg|g|ml|l|ltr|litre))\b", t)
    # Pack size: Pack of 6, (6 x 100g), 6pcs
    pack_match = re.search(r"(?:pack\s*of\s*(\d+)|\((\d+)\s*x\s*\d+|(\d+)\s*pcs)", t)
    # Model code: alphanumeric patterns like "WH-1000XM5", "iPhone15"
    model_match = re.search(r"\b([a-z]*\d+[a-z]*(?:[-\s]?[a-z0-9]+)?)\b", t)

    return {
        "storage": storage_match.group(1).replace(" ", "").upper() if storage_match else None,
        "weight": weight_match.group(0).replace(" ", "").lower() if weight_match else None,
        "pack_size": next((x for x in [pack_match.group(i) for i in range(1, 4)] if x), None),
        "model_code": model_match.group(1).upper() if model_match else None,
    }


def _get_confidence_thresholds() -> tuple[float, float]:
    """
    Reads per-category thresholds from DB or falls back to defaults.
    In future, train these from match_feedback table.
    """
    # TODO: read from merchant_rules or a new table `match_thresholds`
    return 0.82, 0.60  # (auto_match, pending_review)


def process_scraped_offer(scraped_item: dict) -> Optional[str]:
    """
    Ingests one scraped payload: resolves to master product, upserts offer,
    logs price_history, and triggers alerts only on genuine price drops.
    """
    raw_title = scraped_item["raw_title"][:300].strip()
    brand = scraped_item.get("brand")
    vendor_id = scraped_item["vendor_id"]
    vendor_product_id = scraped_item["vendor_product_id"]
    price = float(scraped_item["price"])
    in_stock = scraped_item.get("in_stock", True)
    product_url = scraped_item["product_url"]
    affiliate_url = scraped_item.get("affiliate_url")

    embedding = get_embedding(raw_title)
    scraped_specs = extract_specifications(raw_title)
    auto_match_thresh, pending_thresh = _get_confidence_thresholds()

    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        try:
            matched_product_id = None
            best_confidence = 0.0

            # Step 1: pgvector candidate search
            cursor.execute(
                """
                SELECT id, title, brand, 1 - (title_embedding <=> %s::vector) AS similarity
                FROM products
                WHERE 1 - (title_embedding <=> %s::vector) > %s
                ORDER BY title_embedding <=> %s::vector
                LIMIT 5;
                """,
                (embedding, embedding, pending_thresh, embedding),
            )
            candidates = cursor.fetchall()

            # Step 2 & 3: hard filters + hybrid scoring
            for candidate in candidates:
                cand_specs = extract_specifications(candidate["title"])

                if brand and candidate["brand"] and brand.lower() != candidate["brand"].lower():
                    continue
                if scraped_specs["storage"] and cand_specs["storage"]:
                    if scraped_specs["storage"] != cand_specs["storage"]:
                        continue
                if scraped_specs["weight"] and cand_specs["weight"]:
                    if scraped_specs["weight"] != cand_specs["weight"]:
                        continue

                token_score = fuzz.token_sort_ratio(raw_title, candidate["title"]) / 100.0
                combined_confidence = (candidate["similarity"] * 0.5) + (token_score * 0.5)

                if combined_confidence > best_confidence:
                    best_confidence = combined_confidence
                    matched_product_id = candidate["id"]

            # Step 4: routing decision
            if matched_product_id and best_confidence >= auto_match_thresh:
                status = "matched"
            elif matched_product_id and pending_thresh <= best_confidence < auto_match_thresh:
                status = "pending_review"
            else:
                cursor.execute(
                    """
                    INSERT INTO products (title, brand, model_code, specifications, title_embedding)
                    VALUES (%s, %s, %s, %s, %s::vector)
                    RETURNING id;
                    """,
                    (raw_title, brand, scraped_specs["model_code"], Json(scraped_specs), embedding),
                )
                matched_product_id = cursor.fetchone()["id"]
                status = "matched"

            # Record confidence histogram for observability
            MATCHING_CONFIDENCE_HISTOGRAM.observe(best_confidence)

            # Step 5: upsert vendor offer
            cursor.execute(
                """
                INSERT INTO vendor_offers (
                    product_id, vendor_id, vendor_product_id, raw_title,
                    product_url, affiliate_url, current_price, in_stock,
                    match_status, confidence_score, last_scraped_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (vendor_id, vendor_product_id)
                DO UPDATE SET
                    product_id = EXCLUDED.product_id,
                    current_price = EXCLUDED.current_price,
                    in_stock = EXCLUDED.in_stock,
                    match_status = EXCLUDED.match_status,
                    confidence_score = EXCLUDED.confidence_score,
                    affiliate_url = COALESCE(EXCLUDED.affiliate_url, vendor_offers.affiliate_url),
                    last_scraped_at = NOW()
                RETURNING id;
                """,
                (
                    matched_product_id, vendor_id, vendor_product_id, raw_title,
                    product_url, affiliate_url, price, in_stock,
                    status, best_confidence,
                ),
            )
            offer_id = cursor.fetchone()["id"]

            # Step 6: append-only price_history
            cursor.execute(
                """
                INSERT INTO price_history (offer_id, price, in_stock, recorded_at)
                VALUES (%s, %s, %s, NOW());
                """,
                (offer_id, price, in_stock),
            )

            conn.commit()

            # Step 7: post-processing
            if status == "matched":
                invalidate_grid_cache(str(matched_product_id))

                # Find previous lowest price (before this scrape)
                cursor.execute(
                    """
                    SELECT MIN(current_price) AS prev_lowest
                    FROM vendor_offers
                    WHERE product_id = %s::uuid
                      AND in_stock = TRUE
                      AND match_status = 'matched'
                      AND last_scraped_at < NOW() - INTERVAL '1 hour';
                    """,
                    (matched_product_id,),
                )
                prev_row = cursor.fetchone()
                prev_lowest = float(prev_row["prev_lowest"]) if prev_row and prev_row["prev_lowest"] else None

                # Current lowest after this update
                cursor.execute(
                    """
                    SELECT MIN(current_price) AS lowest_price
                    FROM vendor_offers
                    WHERE product_id = %s::uuid AND in_stock = TRUE AND match_status = 'matched';
                    """,
                    (matched_product_id,),
                )
                lowest_row = cursor.fetchone()
                lowest_price = float(lowest_row["lowest_price"]) if lowest_row and lowest_row["lowest_price"] else None

                if lowest_price is not None:
                    # Only alert if this is a genuine new low or first time
                    is_new_low = prev_lowest is None or lowest_price < prev_lowest
                    if is_new_low:
                        from app.tasks import (
                            check_and_notify_price_drops,
                            dispatch_merchant_repricing_webhooks,
                        )
                        check_and_notify_price_drops.delay(str(matched_product_id), lowest_price)
                        dispatch_merchant_repricing_webhooks.delay(str(matched_product_id), lowest_price)

            return str(matched_product_id)

        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to process offer '{raw_title}': {e}")
            raise
