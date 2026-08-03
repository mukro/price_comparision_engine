"""Tools for detecting vendor website HTML structure changes."""
import asyncio
import logging
from typing import Optional, Dict, Any
from urllib.parse import urlparse

from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


async def test_selector_on_page(
    url: str,
    title_selector: str,
    price_selector: str,
    stock_selector: Optional[str] = None,
    timeout_ms: int = 15000
) -> Dict[str, Any]:
    """Test if CSS selectors still work on a vendor page. Returns health report."""
    result = {
        "url": url,
        "title_selector": title_selector,
        "price_selector": price_selector,
        "stock_selector": stock_selector,
        "title_found": False,
        "price_found": False,
        "stock_found": False,
        "title_text": None,
        "price_text": None,
        "stock_text": None,
        "html_sample": None,
        "error": None,
    }

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (compatible; PCE-HealthBot/1.0)",
                viewport={"width": 1280, "height": 800},
            )
            page = await context.new_page()

            response = await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")

            if response.status >= 400:
                result["error"] = f"HTTP {response.status}"
                await browser.close()
                return result

            # Test title selector
            try:
                title_el = await page.query_selector(title_selector)
                if title_el:
                    result["title_found"] = True
                    result["title_text"] = await title_el.inner_text()
            except Exception as e:
                result["error"] = f"Title selector failed: {e}"

            # Test price selector
            try:
                price_el = await page.query_selector(price_selector)
                if price_el:
                    result["price_found"] = True
                    result["price_text"] = await price_el.inner_text()
            except Exception as e:
                result["error"] = f"Price selector failed: {e}"

            # Test stock selector
            if stock_selector:
                try:
                    stock_el = await page.query_selector(stock_selector)
                    if stock_el:
                        result["stock_found"] = True
                        result["stock_text"] = await stock_el.inner_text()
                except Exception as e:
                    result["error"] = f"Stock selector failed: {e}"

            # Grab HTML sample for LLM analysis
            html = await page.content()
            soup = BeautifulSoup(html, "lxml")
            # Extract a sample of the body for LLM context
            body_text = soup.body.get_text(separator=" ", strip=True)[:3000] if soup.body else ""
            result["html_sample"] = body_text

            await browser.close()

    except Exception as e:
        result["error"] = f"Browser error: {str(e)}"

    return result


def suggest_selector_fixes(health_report: Dict[str, Any]) -> Dict[str, Any]:
    """Use heuristics to suggest new selectors when old ones fail."""
    suggestions = {
        "title_suggestions": [],
        "price_suggestions": [],
        "stock_suggestions": [],
        "analysis": "",
    }

    html_sample = health_report.get("html_sample", "")
    if not html_sample:
        suggestions["analysis"] = "Could not retrieve page content."
        return suggestions

    # Heuristic: look for common price patterns in the HTML text
    import re

    # Price patterns: ₹1,299.99, $199, Rs. 499, etc.
    price_patterns = [
        r'[₹$€£]\s*[\d,]+(?:\.\d{2})?',
        r'Rs\.?\s*[\d,]+',
        r'price[\s:=]*[₹$€£]?\s*[\d,]+',
    ]

    found_prices = []
    for pattern in price_patterns:
        matches = re.findall(pattern, html_sample, re.IGNORECASE)
        found_prices.extend(matches)

    if found_prices:
        suggestions["price_suggestions"] = [
            f"[class*=price]",  # Common class pattern
            f"span:has-text('{found_prices[0][:5]}')",  # Playwright text-based
        ]

    # Title patterns: look for h1 or product-title class
    if "h1" in html_sample.lower():
        suggestions["title_suggestions"].append("h1")
    suggestions["title_suggestions"].extend([
        "[class*=title]",
        "[class*=product-name]",
        "[data-testid*=title]",
    ])

    # Stock patterns
    stock_keywords = ["in stock", "out of stock", "available", "sold out"]
    for kw in stock_keywords:
        if kw in html_sample.lower():
            suggestions["stock_suggestions"].append(f"text={kw}")
            break

    suggestions["analysis"] = (
        f"Page loaded successfully. "
        f"Found {len(found_prices)} price-like strings. "
        f"Original selectors: title={health_report['title_found']}, "
        f"price={health_report['price_found']}, stock={health_report['stock_found']}."
    )

    return suggestions
