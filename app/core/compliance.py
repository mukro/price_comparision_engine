# app/core/compliance.py
"""
Scraping governance layer with Protego (production-grade robots.txt parsing):
  - robots.txt checking (with Redis caching)
  - per-domain rate limiting (token-bucket via Redis)
  - domain allow-list enforcement
  - master kill-switch
  - crawl-delay awareness

All scraping tasks MUST call can_scrape_domain() before hitting a URL.
"""
import logging
from urllib.parse import urlparse
from typing import Optional

import requests
from protego import Protego

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


def _is_allowed_by_robots(domain: str, path: str = "/") -> tuple[bool, Optional[float]]:
    """
    Production-grade robots.txt parser using Protego.

    Returns:
        (allowed: bool, crawl_delay: Optional[float])
        crawl_delay is in seconds if specified in robots.txt, else None.
    """
    cache_key = _cache_key(domain, "robots")
    cached = redis_client.get(cache_key)

    if cached is not None:
        robots_text = cached
    else:
        robots_text = _fetch_robots_txt(domain)
        if robots_text is None:
            return True, None  # permissive default when unreachable
        redis_client.setex(cache_key, ROBOTS_CACHE_TTL_SECONDS, robots_text)

    try:
        rp = Protego.parse(robots_text)
        allowed = rp.can_fetch(settings.SCRAPER_USER_AGENT, f"https://{domain}{path}")

        # Extract crawl-delay if specified for our user-agent or wildcard
        crawl_delay = None
        for rule in rp._rules:
            if rule.applies_to(settings.SCRAPER_USER_AGENT):
                crawl_delay = rule.crawl_delay
                break
        if crawl_delay is None:
            # Try wildcard
            for rule in rp._rules:
                if rule.applies_to("*"):
                    crawl_delay = rule.crawl_delay
                    break

        return allowed, crawl_delay
    except Exception as e:
        logger.warning(f"Protego parse failed for {domain}: {e}")
        return True, None  # permissive fallback


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


def _get_crawl_delay(domain: str) -> Optional[float]:
    """Returns crawl-delay in seconds from robots.txt if present."""
    _, delay = _is_allowed_by_robots(domain)
    return delay


def can_scrape_domain(url: str) -> tuple[bool, str, dict]:
    """
    Central gatekeeper. Returns (allowed: bool, reason: str, metadata: dict).
    Every scraper task must call this before Playwright.

    metadata contains:
        - robots_allowed: bool
        - crawl_delay: Optional[float] (seconds)
        - rate_limit_rpm: int
        - domain: str
    """
    metadata = {
        "robots_allowed": True,
        "crawl_delay": None,
        "rate_limit_rpm": settings.DEFAULT_SCRAPE_RPM,
        "domain": "",
    }

    if not settings.SCRAPING_ENABLED:
        return False, "SCRAPING_ENABLED is False (global kill-switch).", metadata

    domain = get_domain_from_url(url)
    metadata["domain"] = domain

    # 1. Domain allowlist check
    if settings.ENFORCE_DOMAIN_ALLOWLIST:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM vendors WHERE domain = %s AND is_active = TRUE AND scraping_allowed = TRUE LIMIT 1;",
                (domain,),
            )
            if not cursor.fetchone():
                return False, f"Domain {domain} is not in the active allowlist.", metadata

    # 2. robots.txt check with Protego
    if settings.ENFORCE_ROBOTS_TXT:
        allowed, crawl_delay = _is_allowed_by_robots(domain)
        metadata["robots_allowed"] = allowed
        metadata["crawl_delay"] = crawl_delay

        if not allowed:
            return False, f"robots.txt disallows scraping on {domain}.", metadata

        # Respect crawl-delay if specified and stricter than our rate limit
        if crawl_delay and crawl_delay > 0:
            # Convert crawl-delay (seconds) to RPM for comparison
            robots_rpm = int(60 / crawl_delay)
            if robots_rpm < settings.DEFAULT_SCRAPE_RPM:
                metadata["rate_limit_rpm"] = robots_rpm
                logger.info(f"Respecting robots.txt crawl-delay: {crawl_delay}s ({robots_rpm} RPM) for {domain}")

    # 3. Rate limit check (use the stricter of config or robots.txt)
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT scrape_rpm FROM vendors WHERE domain = %s LIMIT 1;",
            (domain,),
        )
        row = cursor.fetchone()
        vendor_rpm = row[0] if row and row[0] else settings.DEFAULT_SCRAPE_RPM

    # Use the most restrictive rate limit
    effective_rpm = min(vendor_rpm, metadata["rate_limit_rpm"])
    metadata["rate_limit_rpm"] = effective_rpm

    if not _check_rate_limit(domain, effective_rpm):
        return False, f"Rate limit exceeded for {domain} ({effective_rpm} RPM).", metadata

    return True, "OK", metadata


def log_scrape_attempt(domain: str, url: str, allowed: bool, reason: str, metadata: dict = None) -> None:
    """Audit trail for compliance reviews."""
    meta = metadata or {}
    logger.info(
        "scrape_attempt",
        extra={
            "domain": domain,
            "url": url,
            "allowed": allowed,
            "reason": reason,
            "robots_allowed": meta.get("robots_allowed"),
            "crawl_delay": meta.get("crawl_delay"),
            "rate_limit_rpm": meta.get("rate_limit_rpm"),
        },
    )


def get_domain_compliance_report(domain: str) -> dict:
    """
    Returns a full compliance report for a domain.
    Useful for admin diagnostics.
    """
    allowed, crawl_delay = _is_allowed_by_robots(domain)

    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT name, domain, is_active, scraping_allowed, scrape_rpm,
                   respects_robots_txt, title_selector, price_selector
            FROM vendors WHERE domain = %s LIMIT 1;
            """,
            (domain,),
        )
        vendor = cursor.fetchone()

    return {
        "domain": domain,
        "robots_txt_allowed": allowed,
        "robots_txt_crawl_delay": crawl_delay,
        "vendor_config": dict(vendor) if vendor else None,
        "effective_rate_limit": min(
            vendor["scrape_rpm"] if vendor and vendor["scrape_rpm"] else settings.DEFAULT_SCRAPE_RPM,
            int(60 / crawl_delay) if crawl_delay else settings.DEFAULT_SCRAPE_RPM,
        ),
    }
