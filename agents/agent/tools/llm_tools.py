"""LLM-powered reasoning tools for the Data Quality Agent."""
import os
import json
import logging
from typing import List, Dict, Any, Optional

from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage, HumanMessage

logger = logging.getLogger(__name__)

# Lazy initialization — models load on first use
_openai_llm = None
_anthropic_llm = None


def get_openai_llm():
    global _openai_llm
    if _openai_llm is None:
        _openai_llm = ChatOpenAI(
            model="gpt-4o-mini",  # Fast + cheap for structured extraction
            temperature=0.1,       # Low creativity — we want consistency
            api_key=os.environ.get("OPENAI_API_KEY"),
            max_retries=3,
        )
    return _openai_llm


def get_anthropic_llm():
    global _anthropic_llm
    if _anthropic_llm is None:
        _anthropic_llm = ChatAnthropic(
            model="claude-3-haiku-20240307",  # Fastest Claude for routing
            temperature=0.1,
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
            max_retries=3,
        )
    return _anthropic_llm


ENTITY_RESOLUTION_PROMPT = """You are the Data Quality Agent for a price comparison platform.

TASK: Decide how to handle a scraped vendor offer that needs entity resolution.

OFFER DETAILS:
- Vendor: {vendor_name}
- Raw Title: {raw_title}
- Price: {current_price}
- URL: {product_url}
- Confidence Score (from scraper): {confidence_score}

CANDIDATE MATCHES (from vector + text search):
{candidates_text}

DECISION RULES (in priority order):
1. If ANY candidate has similarity >= 0.92 AND brand matches AND model code matches → auto_match
2. If similarity >= 0.85 AND brand matches → suggest_match (escalate to human with this suggestion)
3. If similarity >= 0.75 but brand differs → suggest_match with note about cross-brand
4. If similarity < 0.75 OR no candidates → new_product
5. If title is garbled, nonsensical, or clearly wrong → needs_human

BRAND EXTRACTION RULE:
- The brand is usually the first word of the title (e.g., "Sony WH-1000XM5" → brand is "Sony")
- If the first word is a common word ("The", "New", "Best"), the brand is the second word

RESPONSE FORMAT (strict JSON):
{{
    "action": "auto_match|suggest_match|new_product|needs_human",
    "target_product_id": "uuid_or_null",
    "confidence": 0.0_to_1.0,
    "reasoning": "detailed explanation",
    "suggested_brand": "extracted brand",
    "suggested_title": "cleaned title if creating new product"
}}

Be conservative. When in doubt, escalate to human rather than guessing."""


SELECTOR_RECOVERY_PROMPT = """You are a web scraping expert. A vendor's CSS selectors have stopped working.

VENDOR: {vendor_name}
DOMAIN: {domain}
FAILED SELECTORS:
- Title: {title_selector} (found: {title_found})
- Price: {price_selector} (found: {price_found})
- Stock: {stock_selector} (found: {stock_found})

HTML TEXT SAMPLE (first 3000 chars):
{html_sample}

HEURISTIC SUGGESTIONS:
{heuristic_suggestions}

TASK: Suggest new CSS selectors that will work. Consider:
1. Common e-commerce patterns: [class*=price], [class*=title], h1, [data-testid]
2. Text-based selectors: text=\"Price:\" or has-text(\"₹\")
3. Structural selectors: div >> nth=0

RESPONSE FORMAT (strict JSON):
{{
    "title_selector": "suggested selector",
    "price_selector": "suggested selector", 
    "stock_selector": "suggested selector or null",
    "confidence": 0.0_to_1.0,
    "reasoning": "why these selectors should work"
}}

If you cannot confidently suggest selectors, return confidence < 0.5 and explain why."""


