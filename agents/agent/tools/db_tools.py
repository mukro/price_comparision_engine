"""Database tools for the Data Quality Agent."""
import os
import logging
from typing import List, Optional, Dict, Any
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

DB_URL = os.environ.get(
    "DATABASE_URL",
    f"postgresql://{os.environ.get('POSTGRES_USER', 'price_engine')}:{os.environ.get('POSTGRES_PASSWORD', 'password')}@{os.environ.get('POSTGRES_HOST', 'db')}:{os.environ.get('POSTGRES_PORT', '5432')}/{os.environ.get('POSTGRES_DB', 'price_comparison')}"
)


@contextmanager
def get_conn():
    conn = psycopg2.connect(DB_URL)
    try:
        yield conn
    finally:
        conn.close()


def fetch_pending_offers(limit: int = 50) -> List[Dict[str, Any]]:
    """Fetch offers stuck in pending_review that the agent should process."""
    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT 
                vo.id::text AS offer_id,
                vo.vendor_id::text AS vendor_id,
                vo.raw_title,
                vo.current_price,
                vo.product_url,
                v.name AS vendor_name,
                vo.confidence_score,
                vo.match_status
            FROM vendor_offers vo
            JOIN vendors v ON vo.vendor_id = v.id
            WHERE vo.match_status = 'pending_review'
            ORDER BY vo.confidence_score DESC NULLS LAST, vo.last_scraped_at DESC
            LIMIT %s;
        """, (limit,))
        return [dict(row) for row in cursor.fetchall()]


def find_similar_products(query_text: str, query_vector: List[float], limit: int = 5) -> List[Dict[str, Any]]:
    """Hybrid search: pgvector cosine similarity + full-text ts_rank."""
    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
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
                p.id::text AS product_id,
                p.title,
                p.brand,
                p.model_code,
                1 - (p.title_embedding <=> %s::vector) AS spec_similarity,
                COALESCE(1.0 / (60 + vr.rank), 0.0) + COALESCE(1.0 / (60 + fr.rank), 0.0) AS rrf_score
            FROM products p
            LEFT JOIN vector_ranks vr ON p.id = vr.id
            LEFT JOIN fts_ranks fr ON p.id = fr.id
            WHERE vr.rank IS NOT NULL OR fr.rank IS NOT NULL
            ORDER BY rrf_score DESC
            LIMIT %s;
        """, (query_vector, query_text, query_text, query_vector, limit))
        return [dict(row) for row in cursor.fetchall()]


def apply_auto_match(offer_id: str, product_id: str, agent_reasoning: str) -> bool:
    """Auto-approve a pending match. Returns True on success."""
    with get_conn() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                UPDATE vendor_offers 
                SET product_id = %s::uuid, 
                    match_status = 'matched',
                    agent_matched = TRUE,
                    agent_matched_at = NOW(),
                    agent_reasoning = %s,
                    updated_at = NOW()
                WHERE id = %s::uuid;
            """, (product_id, agent_reasoning, offer_id))
            conn.commit()
            logger.info(f"Auto-matched offer {offer_id} -> product {product_id}")
            return cursor.rowcount > 0
        except Exception as e:
            conn.rollback()
            logger.error(f"Auto-match failed for {offer_id}: {e}")
            return False


def create_new_product_from_offer(offer_id: str, raw_title: str, agent_reasoning: str) -> Optional[str]:
    """Create a new product from an unmatched offer. Returns new product_id."""
    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        try:
            # Extract brand from title (simple heuristic)
            brand = raw_title.split()[0] if raw_title else None

            cursor.execute("""
                INSERT INTO products (title, brand, created_at, updated_at)
                VALUES (%s, %s, NOW(), NOW())
                RETURNING id::text;
            """, (raw_title[:300], brand))
            new_product = cursor.fetchone()
            new_product_id = new_product["id"]

            # Link the offer
            cursor.execute("""
                UPDATE vendor_offers 
                SET product_id = %s::uuid, 
                    match_status = 'matched',
                    agent_matched = TRUE,
                    agent_matched_at = NOW(),
                    agent_reasoning = %s,
                    updated_at = NOW()
                WHERE id = %s::uuid;
            """, (new_product_id, agent_reasoning, offer_id))

            conn.commit()
            logger.info(f"Created new product {new_product_id} from offer {offer_id}")
            return new_product_id
        except Exception as e:
            conn.rollback()
            logger.error(f"New product creation failed for {offer_id}: {e}")
            return None


def escalate_to_human(offer_id: str, suggested_matches: List[Dict], agent_reasoning: str) -> bool:
    """Move offer to human review queue with agent suggestions."""
    with get_conn() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                UPDATE vendor_offers 
                SET match_status = 'agent_reviewed',
                    agent_suggestions = %s,
                    agent_reasoning = %s,
                    agent_reviewed_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s::uuid;
            """, (
                str([{"product_id": m["product_id"], "title": m["title"], "similarity": m.get("spec_similarity", 0)} for m in suggested_matches]),
                agent_reasoning,
                offer_id
            ))
            conn.commit()
            logger.info(f"Escalated offer {offer_id} to human review")
            return True
        except Exception as e:
            conn.rollback()
            logger.error(f"Escalation failed for {offer_id}: {e}")
            return False


def fetch_active_vendors() -> List[Dict[str, Any]]:
    """Fetch all active vendors with their CSS selectors."""
    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT id::text AS vendor_id, name, domain, 
                   title_selector, price_selector, stock_selector,
                   is_active, scraping_allowed
            FROM vendors
            WHERE is_active = TRUE AND scraping_allowed = TRUE;
        """)
        return [dict(row) for row in cursor.fetchall()]


def update_vendor_selector(vendor_id: str, selector_type: str, new_selector: str, agent_reasoning: str) -> bool:
    """Update a vendor's CSS selector after detecting a change."""
    with get_conn() as conn:
        cursor = conn.cursor()
        try:
            column = f"{selector_type}_selector"
            cursor.execute(f"""
                UPDATE vendors 
                SET {column} = %s,
                    selector_last_updated_by_agent = NOW(),
                    selector_update_reason = %s,
                    updated_at = NOW()
                WHERE id = %s::uuid;
            """, (new_selector, agent_reasoning, vendor_id))
            conn.commit()
            logger.info(f"Updated {selector_type} selector for vendor {vendor_id}")
            return True
        except Exception as e:
            conn.rollback()
            logger.error(f"Selector update failed for vendor {vendor_id}: {e}")
            return False


def log_selector_health(vendor_id: str, status: str, failure_reason: Optional[str], suggested_fix: Optional[str]) -> None:
    """Log a selector health check result."""
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO agent_selector_health_logs 
                (vendor_id, status, failure_reason, suggested_fix, checked_at)
            VALUES (%s::uuid, %s, %s, %s, NOW());
        """, (vendor_id, status, failure_reason, suggested_fix))
        conn.commit()
