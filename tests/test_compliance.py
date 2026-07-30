# tests/test_compliance.py
"""
Unit tests for the scraping compliance / governance layer.
Run with: pytest tests/test_compliance.py -v
"""
import pytest
from unittest.mock import MagicMock, patch

from app.core.compliance import (
    can_scrape_domain,
    get_domain_from_url,
    parse_price,
    _is_allowed_by_robots,
    _check_rate_limit,
)


# ==========================================
# URL Parsing
# ==========================================

class TestGetDomainFromUrl:
    def test_standard_https(self):
        assert get_domain_from_url("https://www.amazon.com/dp/B08N5WRWNW") == "amazon.com"

    def test_www_stripped(self):
        assert get_domain_from_url("https://www.flipkart.com/product/123") == "flipkart.com"

    def test_no_www(self):
        assert get_domain_from_url("https://blinkit.com/prn/123") == "blinkit.com"

    def test_with_path_and_query(self):
        assert get_domain_from_url("https://zepto.com/product?id=456&ref=home") == "zepto.com"


# ==========================================
# Price Parsing
# ==========================================

class TestParsePrice:
    def test_us_dollar_format(self):
        assert parse_price("$1,299.99") == 1299.99

    def test_indian_rupee(self):
        assert parse_price("₹1,299") == 1299.0

    def test_european_decimal_comma(self):
        assert parse_price("1.299,99 €") == 1299.99

    def test_european_no_thousands(self):
        assert parse_price("1299,99") == 1299.99

    def test_plain_number(self):
        assert parse_price("499.00") == 499.0

    def test_out_of_stock_text(self):
        assert parse_price("Out of stock") is None

    def test_empty_string(self):
        assert parse_price("") is None

    def test_none_input(self):
        assert parse_price(None) is None

    def test_zero_price_rejected(self):
        assert parse_price("0.00") is None

    def test_negative_price_rejected(self):
        assert parse_price("-10.00") is None


# ==========================================
# robots.txt compliance (Protego)
# ==========================================

class TestRobotsCompliance:
    @patch("app.core.compliance.redis_client")
    @patch("app.core.compliance._fetch_robots_txt")
    def test_allowed_when_no_disallow(self, mock_fetch, mock_redis):
        mock_redis.get.return_value = None
        mock_fetch.return_value = "User-agent: *\nAllow: /"
        allowed, delay = _is_allowed_by_robots("example.com")
        assert allowed is True
        assert delay is None

    @patch("app.core.compliance.redis_client")
    @patch("app.core.compliance._fetch_robots_txt")
    def test_blocked_when_disallow_all(self, mock_fetch, mock_redis):
        mock_redis.get.return_value = None
        mock_fetch.return_value = "User-agent: *\nDisallow: /"
        allowed, delay = _is_allowed_by_robots("example.com")
        assert allowed is False
        assert delay is None

    @patch("app.core.compliance.redis_client")
    def test_uses_cache_when_available(self, mock_redis):
        mock_redis.get.return_value = "User-agent: *\nDisallow: /"
        allowed, delay = _is_allowed_by_robots("example.com")
        assert allowed is False
        assert delay is None

    @patch("app.core.compliance.redis_client")
    @patch("app.core.compliance._fetch_robots_txt")
    def test_permissive_when_robots_unreachable(self, mock_fetch, mock_redis):
        mock_redis.get.return_value = None
        mock_fetch.return_value = None
        allowed, delay = _is_allowed_by_robots("example.com")
        assert allowed is True
        assert delay is None

    @patch("app.core.compliance.redis_client")
    @patch("app.core.compliance._fetch_robots_txt")
    def test_crawl_delay_extracted(self, mock_fetch, mock_redis):
        mock_redis.get.return_value = None
        mock_fetch.return_value = (
            "User-agent: PriceComparisonBot\n"
            "Crawl-delay: 5\n"
            "Allow: /\n"
        )
        allowed, delay = _is_allowed_by_robots("example.com")
        assert allowed is True
        assert delay == 5.0


# ==========================================
# Rate Limiting
# ==========================================

class TestRateLimit:
    @patch("app.core.compliance.redis_client")
    def test_within_limit(self, mock_redis):
        mock_redis.pipeline.return_value = mock_pipe = MagicMock()
        mock_pipe.execute.return_value = [5]  # 5th request, limit is 6
        assert _check_rate_limit("example.com", 6) is True

    @patch("app.core.compliance.redis_client")
    def test_exceeds_limit(self, mock_redis):
        mock_redis.pipeline.return_value = mock_pipe = MagicMock()
        mock_pipe.execute.return_value = [7]  # 7th request, limit is 6
        assert _check_rate_limit("example.com", 6) is False


# ==========================================
# Integration: can_scrape_domain gatekeeper
# ==========================================

class TestCanScrapeDomain:
    @patch("app.core.compliance.settings")
    @patch("app.core.compliance._check_rate_limit")
    @patch("app.core.compliance._is_allowed_by_robots")
    @patch("app.core.compliance.get_conn")
    def test_all_clear(self, mock_get_conn, mock_robots, mock_ratelimit, mock_settings):
        mock_settings.SCRAPING_ENABLED = True
        mock_settings.ENFORCE_DOMAIN_ALLOWLIST = False
        mock_settings.ENFORCE_ROBOTS_TXT = True
        mock_settings.DEFAULT_SCRAPE_RPM = 6
        mock_robots.return_value = (True, None)
        mock_ratelimit.return_value = True

        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        allowed, reason, metadata = can_scrape_domain("https://example.com/product/123")
        assert allowed is True
        assert reason == "OK"
        assert metadata["domain"] == "example.com"
        assert metadata["robots_allowed"] is True

    @patch("app.core.compliance.settings")
    def test_global_kill_switch(self, mock_settings):
        mock_settings.SCRAPING_ENABLED = False
        allowed, reason, metadata = can_scrape_domain("https://example.com")
        assert allowed is False
        assert "kill-switch" in reason.lower()
        assert metadata["domain"] == "example.com"

    @patch("app.core.compliance.settings")
    @patch("app.core.compliance._is_allowed_by_robots")
    def test_robots_blocked(self, mock_robots, mock_settings):
        mock_settings.SCRAPING_ENABLED = True
        mock_settings.ENFORCE_ROBOTS_TXT = True
        mock_settings.ENFORCE_DOMAIN_ALLOWLIST = False
        mock_robots.return_value = (False, None)

        allowed, reason, metadata = can_scrape_domain("https://example.com")
        assert allowed is False
        assert "robots.txt" in reason.lower()
        assert metadata["robots_allowed"] is False

    @patch("app.core.compliance.settings")
    @patch("app.core.compliance._is_allowed_by_robots")
    @patch("app.core.compliance._check_rate_limit")
    @patch("app.core.compliance.get_conn")
    def test_crawl_delay_respected(self, mock_get_conn, mock_ratelimit, mock_robots, mock_settings):
        mock_settings.SCRAPING_ENABLED = True
        mock_settings.ENFORCE_ROBOTS_TXT = True
        mock_settings.ENFORCE_DOMAIN_ALLOWLIST = False
        mock_settings.DEFAULT_SCRAPE_RPM = 60  # 1 per second
        mock_robots.return_value = (True, 10.0)  # 10 second crawl-delay
        mock_ratelimit.return_value = True

        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        allowed, reason, metadata = can_scrape_domain("https://example.com")
        assert allowed is True
        assert metadata["crawl_delay"] == 10.0
        assert metadata["rate_limit_rpm"] == 6  # 60/10 = 6 RPM (stricter than default)
