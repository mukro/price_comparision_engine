# app/ml/ocr_intelligence.py
"""
OCR Intelligence Layer
- Takes raw OCR text + image
- Uses LLM (OpenAI GPT-4V or local LLaVA) to extract structured data
- Auto-matches to product catalog
- Fraud detection on submissions
"""
import json
import re
from typing import Dict, List, Optional

import openai
from sqlalchemy import text

from app.config import settings
from app.db import get_db_pool


class OCRIntelligence:
    """
    Post-processes OCR submissions using LLM for structured extraction
    and catalog matching.
    """
    
    def __init__(self):
        self.client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY) if hasattr(settings, 'OPENAI_API_KEY') else None

    async def extract_from_ocr(self, raw_text: str, image_url: Optional[str] = None) -> Dict:
        """
        Use LLM to extract structured price data from OCR text.
        Falls back to regex if LLM unavailable.
        """
        if not self.client or not raw_text:
            return self._fallback_extract(raw_text)
        
        prompt = f"""
        You are a price extraction specialist. Extract structured data from this OCR text from a store shelf or product label.
        
        OCR Text:
        {raw_text}
        
        Extract and return ONLY a JSON object with these fields:
        - product_name: The full product name (e.g., "Apple iPhone 15 128GB Black")
        - brand: Brand name (e.g., "Apple")
        - price: Numeric price only (e.g., 72900)
        - currency: Currency code (e.g., "INR")
        - mrp: Maximum retail price if shown (numeric)
        - discount_pct: Discount percentage if shown (numeric)
        - store_name: Store/vendor name if visible
        - in_stock: true/false if stock status is visible
        - confidence: Your confidence 0-1 in this extraction
        
        Rules:
        - If price has commas (72,900), remove them
        - If multiple prices shown, pick the current selling price
        - If product name is unclear, set confidence < 0.5
        - Return ONLY valid JSON, no markdown
        """
        
        try:
            response = await self.client.chat.completions.create(
                model="gpt-4o-mini",  # Cheap and fast
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=300,
            )
            
            content = response.choices[0].message.content.strip()
            # Clean up markdown code blocks if present
            content = content.replace("```json", "").replace("```", "").strip()
            extracted = json.loads(content)
            extracted["extraction_method"] = "llm"
            return extracted
            
        except Exception as e:
            return self._fallback_extract(raw_text)

    def _fallback_extract(self, raw_text: str) -> Dict:
        """Regex-based fallback extraction."""
        # Price pattern: ₹72,900 or Rs. 72900 or 72900/-
        price_match = re.search(r'[₹Rs\.]+\s*([\d,]+)', raw_text)
        price = float(price_match.group(1).replace(',', '')) if price_match else None
        
        # MRP pattern
        mrp_match = re.search(r'MRP[:\s]*[₹Rs\.]*\s*([\d,]+)', raw_text, re.IGNORECASE)
        mrp = float(mrp_match.group(1).replace(',', '')) if mrp_match else None
        
        # Brand detection (common brands)
        brands = ["Apple", "Samsung", "Sony", "OnePlus", "Xiaomi", "Realme", "Nokia", "LG"]
        detected_brand = next((b for b in brands if b.lower() in raw_text.lower()), None)
        
        return {
            "product_name": raw_text[:100],  # Truncated raw text
            "brand": detected_brand,
            "price": price,
            "currency": "INR",
            "mrp": mrp,
            "discount_pct": round((mrp - price) / mrp * 100, 1) if mrp and price else None,
            "store_name": None,
            "in_stock": "out of stock" not in raw_text.lower(),
            "confidence": 0.4 if price else 0.1,
            "extraction_method": "regex_fallback",
        }

    async def match_to_catalog(self, extracted: Dict) -> Dict:
        """
        Match extracted product to existing catalog using pgvector + fuzzy text.
        Returns matched product_id or creates pending match.
        """
        pool = get_db_pool()
        product_name = extracted.get("product_name", "")
        brand = extracted.get("brand", "")
        
        async with pool.acquire() as conn:
            # Strategy 1: Exact model code match
            if brand:
                exact = await conn.fetchrow(
                    "SELECT id, title FROM products WHERE brand = $1 AND (title % $2 OR model_code = $3) LIMIT 1;",
                    brand, product_name, product_name,
                )
                if exact:
                    return {
                        "matched": True,
                        "product_id": str(exact["id"]),
                        "product_title": exact["title"],
                        "match_method": "exact",
                    }
            
            # Strategy 2: Vector similarity search
            # (Requires generating embedding for OCR text — simplified here)
            similar = await conn.fetch(
                """
                SELECT id, title, 1 - (embedding <=> query_embedding) as similarity
                FROM products
                WHERE title % $1
                ORDER BY similarity DESC
                LIMIT 3;
                """,
                product_name,
            )
            
            if similar and similar[0].get("similarity", 0) > 0.7:
                return {
                    "matched": True,
                    "product_id": str(similar[0]["id"]),
                    "product_title": similar[0]["title"],
                    "match_method": "similarity",
                    "similarity": similar[0]["similarity"],
                }
            
            # Strategy 3: Create pending match for admin review
            return {
                "matched": False,
                "suggested_matches": [
                    {"id": str(s["id"]), "title": s["title"], "similarity": s.get("similarity")}
                    for s in similar[:3]
                ],
                "match_method": "pending_review",
            }

    async def detect_fraud(self, submission: Dict) -> Dict:
        """
        Detect suspicious OCR submissions.
        - Same device hash flooding submissions
        - Prices that are extreme outliers
        - Text that doesn't look like a price tag
        """
        pool = get_db_pool()
        device_hash = submission.get("device_hash")
        price = submission.get("price")
        
        flags = []
        risk_score = 0.0
        
        # Check 1: Submission velocity from same device
        if device_hash:
            async with pool.acquire() as conn:
                recent = await conn.fetchrow(
                    "SELECT COUNT(*) as cnt FROM ocr_submissions WHERE device_hash = $1 AND created_at > NOW() - INTERVAL '1 hour';",
                    device_hash,
                )
                if recent and recent["cnt"] > 10:
                    flags.append("High submission velocity")
                    risk_score += 0.3
        
        # Check 2: Price outlier (if we have product context)
        product_id = submission.get("product_id")
        if product_id and price:
            async with pool.acquire() as conn:
                price_stats = await conn.fetchrow(
                    "SELECT AVG(current_price) as avg, STDDEV(current_price) as std FROM vendor_offers WHERE product_id = $1::uuid;",
                    product_id,
                )
                if price_stats and price_stats["std"]:
                    z_score = abs(price - price_stats["avg"]) / price_stats["std"]
                    if z_score > 3:
                        flags.append(f"Price outlier (z={z_score:.1f})")
                        risk_score += 0.4
        
        # Check 3: Text quality heuristics
        raw_text = submission.get("raw_text", "")
        if len(raw_text) < 10:
            flags.append("Suspiciously short text")
            risk_score += 0.2
        
        return {
            "is_fraudulent": risk_score > 0.5,
            "risk_score": round(risk_score, 2),
            "flags": flags,
            "recommendation": "reject" if risk_score > 0.7 else "review" if risk_score > 0.3 else "approve",
        }
