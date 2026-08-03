"""LangGraph workflow for the PCE Data Quality Agent.

The graph has two main pipelines:
1. Entity Resolution Pipeline: pending offers -> match decisions -> apply
2. Selector Health Pipeline: vendor check -> test -> fix or escalate
"""
import logging
from typing import Dict, Any, List

from langgraph.graph import StateGraph, END

from agent.state import AgentState, ScrapedOffer, MatchedProduct, ResolutionDecision, SelectorHealth
from agent.tools.db_tools import (
    fetch_pending_offers,
    find_similar_products,
    apply_auto_match,
    create_new_product_from_offer,
    escalate_to_human,
    fetch_active_vendors,
    update_vendor_selector,
    log_selector_health,
)
from agent.tools.llm_tools import resolve_entity, recover_selectors
from agent.tools.scraper_tools import test_selector_on_page, suggest_selector_fixes

logger = logging.getLogger(__name__)


# ==================== ENTITY RESOLUTION NODES ====================

def load_pending_offers(state: AgentState) -> Dict[str, Any]:
    """Node 1: Load offers stuck in pending_review."""
    offers = fetch_pending_offers(limit=state.get("batch_size", 50))
    logger.info(f"Loaded {len(offers)} pending offers")
    return {
        "pending_offers": offers,
        "processed_count": 0,
        "escalated_count": 0,
    }


def find_candidates(state: AgentState) -> Dict[str, Any]:
    """Node 2: For each offer, find similar products via hybrid search."""
    offers = state.get("pending_offers", [])
    candidates: List[MatchedProduct] = []

    for offer in offers:
        # Get embedding for the raw title
        # In production, you'd call your embedding service. Here we use a simple approach.
        # For now, pass empty vector and let the DB fallback to text search
        query_vector = [0.0] * 384  # Placeholder — in prod, call embedding API

        similar = find_similar_products(
            query_text=offer.get("raw_title", ""),
            query_vector=query_vector,
            limit=5
        )

        for s in similar:
            candidates.append(MatchedProduct(
                product_id=s.get("product_id", ""),
                title=s.get("title", ""),
                brand=s.get("brand"),
                model_code=s.get("model_code"),
                title_embedding=None,
                similarity_score=s.get("spec_similarity", s.get("rrf_score", 0)),
            ))

    return {"candidate_products": candidates}


def make_decisions(state: AgentState) -> Dict[str, Any]:
    """Node 3: Use LLM to decide match action for each offer."""
    offers = state.get("pending_offers", [])
    candidates = state.get("candidate_products", [])
    decisions: List[ResolutionDecision] = []

    # Group candidates by offer (simplified — in production, track offer_id in candidates)
    for offer in offers:
        # Filter candidates that might belong to this offer
        # In a real implementation, candidates would include offer_id
        offer_candidates = candidates[:5]  # Simplified

        decision = resolve_entity(offer, offer_candidates, use_anthropic=False)

        decisions.append(ResolutionDecision(
            offer_id=offer.get("offer_id", ""),
            action=decision.get("action", "needs_human"),
            target_product_id=decision.get("target_product_id"),
            confidence=decision.get("confidence", 0.0),
            reasoning=decision.get("reasoning", ""),
            suggested_title=decision.get("suggested_title"),
            suggested_brand=decision.get("suggested_brand"),
        ))

    logger.info(f"Made {len(decisions)} resolution decisions")
    return {"decisions": decisions}


def apply_decisions(state: AgentState) -> Dict[str, Any]:
    """Node 4: Execute the decisions (auto-match, create, or escalate)."""
    decisions = state.get("decisions", [])
    processed = 0
    escalated = 0
    errors = []

    for decision in decisions:
        action = decision.get("action")
        offer_id = decision.get("offer_id")

        try:
            if action == "auto_match" and decision.get("target_product_id"):
                success = apply_auto_match(
                    offer_id=offer_id,
                    product_id=decision["target_product_id"],
                    agent_reasoning=decision.get("reasoning", ""),
                )
                if success:
                    processed += 1
                else:
                    errors.append(f"Auto-match failed for {offer_id}")

            elif action == "new_product":
                new_id = create_new_product_from_offer(
                    offer_id=offer_id,
                    raw_title=decision.get("suggested_title", "Unknown Product"),
                    agent_reasoning=decision.get("reasoning", ""),
                )
                if new_id:
                    processed += 1
                else:
                    errors.append(f"New product creation failed for {offer_id}")

            elif action in ("suggest_match", "needs_human"):
                # Escalate to human with suggestions
                # Find candidates for this offer
                candidates = []  # In production, filter by offer_id
                success = escalate_to_human(
                    offer_id=offer_id,
                    suggested_matches=candidates,
                    agent_reasoning=decision.get("reasoning", ""),
                )
                if success:
                    escalated += 1
                else:
                    errors.append(f"Escalation failed for {offer_id}")

        except Exception as e:
            errors.append(f"Decision execution error for {offer_id}: {e}")

    summary = (
        f"Entity Resolution Complete: {processed} auto-resolved, "
        f"{escalated} escalated to human, {len(errors)} errors."
    )
    logger.info(summary)

    return {
        "processed_count": processed,
        "escalated_count": escalated,
        "errors": errors,
        "summary": summary,
    }


