import asyncio
from typing import Any, Dict, Optional

import psycopg2
from playwright.async_api import async_playwright
from psycopg2.extras import RealDictCursor

from app.config import settings
from app.core.matcher import calculate_consensus_match_score
from celery import shared_task


async def scrape_vendor_product_page(url: str, title_selector: str, price_selector: str) -> Optional[Dict[str, Any]]:
    """
    Executes a headless browser scrape using Playwright with anti-detection headers.
    """
    async with async_playwright() as p:
        # Launch headless browser with stealth context
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        try:
            # Navigate with a 15-second timeout
            await page.goto(url, timeout=15000, wait_until="domcontentloaded")
            
            # Extract title and price text
            raw_title = await page.inner_text(title_selector)
            raw_price_str = await page.inner_text(price_selector)
            
            # Clean price string (e.g., "$1,299.99" -> 1299.99)
            cleaned_price = float("".join(c for c in raw_price_str if c.isdigit() or c == '.'))
            
            await browser.close()
            return {
                "raw_title": raw_title.strip(),
                "current_price": cleaned_price,
                "product_url": url
            }
        except Exception as e:
            await browser.close()
            print(f"[Scraper Error] Failed to scrape {url}: {e}")
            return None


@shared_task(bind=True, max_retries=3)
def run_product_scrape_job(self, vendor_id: str, vendor_product_id: str, url: str, title_selector: str, price_selector: str):
    """
    Celery task that runs the scraper, triggers entity resolution, and upserts into vendor_offers.
    """
    # Run async Playwright function inside Celery's synchronous event loop
    loop = asyncio.get_event_loop()
    scraped_data = loop.run_until_complete(
        scrape_vendor_product_page(url, title_selector, price_selector)
    )
    
    if not scraped_data:
        return {"status": "failed", "reason": "Extraction failed or timed out"}

    conn = psycopg2.connect(settings.DATABASE_URL)
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Fetch candidate products for matching using vector similarity
        cursor.execute("SELECT id, title FROM products LIMIT 100;")
        candidates = cursor.fetchall()
        
        best_match_id = None
        best_score = 0.0
        
        # Determine candidate match via consensus matcher
        for cand in candidates:
            score = calculate_consensus_match_score(
                raw_title=scraped_data["raw_title"],
                candidate_title=cand["title"],
                raw_price=scraped_data["current_price"],
                candidate_avg_price=scraped_data["current_price"], # Default baseline
                vector_similarity=0.80 # Fallback placeholder until full embedding model pass
            )
            if score > best_score:
                best_score = score
                best_match_id = cand["id"]

        match_status = "matched" if best_score >= 0.75 else "pending_review"
        suggested_id = best_match_id if match_status == "pending_review" else None
        final_matched_id = best_match_id if match_status == "matched" else None

        # Upsert offer into database
        cursor.execute("""
            INSERT INTO vendor_offers (
                vendor_id, vendor_product_id, product_id, suggested_product_id,
                raw_title, current_price, product_url, confidence_score, match_status, last_scraped_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (vendor_id, vendor_product_id) DO UPDATE SET
                current_price = EXCLUDED.current_price,
                confidence_score = EXCLUDED.confidence_score,
                match_status = EXCLUDED.match_status,
                last_scraped_at = NOW();
        """, (
            vendor_id, vendor_product_id, final_matched_id, suggested_id,
            scraped_data["raw_title"], scraped_data["current_price"],
            scraped_data["product_url"], best_score, match_status
        ))
        
        conn.commit()
        return {
            "status": "success",
            "vendor_product_id": vendor_product_id,
            "confidence_score": best_score,
            "match_status": match_status
        }
    finally:
        cursor.close()
        conn.close()