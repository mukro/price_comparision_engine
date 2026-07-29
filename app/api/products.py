# app/api/products.py
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel

from app import cache
from app.core.insights import (
    calculate_buy_timing_recommendation,
    find_feature_equivalent_alternatives,
)
from app.db import get_db_pool

router = APIRouter(prefix="/api/v1/products", tags=["Products"])


# ------------------------------------------------------------------
# Schemas
# ------------------------------------------------------------------

class VendorOfferSchema(BaseModel):
    offer_id: str
    vendor_name: str
    vendor_domain: str
    raw_title: str
    current_price: float
    currency: str
    in_stock: bool
    buy_url: str
    last_scraped_at: datetime


class ProductSummarySchema(BaseModel):
    id: str
    title: str
    brand: Optional[str]
    model_code: Optional[str]
    image_url: Optional[str]
    lowest_price: Optional[float]
    highest_price: Optional[float]
    offer_count: int


class PriceGridResponse(BaseModel):
    product_id: str
    title: str
    brand: Optional[str]
    model_code: Optional[str]
    image_url: Optional[str]
    specifications: dict
    offers: List[VendorOfferSchema]


class PricePointSchema(BaseModel):
    price: float
    in_stock: bool
    recorded_at: datetime


class PriceHistoryResponse(BaseModel):
    product_id: str
    currency: str
    history: List[PricePointSchema]


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------

@router.get("/search", response_model=List[ProductSummarySchema])
async def search_products(
    q: str = Query(..., min_length=2, description="Search query string"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    cache_key = f"search:{q.lower()}:{limit}:{offset}"
    cached_result = await cache.get_cached_json(cache_key)
    if cached_result:
        return cached_result

    pool = get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                p.id::text, p.title, p.brand, p.model_code, p.image_url,
                MIN(vo.current_price) AS lowest_price,
                MAX(vo.current_price) AS highest_price,
                COUNT(vo.id)::int AS offer_count,
                similarity(p.title, $1) AS score
            FROM products p
            LEFT JOIN vendor_offers vo ON p.id = vo.product_id AND vo.in_stock = TRUE
            WHERE p.title % $1 OR p.brand ILIKE $1 OR p.model_code ILIKE $1
            GROUP BY p.id
            ORDER BY score DESC, lowest_price ASC NULLS LAST
            LIMIT $2 OFFSET $3;
            """,
            q, limit, offset,
        )

    results = [
        {
            "id": r["id"],
            "title": r["title"],
            "brand": r["brand"],
            "model_code": r["model_code"],
            "image_url": r["image_url"],
            "lowest_price": float(r["lowest_price"]) if r["lowest_price"] is not None else None,
            "highest_price": float(r["highest_price"]) if r["highest_price"] is not None else None,
            "offer_count": r["offer_count"],
        }
        for r in rows
    ]
    await cache.set_cached_json(cache_key, results, ttl_seconds=600)
    return results


@router.get("/{product_id}/grid", response_model=PriceGridResponse)
async def get_price_grid(product_id: str = Path(..., description="UUID of master product")):
    cache_key = f"grid:{product_id}"
    cached_result = await cache.get_cached_json(cache_key)
    if cached_result:
        return cached_result

    pool = get_db_pool()
    async with pool.acquire() as conn:
        product = await conn.fetchrow(
            "SELECT id::text, title, brand, model_code, image_url, specifications FROM products WHERE id = $1::uuid;",
            product_id,
        )
        if not product:
            raise HTTPException(status_code=404, detail="Product not found")

        offer_rows = await conn.fetch(
            """
            SELECT
                vo.id::text AS offer_id, v.name AS vendor_name, v.domain AS vendor_domain,
                vo.raw_title, vo.current_price, vo.currency, vo.in_stock,
                COALESCE(vo.affiliate_url, vo.product_url) AS buy_url, vo.last_scraped_at
            FROM vendor_offers vo
            JOIN vendors v ON vo.vendor_id = v.id
            WHERE vo.product_id = $1::uuid
            ORDER BY vo.in_stock DESC, vo.current_price ASC;
            """,
            product_id,
        )

    specs = product["specifications"] or {}
    if isinstance(specs, str):
        specs = json.loads(specs)

    response_data = {
        "product_id": product["id"],
        "title": product["title"],
        "brand": product["brand"],
        "model_code": product["model_code"],
        "image_url": product["image_url"],
        "specifications": specs,
        "offers": [
            {
                "offer_id": r["offer_id"],
                "vendor_name": r["vendor_name"],
                "vendor_domain": r["vendor_domain"],
                "raw_title": r["raw_title"],
                "current_price": float(r["current_price"]),
                "currency": r["currency"],
                "in_stock": r["in_stock"],
                "buy_url": r["buy_url"],
                "last_scraped_at": r["last_scraped_at"].isoformat(),
            }
            for r in offer_rows
        ],
    }
    await cache.set_cached_json(cache_key, response_data, ttl_seconds=900)
    return response_data


@router.get("/{product_id}/history", response_model=PriceHistoryResponse)
async def get_price_history(
    product_id: str = Path(..., description="UUID of master product"),
    days: int = Query(30, ge=7, le=365),
):
    pool = get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ph.price, ph.in_stock, ph.recorded_at
            FROM price_history ph
            JOIN vendor_offers vo ON ph.offer_id = vo.id
            WHERE vo.product_id = $1::uuid
                AND ph.recorded_at >= NOW() - ($2 || ' days')::interval
            ORDER BY ph.recorded_at ASC;
            """,
            product_id, str(days),
        )

    return PriceHistoryResponse(
        product_id=product_id,
        currency="USD",
        history=[
            PricePointSchema(price=float(r["price"]), in_stock=r["in_stock"], recorded_at=r["recorded_at"])
            for r in rows
        ],
    )


@router.get("/{product_id}/insights", response_model=Dict[str, Any])
def get_product_buying_insights(product_id: str):
    """[USP] Real-time 'BUY_NOW' vs 'WAIT' recommendation backed by 90-day price history."""
    try:
        insights = calculate_buy_timing_recommendation(product_id)
        return {"status": "success", "data": insights}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate insights: {e}")


@router.get("/{product_id}/alternatives", response_model=Dict[str, Any])
def get_cheaper_feature_alternatives(product_id: str, limit: int = Query(default=3, le=10)):
    """[USP] Vector similarity search for cheaper, spec-equivalent cross-brand alternatives."""
    try:
        alternatives = find_feature_equivalent_alternatives(product_id, limit=limit)
        return {"status": "success", "count": len(alternatives), "data": alternatives}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to calculate alternatives: {e}")
