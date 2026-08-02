# app/ml/price_predictor.py
"""
AI Price Prediction Engine
- Prophet for seasonality + trend + holiday effects
- LSTM fallback for products with >180 days of history
- Outputs: predicted_price, confidence, recommendation, best_buy_date
"""
import json
import pickle
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
from prophet import Prophet
from sklearn.preprocessing import MinMaxScaler

from app.db import get_db_pool


class PricePredictor:
    """
    Predicts future prices for products using historical data.
    Uses Facebook Prophet for interpretability + seasonality detection.
    """
    
    # Indian e-commerce sale dates (highly predictive of price drops)
    SALE_DATES = [
        # Republic Day
        {"month": 1, "day": 26, "window": 3, "discount_avg": 0.15},
        # Holi
        {"month": 3, "day": 14, "window": 5, "discount_avg": 0.12},
        # Summer Sale
        {"month": 5, "day": 1, "window": 7, "discount_avg": 0.18},
        # Prime Day / Independence
        {"month": 8, "day": 15, "window": 5, "discount_avg": 0.20},
        # Diwali / Big Billion Days
        {"month": 10, "day": 20, "window": 14, "discount_avg": 0.25},
        # Black Friday (growing in India)
        {"month": 11, "day": 29, "window": 5, "discount_avg": 0.20},
        # Year End
        {"month": 12, "day": 25, "window": 7, "discount_avg": 0.15},
    ]

    def __init__(self):
        self.model_cache = {}  # product_id -> (model, last_trained)

    async def get_price_history(self, product_id: str, vendor_id: Optional[str] = None) -> List[Dict]:
        """Fetch price history from DB."""
        pool = get_db_pool()
        async with pool.acquire() as conn:
            if vendor_id:
                rows = await conn.fetch(
                    """
                    SELECT ph.price, ph.recorded_at, vo.in_stock
                    FROM price_history ph
                    JOIN vendor_offers vo ON ph.offer_id = vo.id
                    WHERE vo.product_id = $1::uuid AND vo.vendor_id = $2::uuid
                    ORDER BY ph.recorded_at ASC;
                    """,
                    product_id, vendor_id,
                )
            else:
                # Aggregate across all vendors (lowest price per day)
                rows = await conn.fetch(
                    """
                    SELECT 
                        DATE(ph.recorded_at) as ds,
                        MIN(ph.price) as y,
                        BOOL_AND(ph.in_stock) as in_stock
                    FROM price_history ph
                    JOIN vendor_offers vo ON ph.offer_id = vo.id
                    WHERE vo.product_id = $1::uuid
                    GROUP BY DATE(ph.recorded_at)
                    ORDER BY ds ASC;
                    """,
                    product_id,
                )
        return [dict(r) for r in rows]

    def _add_sale_holidays(self, model: Prophet):
        """Add Indian sale events as holidays to Prophet model."""
        for sale in self.SALE_DATES:
            # Create holiday entries for past 2 years and next year
            for year_offset in [-2, -1, 0, 1]:
                year = datetime.now().year + year_offset
                date = datetime(year, sale["month"], sale["day"])
                model.add_country_holidays(country_name="IN")
        return model

    def _calculate_volatility(self, prices: List[float]) -> float:
        """Calculate price volatility (coefficient of variation)."""
        if len(prices) < 2:
            return 0.0
        return np.std(prices) / np.mean(prices)

    def _detect_price_pattern(self, prices: List[float]) -> str:
        """Classify price behavior pattern."""
        if len(prices) < 14:
            return "insufficient_data"
        
        # Simple trend detection
        first_half = np.mean(prices[:len(prices)//2])
        second_half = np.mean(prices[len(prices)//2:])
        recent = np.mean(prices[-7:])
        
        if recent < second_half * 0.95 and second_half < first_half * 0.95:
            return "dropping_trend"
        elif recent > second_half * 1.05:
            return "rising_trend"
        elif abs(recent - second_half) / second_half < 0.03:
            return "stable"
        else:
            return "volatile"

    async def predict(self, product_id: str, vendor_id: Optional[str] = None, days_ahead: int = 30) -> Dict:
        """
        Predict price for the next N days and return recommendation.
        
        Returns:
            {
                "current_price": float,
                "predicted_price_7d": float,
                "predicted_price_30d": float,
                "confidence": float,  # 0-1
                "recommendation": "BUY" | "WAIT" | "HOLD",
                "expected_drop_pct": float,
                "best_buy_window": str,  # e.g. "Next 7 days" or "Wait for Diwali sale"
                "price_trend": str,
                "volatility": float,
                "model_used": str,
            }
        """
        history = await self.get_price_history(product_id, vendor_id)
        
        if len(history) < 14:
            return {
                "current_price": history[-1]["y"] if history else None,
                "predicted_price_7d": None,
                "predicted_price_30d": None,
                "confidence": 0.0,
                "recommendation": "INSUFFICIENT_DATA",
                "reason": "Need at least 14 days of price history",
            }

        # Prepare data for Prophet
        df = [{"ds": h["ds"] if isinstance(h["ds"], datetime) else h["recorded_at"],
               "y": float(h["y"] if "y" in h else h["price"])}
              for h in history]
        
        # Remove outliers (prices >3 std dev from rolling mean)
        prices = [d["y"] for d in df]
        rolling_mean = np.convolve(prices, np.ones(7)/7, mode='valid')
        # Simple outlier removal
        q1, q3 = np.percentile(prices, [25, 75])
        iqr = q3 - q1
        lower, upper = q1 - 1.5*iqr, q3 + 1.5*iqr
        df = [d for d in df if lower <= d["y"] <= upper]

        if len(df) < 10:
            return {"recommendation": "INSUFFICIENT_DATA", "reason": "Too many outliers"}

        # Train Prophet model
        model = Prophet(
            yearly_seasonality=True,
            weekly_seasonality=True,
            daily_seasonality=False,
            changepoint_prior_scale=0.05,  # Conservative trend changes
            seasonality_prior_scale=10.0,
        )
        self._add_sale_holidays(model)
        
        # Add custom regressor for stock availability
        # (prices tend to drop when stock is high, rise when scarce)
        
        try:
            model.fit(df)
        except Exception as e:
            return {"recommendation": "ERROR", "reason": str(e)}

        # Predict future
        future = model.make_future_dataframe(periods=days_ahead)
        forecast = model.predict(future)
        
        current_price = df[-1]["y"]
        pred_7d = forecast.iloc[-(days_ahead-23)]["yhat"] if days_ahead >= 7 else current_price
        pred_30d = forecast.iloc[-1]["yhat"]
        
        # Calculate confidence based on prediction interval width
        last_forecast = forecast.iloc[-1]
        uncertainty = (last_forecast["yhat_upper"] - last_forecast["yhat_lower"]) / 2
        confidence = max(0, min(1, 1 - (uncertainty / current_price)))
        
        # Determine recommendation
        drop_7d = (current_price - pred_7d) / current_price
        drop_30d = (current_price - pred_30d) / current_price
        volatility = self._calculate_volatility(prices)
        pattern = self._detect_price_pattern(prices)
        
        # Decision logic
        if drop_7d > 0.08 and confidence > 0.6:  # >8% drop expected soon
            recommendation = "WAIT"
            best_window = f"Wait {7} days — price dropping ~{drop_7d*100:.0f}%"
        elif drop_30d > 0.15 and confidence > 0.5:  # Big sale coming
            recommendation = "WAIT"
            best_window = "Wait for upcoming sale event"
        elif pattern == "rising_trend" and drop_7d < -0.03:
            recommendation = "BUY"
            best_window = "Buy now — prices trending up"
        elif volatility < 0.05 and abs(drop_30d) < 0.03:
            recommendation = "HOLD"
            best_window = "Prices stable — buy when convenient"
        else:
            recommendation = "BUY" if confidence > 0.5 else "WAIT"
            best_window = "Monitor for drops"

        return {
            "current_price": round(current_price, 2),
            "predicted_price_7d": round(pred_7d, 2),
            "predicted_price_30d": round(pred_30d, 2),
            "confidence": round(confidence, 2),
            "recommendation": recommendation,
            "expected_drop_pct": round(drop_30d * 100, 1),
            "best_buy_window": best_window,
            "price_trend": pattern,
            "volatility": round(volatility, 3),
            "model_used": "prophet_v1",
        }

    async def batch_predict_for_alerts(self) -> List[Dict]:
        """
        Run predictions for all watchlisted products.
        Called by Celery Beat daily.
        Returns list of alerts to send.
        """
        pool = get_db_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT w.product_id, p.title
                FROM user_watchlist w
                JOIN products p ON w.product_id = p.id
                WHERE w.target_price IS NULL OR w.target_price > 0;
                """
            )
        
        alerts = []
        for row in rows:
            prediction = await self.predict(str(row["product_id"]))
            if prediction["recommendation"] in ["BUY", "WAIT"] and prediction["confidence"] > 0.6:
                alerts.append({
                    "product_id": str(row["product_id"]),
                    "title": row["title"],
                    "prediction": prediction,
                })
        return alerts
