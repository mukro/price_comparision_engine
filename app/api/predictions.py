# app/api/predictions.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.ml.price_predictor import PricePredictor

router = APIRouter(prefix="/api/v1/predictions", tags=["AI Predictions"])

predictor = PricePredictor()

class PredictionResponse(BaseModel):
    current_price: float
    predicted_price_7d: float
    predicted_price_30d: float
    confidence: float
    recommendation: str
    expected_drop_pct: float
    best_buy_window: str
    price_trend: str


@router.get("/{product_id}")
async def get_price_prediction(product_id: str):
    """Get AI-powered price prediction for a product."""
    result = await predictor.predict(product_id)
    if result.get("recommendation") == "ERROR":
        raise HTTPException(status_code=500, detail=result["reason"])
    if result.get("recommendation") == "INSUFFICIENT_DATA":
        raise HTTPException(status_code=404, detail=result["reason"])
    return result


@router.get("/{product_id}/chart")
async def get_prediction_chart(product_id: str, days: int = 90):
    """
    Return prediction data formatted for chart rendering.
    Includes historical prices + forecast line + confidence bands.
    """
    # Implementation: return Prophet forecast dataframe as JSON
    pass