def resolve_entity(
    offer: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    use_anthropic: bool = False
) -> Dict[str, Any]:
    """Use LLM to decide how to resolve a scraped offer."""
    llm = get_anthropic_llm() if use_anthropic else get_openai_llm()

    # Format candidates for the prompt
    candidates_text = ""
    for i, c in enumerate(candidates[:5], 1):
        candidates_text += (
            f"\n{i}. ID: {c.get('product_id', 'N/A')}\n"
            f"   Title: {c.get('title', 'N/A')}\n"
            f"   Brand: {c.get('brand', 'N/A')}\n"
            f"   Model: {c.get('model_code', 'N/A')}\n"
            f"   Similarity: {c.get('spec_similarity', c.get('rrf_score', 0)):.3f}\n"
        )

    if not candidates_text:
        candidates_text = "No candidate products found in database."

    prompt = ENTITY_RESOLUTION_PROMPT.format(
        vendor_name=offer.get("vendor_name", "Unknown"),
        raw_title=offer.get("raw_title", ""),
        current_price=offer.get("current_price", 0),
        product_url=offer.get("product_url", ""),
        confidence_score=offer.get("confidence_score", "N/A"),
        candidates_text=candidates_text,
    )

    try:
        messages = [
            SystemMessage(content="You are a precise data quality agent. Always respond with valid JSON."),
            HumanMessage(content=prompt)
        ]
        response = llm.invoke(messages)

        # Extract JSON from response
        content = response.content.strip()
        # Handle markdown code blocks
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]

        decision = json.loads(content.strip())

        # Validate required fields
        if "action" not in decision:
            decision["action"] = "needs_human"
        if "confidence" not in decision:
            decision["confidence"] = 0.0
        if "reasoning" not in decision:
            decision["reasoning"] = "No reasoning provided by LLM."

        return decision

    except json.JSONDecodeError as e:
        logger.error(f"LLM returned invalid JSON: {e}. Raw: {response.content[:200]}")
        return {
            "action": "needs_human",
            "target_product_id": None,
            "confidence": 0.0,
            "reasoning": f"LLM parsing error: {e}",
            "suggested_brand": None,
            "suggested_title": None,
        }
    except Exception as e:
        logger.error(f"LLM invocation failed: {e}")
        return {
            "action": "needs_human",
            "target_product_id": None,
            "confidence": 0.0,
            "reasoning": f"LLM error: {e}",
            "suggested_brand": None,
            "suggested_title": None,
        }


def recover_selectors(
    vendor: Dict[str, Any],
    health_report: Dict[str, Any],
    heuristic_suggestions: Dict[str, Any],
    use_anthropic: bool = False
) -> Dict[str, Any]:
    """Use LLM to suggest new CSS selectors when old ones fail."""
    llm = get_anthropic_llm() if use_anthropic else get_openai_llm()

    prompt = SELECTOR_RECOVERY_PROMPT.format(
        vendor_name=vendor.get("name", "Unknown"),
        domain=vendor.get("domain", ""),
        title_selector=vendor.get("title_selector", ""),
        price_selector=vendor.get("price_selector", ""),
        stock_selector=vendor.get("stock_selector", "N/A"),
        title_found=health_report.get("title_found", False),
        price_found=health_report.get("price_found", False),
        stock_found=health_report.get("stock_found", False),
        html_sample=health_report.get("html_sample", "")[:3000],
        heuristic_suggestions=json.dumps(heuristic_suggestions, indent=2),
    )

    try:
        messages = [
            SystemMessage(content="You are a web scraping expert. Always respond with valid JSON."),
            HumanMessage(content=prompt)
        ]
        response = llm.invoke(messages)

        content = response.content.strip()
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]

        result = json.loads(content.strip())

        if "confidence" not in result:
            result["confidence"] = 0.5
        if "reasoning" not in result:
            result["reasoning"] = "No reasoning provided."

        return result

    except Exception as e:
        logger.error(f"Selector recovery LLM failed: {e}")
        return {
            "title_selector": None,
            "price_selector": None,
            "stock_selector": None,
            "confidence": 0.0,
            "reasoning": f"LLM error: {e}",
        }
