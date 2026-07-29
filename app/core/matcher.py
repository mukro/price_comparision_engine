# app/core/matcher.py
"""
Core Entity Resolution Pipeline.

1. Vector Search (pgvector) -> Retrieve top 5 semantic candidates.
2. Hard Filtering -> Eliminate brand/spec mismatches.
3. Hybrid Scoring -> Vector similarity (50%) + Fuzzy Token Match (50%).
4. Threshold Routing:
   - > 0.82 Confidence  --> Auto-Match
   - 0.60 - 0.81        --> Send to Admin HITL Review Queue
   - < 0.60              --> Auto-Create new Master Product
5. Price history logging, cache invalidation & price-alert dispatch.

NOTE: this module loads a sentence-transformers model at import time. It
should only ever be imported by Celery workers, never by the FastAPI app
process (the API layer never needs it directly).
"""
import logging
import re
from functools import lru_cache

from psycopg2.extras import Json, RealDictCursor
from rapidfuzz import fuzz

from app.db_sync import get_conn, invalidate_grid_cache

logger = logging.getLogger("matcher")


@lru_cache(maxsize=1)
def _get_model():
    """Lazily load the embedding model once per process, on first use."""
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer("all-MiniLM-L6-v2")


def get_embedding(text: str) -> list[float]:
    """Generates a 384-dimensional dense vector representation of the product title."""
    return _get_model().encode(text, convert_to_numpy=True).tolist()


def extract_specifications(title: str) -> dict:
    """
    Extracts key product attributes (storage capacity, model codes)
    using regular expressions to enforce hard constraints during matching.
    """
    title_lower = title.lower()

    storage_match = re.search(r"\b(\d+\s?(gb|tb))\b", title_lower)
    model_match = re.search(r"\b([a-zA-Z0-9]{2,5}[-\s]?[a-zA-Z0-9]{3,6})\b", title)

    return {
        "storage": storage_match.group(1).replace(" ", "").upper() if storage_match else None,
        "model_code": model_match.group(1).upper() if model_match else None,
    }


def process_scraped_offer(scraped_item: dict) -> str | None:
    """
    Ingests one scraped {title, price, vendor, url, ...} payload: resolves
    it to a master product, upserts the vendor_offer, logs a price_history
    point, and (if the price changed) triggers the price-drop-alert task.

    Returns the matched product_id, or None on failure.
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
                WHERE 1 - (title_embedding <=> %s::vector) > 0.60
                ORDER BY title_embedding <=> %s::vector
                LIMIT 5;
                """,
                (embedding, embedding, embedding),
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

                token_score = fuzz.token_sort_ratio(raw_title, candidate["title"]) / 100.0
                combined_confidence = (candidate["similarity"] * 0.5) + (token_score * 0.5)

                if combined_confidence > best_confidence:
                    best_confidence = combined_confidence
                    matched_product_id = candidate["id"]

            # Step 4: routing decision
            if matched_product_id and best_confidence >= 0.82:
                status = "matched"
            elif matched_product_id and 0.60 <= best_confidence < 0.82:
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

            # Step 5: upsert the vendor offer
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
                    product_url, affiliate_url, price, in_stock, status, best_confidence,
                ),
            )
            offer_id = cursor.fetchone()["id"]

            # Step 6: append to price_history (append-only log, powers /history)
            cursor.execute(
                """
                INSERT INTO price_history (offer_id, price, in_stock, recorded_at)
                VALUES (%s, %s, %s, NOW());
                """,
                (offer_id, price, in_stock),
            )

            conn.commit()

            # Step 7: post-processing (cache invalidation + price-drop alert dispatch)
            if status == "matched":
                invalidate_grid_cache(str(matched_product_id))

                cursor.execute(
                    """
                    SELECT MIN(current_price) AS lowest_price
                    FROM vendor_offers
                    WHERE product_id = %s::uuid AND in_stock = TRUE AND match_status = 'matched';
                    """,
                    (matched_product_id,),
                )
                lowest_row = cursor.fetchone()
                if lowest_row and lowest_row["lowest_price"] is not None:
                    # Imported lazily to avoid a circular import (tasks -> matcher -> tasks)
                    from app.tasks import (
                        check_and_notify_price_drops,
                        dispatch_merchant_repricing_webhooks,
                    )
                    lowest_price = float(lowest_row["lowest_price"])
                    check_and_notify_price_drops.delay(str(matched_product_id), lowest_price)
                    dispatch_merchant_repricing_webhooks.delay(str(matched_product_id), lowest_price)

            return str(matched_product_id)

        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to process offer '{raw_title}': {e}")
            raise
