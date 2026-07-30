# app/core/compliance.py
"""
Scraping governance layer:
  - robots.txt checking (with caching)
  - per-domain rate limiting
  - domain allow-list enforcement
  - master kill-switch

All scraping tasks MUST call can_scrape_domain() before hitting a URL.
"""
import logging
from urllib.parse import urlparse
from typing import Optional

import requests
from redis import Redis

from app.config import settings
from app.db_sync import get_conn, redis_client

logger = logging.getLogger("compliance")

ROBOTS_CACHE_TTL_SECONDS = 3600  # 1 hour


def _cache_key(domain: str, suffix: str) -> str:
    return f"compliance:{domain}:{suffix}"


def get_domain_from_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc.lower().lstrip("www.")


def _fetch_robots_txt(domain: str) -> Optional[str]:
    """Fetches raw robots.txt; returns None on any failure."""
    try:
        resp = requests.get(
            f"https://{domain}/robots.txt",
            timeout=10,
            headers={"User-Agent": settings.SCRAPER_USER_AGENT},
        )
        if resp.status_code == 200:
            return resp.text
    except Exception as exc:
        logger.warning(f"robots.txt fetch failed for {domain}: {exc}")
    return None


def _is_allowed_by_robots(domain: str, path: str = "/") -> bool:
    """
    Naive robots.txt parser. Production-grade: switch to `robotparser` or `protego`.
    Returns True if allowed or if robots.txt is unreachable.
    """
    cache_key = _cache_key(domain, "robots")
    cached = redis_client.get(cache_key)
    if cached is not None:
        robots_text = cached
    else:
        robots_text = _fetch_robots_txt(domain)
        if robots_text is None:
            return True  # permissive default when unreachable
        redis_client.setex(cache_key, ROBOTS_CACHE_TTL_SECONDS, robots_text)

    # Very naive check: look for "Disallow: /" under our user-agent or *.
    # For production use Protego: https://github.com/scrapy/protego
    ua_blocks = robots_text.split("User-agent:")
    for block in ua_blocks:
        lines = block.strip().splitlines()
        if not lines:
            continue
        ua = lines[0].strip().lower()
        if ua in ("*", "pricecomparisonbot", "pricecomparisonbot/1.0"):
            for line in lines[1:]:
                if line.strip().lower().startswith("disallow:"):
                    dis_path = line.split(":", 1)[1].strip()
                    if path.startswith(dis_path) or dis_path == "/":
                        return False
    return True


def _check_rate_limit(domain: str, rpm: int) -> bool:
    """
    Token-bucket style rate limiter in Redis.
    Returns True if the scrape is permitted.
    """
    key = _cache_key(domain, "ratelimit")
    pipe = redis_client.pipeline()
    pipe.incr(key)
    pipe.expire(key, 60)
    results = pipe.execute()
    current_count = results[0]
    return current_count <= rpm


def can_scrape_domain(url: str) -> tuple[bool, str]:
    """
    Central gatekeeper. Returns (allowed: bool, reason: str).
    Every scraper task must call this before Playwright.
    """
    if not settings.SCRAPING_ENABLED:
        return False, "SCRAPING_ENABLED is False (global kill-switch)."

    domain = get_domain_from_url(url)

    # 1. Domain allowlist check
    if settings.ENFORCE_DOMAIN_ALLOWLIST:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM vendors WHERE domain = %s AND is_active = TRUE AND scraping_allowed = TRUE LIMIT 1;",
                (domain,),
            )
            if not cursor.fetchone():
                return False, f"Domain {domain} is not in the active allowlist."

    # 2. robots.txt check
    if settings.ENFORCE_ROBOTS_TXT:
        if not _is_allowed_by_robots(domain):
            return False, f"robots.txt disallows scraping on {domain}."

    # 3. Rate limit check
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT scrape_rpm FROM vendors WHERE domain = %s LIMIT 1;",
            (domain,),
        )
        row = cursor.fetchone()
        rpm = row[0] if row and row[0] else settings.DEFAULT_SCRAPE_RPM

    if not _check_rate_limit(domain, rpm):
        return False, f"Rate limit exceeded for {domain} ({rpm} RPM)."

    return True, "OK"


def log_scrape_attempt(domain: str, url: str, allowed: bool, reason: str) -> None:
    """Audit trail for compliance reviews."""
    logger.info(
        "scrape_attempt",
        extra={
            "domain": domain,
            "url": url,
            "allowed": allowed,
            "reason": reason,
        },
    )
