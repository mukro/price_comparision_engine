# app/workers/scraper_worker.py
"""
Playwright-based page scraper, plus payload validation for anything that
feeds the matcher (whether it came from this scraper or another ingestion
path, e.g. a manual admin upload or a vendor API integration).

This is the ONE real scraping implementation. Previously the codebase had
three: this one (tested, working, never scheduled), a hardcoded mock that
*was* scheduled, and a third unused variant -- all deleted/merged here.
"""
import logging
from typing import Any, Dict, Optional

from playwright.async_api import async_playwright

logger = logging.getLogger("scraper_worker")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; PriceComparisonBot/1.0; "
    "+https://example.com/bot)"
)


async def scrape_vendor_product_page(
    url: str,
    title_selector: str,
    price_selector: str,
    proxy_url: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Executes a headless-browser scrape of a single product page and
    returns the extracted title/price, or None on failure.

    Identifies itself honestly via User-Agent rather than impersonating a
    real browser. If a vendor requires stealth/rotating-proxy evasion to
    scrape at all, that's a signal to pursue their official API or an
    affiliate feed instead -- see the legal/compliance notes in the README.
    """
    launch_kwargs: Dict[str, Any] = {"headless": True}
    if proxy_url:
        launch_kwargs["proxy"] = {"server": proxy_url}

    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_kwargs)
        try:
            context = await browser.new_context(user_agent=DEFAULT_USER_AGENT)
            page = await context.new_page()

            await page.goto(url, timeout=15000, wait_until="domcontentloaded")

            raw_title = await page.inner_text(title_selector)
            raw_price_str = await page.inner_text(price_selector)

            cleaned_price = float("".join(c for c in raw_price_str if c.isdigit() or c == "."))

            return {
                "raw_title": raw_title.strip(),
                "price": cleaned_price,
                "product_url": url,
                "in_stock": True,
            }
        except Exception as e:
            logger.warning(f"Scrape failed for {url}: {e}")
            return None
        finally:
            await browser.close()


def process_incoming_scraped_payload(payload: Dict[str, Any]) -> bool:
    """
    Validates a scraped-offer payload and, if valid, hands it to the
    matcher for entity resolution. Used for ingestion paths other than the
    main Celery scrape task (e.g. a manual/admin bulk-upload endpoint).

    Expected payload:
    {
        "raw_title": "Sony WH-1000XM5 Wireless Noise Canceling Headphones",
        "brand": "Sony",
        "vendor_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "vendor_product_id": "SKU-991823",
        "price": 348.00,
        "product_url": "https://example.com/p/sku-991823",
        "affiliate_url": "https://affiliate.example.com/p/sku-991823?tag=myid"
    }
    """
    from app.core.matcher import process_scraped_offer  # local import: avoid loading the ML model unless needed

    required_fields = ["raw_title", "vendor_id", "vendor_product_id", "price", "product_url"]
    for field in required_fields:
        if field not in payload or payload[field] is None:
            logger.error(f"Missing required field '{field}' in scraped payload.")
            return False

    try:
        payload["price"] = float(payload["price"])
        if payload["price"] <= 0:
            logger.warning(f"Invalid price value {payload['price']} for item {payload['vendor_product_id']}.")
            return False

        logger.info(f"Ingesting offer '{payload['raw_title']}' from vendor ID {payload['vendor_id']}...")
        process_scraped_offer(payload)
        return True

    except Exception as e:
        logger.error(f"Failed to process scraped payload for '{payload.get('raw_title')}': {e}")
        return False


def batch_process_scraped_payloads(payloads: list[Dict[str, Any]]) -> dict:
    """Processes a batch of scraped offers and returns execution metrics."""
    success_count = 0
    failure_count = 0
    for item in payloads:
        if process_incoming_scraped_payload(item):
            success_count += 1
        else:
            failure_count += 1
    return {"total": len(payloads), "success": success_count, "failed": failure_count}
