"""State definitions for the Data Quality Agent workflow."""
from typing import Annotated, List, Optional, Dict, Any
from typing_extensions import TypedDict
from operator import add


class ScrapedOffer(TypedDict):
    """A raw scraped offer that needs entity resolution."""
    offer_id: str
    vendor_id: str
    raw_title: str
    current_price: float
    product_url: str
    vendor_name: str
    confidence_score: Optional[float]
    match_status: str  # pending_review | matched | new_product | rejected


class MatchedProduct(TypedDict):
    """An existing product in the database."""
    product_id: str
    title: str
    brand: Optional[str]
    model_code: Optional[str]
    title_embedding: Optional[List[float]]
    similarity_score: Optional[float]


class ResolutionDecision(TypedDict):
    """The agent's decision for a single offer."""
    offer_id: str
    action: str  # auto_match | suggest_match | new_product | needs_human
    target_product_id: Optional[str]
    confidence: float  # 0.0 to 1.0
    reasoning: str
    suggested_title: Optional[str]
    suggested_brand: Optional[str]


class SelectorHealth(TypedDict):
    """Health check result for a vendor's CSS selectors."""
    vendor_id: str
    vendor_name: str
    domain: str
    title_selector: str
    price_selector: str
    stock_selector: Optional[str]
    last_working_at: Optional[str]
    last_failure_at: Optional[str]
    failure_reason: Optional[str]
    suggested_fix: Optional[str]
    status: str  # healthy | degraded | broken


class AgentState(TypedDict):
    """The complete state object passed through the LangGraph."""
    # Input
    batch_size: int
    mode: str  # entity_resolution | selector_health | both

    # Entity Resolution Pipeline
    pending_offers: List[ScrapedOffer]
    candidate_products: Annotated[List[MatchedProduct], add]
    decisions: Annotated[List[ResolutionDecision], add]

    # Selector Health Pipeline
    vendor_health_checks: Annotated[List[SelectorHealth], add]

    # Meta
    processed_count: int
    escalated_count: int
    errors: Annotated[List[str], add]
    summary: Optional[str]
