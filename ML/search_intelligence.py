# app/ml/search_intelligence.py
"""
AI Search Intelligence
- Classifies query intent (price comparison, product discovery, deal hunting)
- Reranks results based on user behavior signals
- Generates query embeddings for better matching
"""
import json
from typing import Dict, List

import openai
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.naive_bayes import MultinomialNB
import joblib

from app.config import settings


class SearchIntelligence:
    """
    Enhances product search with AI understanding.
    """
    
    INTENTS = {
        "price_compare": ["cheapest", "best price", "lowest", "compare", "vs", "difference"],
        "deal_hunt": ["deal", "discount", "sale", "offer", "cashback", "under"],
        "product_discovery": ["best", "top", "good", "recommended", "which"],
        "spec_query": ["ram", "gb", "mah", "mp", "inch", "display", "processor"],
        "buy_ready": ["buy", "purchase", "order", "where to buy"],
    }
    
    def __init__(self):
        self.client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY) if hasattr(settings, 'OPENAI_API_KEY') else None
        # Simple intent classifier (can be replaced with fine-tuned model)
        self._init_intent_classifier()

    def _init_intent_classifier(self):
        """Initialize a lightweight intent classifier."""
        # In production, load a pre-trained model
        self.vectorizer = TfidfVectorizer(max_features=1000)
        self.classifier = MultinomialNB()
        
        # Training data (simplified — in production use labeled queries)
        training_queries = []
        training_labels = []
        for intent, keywords in self.INTENTS.items():
            for kw in keywords:
                training_queries.append(kw)
                training_labels.append(intent)
        
        if training_queries:
            X = self.vectorizer.fit_transform(training_queries)
            self.classifier.fit(X, training_labels)

    def classify_intent(self, query: str) -> Dict:
        """Classify search intent."""
        query_lower = query.lower()
        
        # Rule-based override for common patterns
        if any(w in query_lower for w in ["cheapest", "lowest", "best price"]):
            return {"intent": "price_compare", "confidence": 0.95}
        if any(w in query_lower for w in ["deal", "discount", "offer", "under ₹"]):
            return {"intent": "deal_hunt", "confidence": 0.9}
        if any(w in query_lower for w in ["buy", "purchase"]):
            return {"intent": "buy_ready", "confidence": 0.85}
        
        # ML fallback
        try:
            X = self.vectorizer.transform([query])
            intent = self.classifier.predict(X)[0]
            proba = self.classifier.predict_proba(X)[0].max()
            return {"intent": intent, "confidence": round(proba, 2)}
        except:
            return {"intent": "general", "confidence": 0.5}

    async def generate_query_embedding(self, query: str) -> List[float]:
        """Generate embedding for search query using OpenAI or local model."""
        if not self.client:
            return []  # Fallback to pg_trgm
        
        try:
            response = await self.client.embeddings.create(
                model="text-embedding-3-small",
                input=query,
            )
            return response.data[0].embedding
        except:
            return []

    def rerank_results(self, results: List[Dict], intent: str, user_id: Optional[str] = None) -> List[Dict]:
        """
        Rerank search results based on intent and user signals.
        """
        scored_results = []
        
        for result in results:
            score = result.get("similarity_score", 0.5)
            
            # Intent-based boosting
            if intent == "price_compare":
                # Boost lowest price, in-stock items
                if result.get("is_lowest_price"):
                    score += 0.15
                if result.get("in_stock"):
                    score += 0.1
                    
            elif intent == "deal_hunt":
                # Boost items with highest discount
                if result.get("discount_pct", 0) > 20:
                    score += 0.2
                if result.get("has_offer_text"):
                    score += 0.1
                    
            elif intent == "buy_ready":
                # Boost sponsored listings (merchants pay for this intent!)
                if result.get("is_sponsored"):
                    score += 0.25
                # Boost items with affiliate links
                if result.get("affiliate_url"):
                    score += 0.05
            
            # Sponsored boost (always applied, intent-weighted)
            if result.get("is_sponsored"):
                bid_boost = result.get("sponsor_bid_amount", 0) / 100  # Normalize
                score += min(bid_boost, 0.3)  # Cap at 0.3
            
            scored_results.append({**result, "rerank_score": score})
        
        # Sort by rerank score descending
        scored_results.sort(key=lambda x: x["rerank_score"], reverse=True)
        return scored_results

    def generate_smart_suggestions(self, query: str) -> List[str]:
        """Generate autocomplete suggestions based on popular queries."""
        # In production: query Redis cache of popular searches
        suggestions = []
        
        if "iphone" in query.lower():
            suggestions.extend([
                "iPhone 15 best price",
                "iPhone 15 vs iPhone 14",
                "iPhone 15 deals under 80000",
            ])
        elif "laptop" in query.lower():
            suggestions.extend([
                "laptop under 50000",
                "best laptop for students",
                "gaming laptop deals",
            ])
        
        return suggestions[:5]
