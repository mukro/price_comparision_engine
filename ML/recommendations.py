# app/ml/recommendations.py
"""
Personalization Engine
- Collaborative filtering for product recommendations
- Session-based recommendations
- Price drop recommendations based on browsing history
"""
from typing import Dict, List, Optional

import numpy as np


class RecommendationEngine:
    """
    Generates personalized product recommendations.
    """
    
    async def get_recommendations_for_user(self, user_id: str, limit: int = 10) -> List[Dict]:
        """
        Get personalized recommendations based on:
        1. Products user viewed but didn't buy
        2. Similar users' purchase patterns
        3. Price drops on watchlisted categories
        4. Trending products in user's price range
        """
        pool = get_db_pool()
        
        async with pool.acquire() as conn:
            # Strategy 1: Recently viewed, not purchased
            viewed = await conn.fetch(
                """
                SELECT DISTINCT product_id, MAX(clicked_at) as last_viewed
                FROM affiliate_clicks
                WHERE user_id = $1::uuid AND converted_at IS NULL
                GROUP BY product_id
                ORDER BY last_viewed DESC
                LIMIT 5;
                """,
                user_id,
            )
            
            # Strategy 2: Similar products to watchlist
            watchlist = await conn.fetch(
                """
                SELECT product_id FROM user_watchlist WHERE user_id = $1::uuid;
                """,
                user_id,
            )
            watchlist_ids = [str(w["product_id"]) for w in watchlist]
            
            # Strategy 3: Price drops in last 24h
            drops = await conn.fetch(
                """
                SELECT p.id, p.title, p.image_url, MIN(vo.current_price) as price,
                       (SELECT price FROM price_history WHERE offer_id = vo.id ORDER BY recorded_at DESC OFFSET 1 LIMIT 1) as old_price
                FROM products p
                JOIN vendor_offers vo ON vo.product_id = p.id
                WHERE vo.current_price < (
                    SELECT AVG(price) FROM price_history ph2 
                    WHERE ph2.offer_id = vo.id AND ph2.recorded_at > NOW() - INTERVAL '7 days'
                ) * 0.9
                AND vo.in_stock = TRUE
                GROUP BY p.id, vo.id
                HAVING COUNT(DISTINCT vo.vendor_id) >= 2
                ORDER BY (old_price - vo.current_price) DESC
                LIMIT 5;
                """
            )
        
        recommendations = []
        
        # Add viewed-but-not-bought with price prediction
        for v in viewed:
            recommendations.append({
                "product_id": str(v["product_id"]),
                "reason": "You recently viewed this",
                "type": "recently_viewed",
            })
        
        # Add price drops
        for d in drops:
            drop_pct = (d["old_price"] - d["price"]) / d["old_price"] * 100 if d["old_price"] else 0
            recommendations.append({
                "product_id": str(d["id"]),
                "title": d["title"],
                "image_url": d["image_url"],
                "current_price": float(d["price"]),
                "drop_pct": round(drop_pct, 1),
                "reason": f"Price dropped {drop_pct:.0f}%",
                "type": "price_drop",
            })
        
        return recommendations[:limit]
    
    async def get_trending_products(self, category: Optional[str] = None, limit: int = 10) -> List[Dict]:
        """
        Get trending products based on click velocity and conversion rate.
        """
        pool = get_db_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT 
                    p.id, p.title, p.image_url, p.brand,
                    COUNT(DISTINCT ac.id) as click_count,
                    COUNT(DISTINCT CASE WHEN ac.status = 'converted' THEN ac.id END) as conversion_count,
                    MIN(vo.current_price) as price
                FROM products p
                JOIN vendor_offers vo ON vo.product_id = p.id
                LEFT JOIN affiliate_clicks ac ON ac.product_id = p.id AND ac.clicked_at > NOW() - INTERVAL '7 days'
                WHERE ($1::varchar IS NULL OR p.category = $1)
                GROUP BY p.id
                HAVING COUNT(DISTINCT ac.id) > 5
                ORDER BY click_count DESC, conversion_count DESC
                LIMIT $2;
                """,
                category, limit,
            )
        
        return [
            {
                "product_id": str(r["id"]),
                "title": r["title"],
                "image_url": r["image_url"],
                "brand": r["brand"],
                "price": float(r["price"]),
                "click_count": r["click_count"],
                "conversion_rate": round(r["conversion_count"] / r["click_count"] * 100, 1) if r["click_count"] > 0 else 0,
            }
            for r in rows
        ]
