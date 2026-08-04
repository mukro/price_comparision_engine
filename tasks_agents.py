"""
Agentic AI task definitions.

All tasks here are gated by the AGENTS_ENABLED setting.
When False, tasks return immediately with a 'skipped' status
and consume zero LLM credits / zero compute.
"""
from celery.utils.log import get_task_logger

from app.celery_app import celery_app
from app.config import settings

logger = get_task_logger(__name__)


def _agents_disabled_guard() -> bool:
    """Returns True if agents are disabled (task should abort)."""
    if not settings.AGENTS_ENABLED:
        logger.info("[AGENT] AGENTS_ENABLED=false — task skipped.")
        return True
    return False


# ==================================================================
# Pipeline 1: Entity Resolution (match scraped offers to products)
# ==================================================================

@celery_app.task(bind=True, max_retries=2, default_retry_delay=60)
def run_entity_resolution_pipeline(self):
    """
    Fetches pending_review offers, runs hybrid vector + text search,
    calls LLM for match/new-product/escalate decision, and writes back.
    """
    if _agents_disabled_guard():
        return {"status": "skipped", "reason": "agents_disabled"}

    logger.info("[AGENT] Starting entity resolution pipeline...")
    # TODO: Wire in real logic from Data Quality Agent when ready.
    # from app.agents.entity_resolution import EntityResolutionPipeline
    # pipeline = EntityResolutionPipeline()
    # return pipeline.run()
    return {"status": "success", "processed": 0, "note": "placeholder"}


# ==================================================================
# Pipeline 2: Selector Health (test vendor CSS selectors via Playwright)
# ==================================================================

@celery_app.task(bind=True, max_retries=2, default_retry_delay=120)
def run_selector_health_check(self):
    """
    Playwright-tests every active vendor CSS selectors.
    If a selector is broken, uses LLM to suggest a fix.
    Auto-applies if confidence > 0.6.
    """
    if _agents_disabled_guard():
        return {"status": "skipped", "reason": "agents_disabled"}

    logger.info("[AGENT] Starting selector health check...")
    # TODO: Wire in real logic from Data Quality Agent when ready.
    # from app.agents.selector_health import SelectorHealthChecker
    # checker = SelectorHealthChecker()
    # return checker.run()
    return {"status": "success", "checked": 0, "fixed": 0, "note": "placeholder"}


# ==================================================================
# Pipeline 3: Data Quality Audit (future expansion)
# ==================================================================

@celery_app.task
def run_data_quality_audit():
    """Placeholder for future data-quality audits (outliers, stale prices)."""
    if _agents_disabled_guard():
        return {"status": "skipped", "reason": "agents_disabled"}
    logger.info("[AGENT] Running data quality audit...")
    return {"status": "success", "issues_found": 0}