# ==================== SELECTOR HEALTH NODES ====================

def check_vendor_selectors(state: AgentState) -> Dict[str, Any]:
    """Node: Test CSS selectors for all active vendors."""
    import asyncio

    vendors = fetch_active_vendors()
    health_checks: List[SelectorHealth] = []

    # Test a sample URL for each vendor
    for vendor in vendors:
        # In production, you'd have a sample product URL per vendor
        # For now, construct a test URL from the domain
        domain = vendor.get("domain", "")
        if not domain.startswith("http"):
            domain = f"https://{domain}"

        test_url = f"{domain}/products/sample"  # Placeholder

        try:
            health_report = asyncio.run(test_selector_on_page(
                url=test_url,
                title_selector=vendor.get("title_selector", ""),
                price_selector=vendor.get("price_selector", ""),
                stock_selector=vendor.get("stock_selector"),
            ))

            status = "healthy"
            failure_reason = None
            suggested_fix = None

            if not health_report.get("title_found") or not health_report.get("price_found"):
                status = "broken"
                failure_reason = health_report.get("error", "Selectors not matching")

                # Try to suggest fixes
                heuristics = suggest_selector_fixes(health_report)

                # Use LLM for better suggestions
                llm_fix = recover_selectors(vendor, health_report, heuristics)

                if llm_fix.get("confidence", 0) > 0.6:
                    # Auto-apply the fix
                    if llm_fix.get("title_selector"):
                        update_vendor_selector(
                            vendor_id=vendor["vendor_id"],
                            selector_type="title",
                            new_selector=llm_fix["title_selector"],
                            agent_reasoning=llm_fix.get("reasoning", ""),
                        )
                    if llm_fix.get("price_selector"):
                        update_vendor_selector(
                            vendor_id=vendor["vendor_id"],
                            selector_type="price",
                            new_selector=llm_fix["price_selector"],
                            agent_reasoning=llm_fix.get("reasoning", ""),
                        )
                    suggested_fix = f"Auto-applied: title={llm_fix.get('title_selector')}, price={llm_fix.get('price_selector')}"
                    status = "degraded"  # Fixed but needs verification
                else:
                    suggested_fix = f"LLM suggestion (low confidence): {llm_fix.get('reasoning', '')}"

            log_selector_health(
                vendor_id=vendor["vendor_id"],
                status=status,
                failure_reason=failure_reason,
                suggested_fix=suggested_fix,
            )

            health_checks.append(SelectorHealth(
                vendor_id=vendor["vendor_id"],
                vendor_name=vendor.get("name", ""),
                domain=domain,
                title_selector=vendor.get("title_selector", ""),
                price_selector=vendor.get("price_selector", ""),
                stock_selector=vendor.get("stock_selector"),
                status=status,
                failure_reason=failure_reason,
                suggested_fix=suggested_fix,
            ))

        except Exception as e:
            logger.error(f"Health check failed for {vendor.get('name')}: {e}")
            log_selector_health(
                vendor_id=vendor["vendor_id"],
                status="broken",
                failure_reason=str(e),
                suggested_fix=None,
            )

    summary = (
        f"Selector Health Check: {sum(1 for h in health_checks if h['status'] == 'healthy')} healthy, "
        f"{sum(1 for h in health_checks if h['status'] == 'degraded')} degraded, "
        f"{sum(1 for h in health_checks if h['status'] == 'broken')} broken."
    )

    return {
        "vendor_health_checks": health_checks,
        "summary": summary,
    }


# ==================== GRAPH ASSEMBLY ====================

def build_graph() -> StateGraph:
    """Build and compile the LangGraph workflow."""
    workflow = StateGraph(AgentState)

    # Add nodes
    workflow.add_node("load_offers", load_pending_offers)
    workflow.add_node("find_candidates", find_candidates)
    workflow.add_node("make_decisions", make_decisions)
    workflow.add_node("apply_decisions", apply_decisions)
    workflow.add_node("check_selectors", check_vendor_selectors)

    # Define edges
    workflow.set_entry_point("load_offers")
    workflow.add_edge("load_offers", "find_candidates")
    workflow.add_edge("find_candidates", "make_decisions")
    workflow.add_edge("make_decisions", "apply_decisions")

    # Conditional: if mode includes selector health, run that too
    def route_after_entity_resolution(state: AgentState) -> str:
        mode = state.get("mode", "both")
        if mode in ("selector_health", "both"):
            return "check_selectors"
        return END

    workflow.add_conditional_edges(
        "apply_decisions",
        route_after_entity_resolution,
        {
            "check_selectors": "check_selectors",
            END: END,
        }
    )

    workflow.add_edge("check_selectors", END)

    return workflow.compile()


# Global compiled graph (singleton)
_agent_graph = None

def get_graph():
    global _agent_graph
    if _agent_graph is None:
        _agent_graph = build_graph()
    return _agent_graph
