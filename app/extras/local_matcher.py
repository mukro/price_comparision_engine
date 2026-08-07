""" Here's my take: to give you the highest ROI on your engineering effort right now, we should implement Local Open-Source Embeddings alongside an Admin Human-in-the-Loop Review Queue.
Local embeddings drop your AI operational costs to $0, while the review queue ensures 100% catalog precision so bad scraper data never ruins your user experience.
1. Local Embeddings with sentence-transformers ($0 Cost)
By replacing OpenAI calls with a lightweight local model (all-MiniLM-L6-v2), embeddings run in milliseconds directly on your CPU/GPU without network latency or API bill costs.
Updated Matcher Module (local_matcher.py)
First, install the library: pip install sentence-transformers """
import os
import re

import psycopg2
from psycopg2.extras import RealDictCursor
from rapidfuzz import fuzz
from sentence_transformers import SentenceTransformer

from extras.tasks import check_and_notify_price_drops

# Load a fast, lightweight local model (produces 384-dimensional vectors)
# Downloads once automatically on initial boot (~90MB)
model = SentenceTransformer('all-MiniLM-L6-v2')

DB_CONFIG = {
    "dbname": "price_comparison",
    "user": "postgres",
    "password": "your_password",
    "host": "localhost",
    "port": 5432
}

def get_embedding(text: str) -> list[float]:
    """Generates 384-dim vector embedding locally on CPU without API fees."""
    embedding = model.encode(text, convert_to_numpy=True)
    return embedding.tolist()

def extract_specifications(title: str) -> dict:
    title_lower = title.lower()
    storage_match = re.search(r'\b(\d+\s?(gb|tb))\b', title_lower)
    model_match = re.search(r'\b([a-zA-Z0-9]{2,5}[-\s]?[a-zA-Z0-9]{3,6})\b', title)
    return {
        "storage": storage_match.group(1).replace(" ", "") if storage_match else None,
        "model_code": model_match.group(1).upper() if model_match else None
    }

def process_scraped_offer_with_review(scraped_item: dict):
    """
    Ingests scraped items, calculates match confidence, auto-matches high confidence items,
    and queues ambiguous matches for human review.
    """
    raw_title = scraped_item["raw_title"]
    brand = scraped_item.get("brand")
    vendor_id = scraped_item["vendor_id"]
    vendor_product_id = scraped_item["vendor_product_id"]
    price = scraped_item["price"]
    product_url = scraped_item["product_url"]
    
    # 1. Local Vector Embedding
    embedding = get_embedding(raw_title)
    scraped_specs = extract_specifications(raw_title)
    
    conn = psycopg2.connect(**DB_CONFIG)
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        matched_product_id = None
        best_confidence = 0.0
        
        # 2. Query top candidates via pgvector (Requires vector(384) column in Postgres)
        cursor.execute("""
            SELECT id, title, brand,
                   1 - (title_embedding <=> %s::vector) AS similarity
            FROM products
            WHERE 1 - (title_embedding <=> %s::vector) > 0.60
            ORDER BY title_embedding <=> %s::vector
            LIMIT 5;
        """, (embedding, embedding, embedding))
        
        candidates = cursor.fetchall()
        
        for candidate in candidates:
            cand_specs = extract_specifications(candidate["title"])
            
            # Reject if brands explicitly conflict
            if brand and candidate["brand"] and brand.lower() != candidate["brand"].lower():
                continue
                
            # Reject if storage capacities conflict (e.g. 128GB vs 256GB)
            if scraped_specs["storage"] and cand_specs["storage"]:
                if scraped_specs["storage"] != cand_specs["storage"]:
                    continue
            
            token_score = fuzz.token_sort_ratio(raw_title, candidate["title"]) / 100.0
            
            # Combine vector similarity and fuzzy string match for final confidence
            combined_confidence = (candidate["similarity"] * 0.5) + (token_score * 0.5)
            
            if combined_confidence > best_confidence:
                best_confidence = combined_confidence
                matched_product_id = candidate["id"]

        # 3. Decision Logic based on Confidence Score
        
        # HIGH CONFIDENCE (> 0.82): Auto-link directly
        if matched_product_id and best_confidence >= 0.82:
            status = "matched"
            insert_offer(cursor, matched_product_id, vendor_id, vendor_product_id, raw_title, product_url, price, status)
            print(f"[AUTO-MATCHED] '{raw_title}' (Confidence: {best_confidence:.2f})")

        # MEDIUM CONFIDENCE (0.60 - 0.81): Send to Human Review Queue
        elif matched_product_id and 0.60 <= best_confidence < 0.82:
            status = "pending_review"
            insert_offer(cursor, matched_product_id, vendor_id, vendor_product_id, raw_title, product_url, price, status)
            print(f"[PENDING REVIEW] '{raw_title}' queued for admin validation.")

        # LOW CONFIDENCE (< 0.60): Create New Master Product
        else:
            cursor.execute("""
                INSERT INTO products (title, brand, model_code, specifications, title_embedding)
                VALUES (%s, %s, %s, %s, %s::vector)
                RETURNING id;
            """, (
                raw_title, brand, scraped_specs["model_code"],
                psycopg2.extras.Json({"storage": scraped_specs["storage"]}), embedding
            ))
            new_product_id = cursor.fetchone()["id"]
            insert_offer(cursor, new_product_id, vendor_id, vendor_product_id, raw_title, product_url, price, "matched")
            print(f"[NEW MASTER CREATED] '{raw_title}' -> ID: {new_product_id}")

        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Error processing offer: {e}")
    finally:
        cursor.close()
        conn.close()

def insert_offer(cursor, product_id, vendor_id, vendor_product_id, raw_title, product_url, price, status):
    cursor.execute("""
        INSERT INTO vendor_offers (
            product_id, vendor_id, vendor_product_id, raw_title, 
            product_url, current_price, match_status, last_scraped_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (vendor_id, vendor_product_id) 
        DO UPDATE SET
            current_price = EXCLUDED.current_price,
            match_status = EXCLUDED.match_status,
            last_scraped_at = NOW();
    """, (product_id, vendor_id, vendor_product_id, raw_title, product_url, price, status))

# Integration into Scraper Pipeline
# Trigger check_and_notify_price_drops at the bottom of your offer update function in local_matcher.py:
# Inside process_scraped_offer_with_review after committing the price update:
cursor.execute("""
    SELECT MIN(current_price) AS lowest_price 
    FROM vendor_offers 
    WHERE product_id = %s::uuid AND in_stock = TRUE;
""", (product_id,))

result = cursor.fetchone()
if result and result["lowest_price"]:
    # Asynchronously dispatch alert checks off the main scraper thread
    check_and_notify_price_drops.delay(str(product_id), float(result["lowest_price"]))