# app/workers/scraper_worker.py
"""
Playwright-based page scraper with:
  - Browser pool (one browser per worker process, reusable contexts)
  - Configurable price & stock parsing
  - Compliance gatekeeper integration (Protego robots.txt)
  - Structured logging
  - Crawl-delay awareness
"""
import asyncio
import logging
import re
from typing import Any, Dict, Optional

from playwright.async_api import async_playwright, BrowserContext, Page

from app.config import settings
from app.core.compliance import can_scrape_domain, get_domain_from_url, log_scrape_attempt

logger = logging.getLogger("scraper_worker")

# Process-wide singletons
_playwright = None
_browser = None
_context: Optional[BrowserContext] = None


async def _ensure_browser() -> BrowserContext:
    """Lazily initialise one browser + context per Celery worker process."""
    global _playwright, _browser, _context
    if _context is None:
        _playwright = await async_playwright().start()
        launch_kwargs: Dict[str, Any] = {"headless": True}
        if settings.SCRAPER_PROXY_URL:
            launch_kwargs["proxy"] = {"server": settings.SCRAPER_PROXY_URL}
        _browser = await _playwright.chromium.launch(**launch_kwargs)
        _context = await _browser.new_context(
            user_agent=settings.SCRAPER_USER_AGENT,
            viewport={"width": 1280, "height": 800},
        )
        logger.info("Browser context initialised for scraping.")
    return _context


async def _close_browser() -> None:
    global _browser, _context, _playwright
    if _context:
        await _context.close()
        _context = None
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright:
        await _playwright.stop()
        _playwright = None


def parse_price(raw: str) -> Optional[float]:
    """
    Robust international price parser.
    Handles: $1,299.99 | ₹1,299 | 1.299,99 € | EUR 1.299,99
    """
    if not raw or not isinstance(raw, str):
        return None
    cleaned = re.sub(r"[^\d.,]", "", raw.strip().lower())
    if not cleaned:
        return None

    comma_idx = cleaned.rfind(",")
    dot_idx = cleaned.rfind(".")

    if comma_idx > dot_idx and (len(cleaned) - comma_idx - 1) == 2:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")

    try:
        val = float(cleaned)
        return val if val > 0 else None
    except ValueError:
        return None


def parse_stock_status(raw: str, present_text: str = "in stock") -> bool:
    """Returns True if the scraped stock text contains the positive indicator."""
    if not raw:
        return False
    return present_text.lower() in raw.lower()


async def scrape_vendor_product_page(
    url: str,
    title_selector: str,
    price_selector: str,
    stock_selector: Optional[str] = None,
    stock_text_present: Optional[str] = None,
    proxy_url: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Scrapes a single product page through the compliance gatekeeper.
    Returns structured payload or None on failure / blocked.
    Respects robots.txt crawl-delay by adding a sleep if needed.
    """
    domain = get_domain_from_url(url)
    allowed, reason, metadata = can_scrape_domain(url)
    log_scrape_attempt(domain, url, allowed, reason, metadata)

    if not allowed:
        logger.warning(f"Scrape blocked for {url}: {reason}")
        return None

    # Respect robots.txt crawl-delay
    if metadata.get("crawl_delay"):
        await asyncio.sleep(metadata["crawl_delay"])

    context = await _ensure_browser()
    page: Optional[Page] = None
    try:
        page = await context.new_page()
        await page.goto(url, timeout=settings.SCRAPER_TIMEOUT_MS, wait_until="domcontentloaded")

        raw_title = await page.inner_text(title_selector)
        raw_price_str = await page.inner_text(price_selector)

        cleaned_price = parse_price(raw_price_str)
        if cleaned_price is None:
            logger.warning(f"Could not parse price from '{raw_price_str}' at {url}")
            return None

        in_stock = True
        if stock_selector and stock_text_present:
            try:
                raw_stock = await page.inner_text(stock_selector)
                in_stock = parse_stock_status(raw_stock, stock_text_present)
            except Exception:
                in_stock = False

        return {
            "raw_title": raw_title.strip()[:300],
            "price": cleaned_price,
            "product_url": url,
            "in_stock": in_stock,
            "scraped_at": "now()",
        }
    except Exception as e:
        logger.warning(f"Scrape failed for {url}: {e}")
        return None
    finally:
        if page:
            await page.close()


def process_incoming_scraped_payload(payload: Dict[str, Any]) -> bool:
    """Validates and routes a scraped payload to the matcher."""
    from app.core.matcher import process_scraped_offer

    required = ["raw_title", "vendor_id", "vendor_product_id", "price", "product_url"]
    for field in required:
        if field not in payload or payload[field] is None:
            logger.error(f"Missing required field '{field}' in scraped payload.")
            return False

    try:
        price = parse_price(str(payload["price"]))
        if price is None or price <= 0:
            logger.warning(f"Invalid price for item {payload['vendor_product_id']}.")
            return False
        payload["price"] = price
        process_scraped_offer(payload)
        return True
    except Exception as e:
        logger.error(f"Failed to process scraped payload: {e}")
        return False


def batch_process_scraped_payloads(payloads: list[Dict[str, Any]]) -> dict:
    success = failure = 0
    for item in payloads:
        if process_incoming_scraped_payload(item):
            success += 1
        else:
            failure += 1
    return {"total": len(payloads), "success": success, "failed": failure}
