# docker compose exec api pytest -v tests/test_scraper.py
from unittest.mock import AsyncMock, patch

import pytest

from app.tasks_scraper import scrape_vendor_product_page


@pytest.mark.asyncio
async def test_scrape_vendor_product_page_success(page_mock=None):
    """
    Tests successful title and price extraction with formatted price string.
    """
    mock_title = "   Sony WH-1000XM5 Wireless Headphones  "
    mock_price_str = "Sale Price: $398.00 USD"

    with patch("playwright.async_api.async_playwright") as mock_playwright:
        mock_browser = AsyncMock()
        mock_context = AsyncMock()
        mock_page = AsyncMock()

        mock_playwright.return_value.__aenter__.return_value.chromium.launch.return_value = mock_browser
        mock_browser.new_context.return_value = mock_context
        mock_context.new_page.return_value = mock_page

        mock_page.inner_text.side_effect = lambda selector: (
            mock_title if selector == ".product-title" else mock_price_str
        )

        result = await scrape_vendor_product_page(
            url="https://example-vendor.com/item/123",
            title_selector=".product-title",
            price_selector=".price-tag"
        )

        assert result is not None
        assert result["raw_title"] == "Sony WH-1000XM5 Wireless Headphones"
        assert result["current_price"] == 398.00
        assert result["product_url"] == "https://example-vendor.com/item/123"


@pytest.mark.asyncio
async def test_scrape_vendor_missing_selector():
    """
    Ensures gracefully returning None when a target selector is absent on the page.
    """
    with patch("playwright.async_api.async_playwright") as mock_playwright:
        mock_browser = AsyncMock()
        mock_context = AsyncMock()
        mock_page = AsyncMock()

        mock_playwright.return_value.__aenter__.return_value.chromium.launch.return_value = mock_browser
        mock_browser.new_context.return_value = mock_context
        mock_context.new_page.return_value = mock_page

        # Simulate Playwright throwing error when selector is not found
        mock_page.inner_text.side_effect = Exception("Element .missing-selector not found")

        result = await scrape_vendor_product_page(
            url="https://example-vendor.com/item/456",
            title_selector=".missing-selector",
            price_selector=".price-tag"
        )

        assert result is None


@pytest.mark.asyncio
async def test_scrape_vendor_page_timeout():
    """
    Ensures handling page navigation timeouts (e.g., slow responses or bot blocking).
    """
    with patch("playwright.async_api.async_playwright") as mock_playwright:
        mock_browser = AsyncMock()
        mock_context = AsyncMock()
        mock_page = AsyncMock()

        mock_playwright.return_value.__aenter__.return_value.chromium.launch.return_value = mock_browser
        mock_browser.new_context.return_value = mock_context
        mock_context.new_page.return_value = mock_page

        # Simulate timeout on page navigation
        mock_page.goto.side_effect = TimeoutError("Navigation timeout of 15000ms exceeded")

        result = await scrape_vendor_product_page(
            url="https://slow-vendor.com/item/789",
            title_selector=".title",
            price_selector=".price"
        )

        assert result is None