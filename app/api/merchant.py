# app/api/merchant.py
"""
B2B Merchant Pricing Engine with row-level security.
Each merchant JWT claim contains their merchant_id, and all endpoints
enforce that merchants can only read/write their own rules.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from jose import JWTError, jwt
from pydantic import BaseModel, Field

from app.api.admin import get_current_user, oauth2_scheme
from app.config import settings
from app.core.dynamic_pricing import evaluate_merchant_rules_for_product
from app.db_sync import get_conn

router = APIRouter(prefix="/api/v1/merchant", tags=["B2B Merchant Pricing Engine"])


class MerchantRuleSchema(BaseModel):
    product_id: str
    merchant_cost: float = Field(gt=0)
    min_margin_pct: float = Field(default=15.0, ge=0)
    map_price: Optional[float] = Field(default=None, ge=0)
    strategy: str = Field(default="undercut_by_fixed")
    strategy_value: float = Field(default=1.00)
    webhook_url: Optional[str] = None
    auto_apply_enabled: bool = False


class MerchantTokenData(BaseModel):
    merchant_id: str
    email: str
    role: str


def get_current_merchant(token: str = Depends(oauth2_scheme)) -> MerchantTokenData:
    """
    Decodes a merchant-scoped JWT.
    Expected payload: {"sub": "merchant-123", "email": "...", "role": "merchant"}
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate merchant credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        if payload.get("role") != "merchant":
            raise credentials_exception
        return MerchantTokenData(
            merchant_id=payload["sub"],
            email=payload["email"],
            role=payload["role"],
        )
    except (JWTError, KeyError):
        raise credentials_exception


def _require_merchant_ownership(
    merchant_id_in_path: Optional[str],
    current_merchant: MerchantTokenData,
) -> str:
    """
    Row-level security gate: ensures the caller owns the resource.
    Returns the effective merchant_id to use in queries.
    """
    effective_id = current_merchant.merchant_id
    if merchant_id_in_path and merchant_id_in_path != effective_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only access your own merchant rules.",
        )
    return effective_id


# ------------------------------------------------------------------
# Merchant Auth (separate from admin — merchants get scoped JWTs)
# ------------------------------------------------------------------

class MerchantLoginSchema(BaseModel):
    merchant_id: str
    api_key: str  # In production, verify against hashed keys in DB


@router.post("/auth/login")
def merchant_login(payload: MerchantLoginSchema):
    """
    Authenticates a merchant and returns a scoped JWT.
    NOTE: This is a minimal implementation. In production, verify api_key
    against a `merchant_api_keys` table with bcrypt hashing.
    """
    # TODO: replace with real API key verification
    # from passlib.context import CryptContext
    # pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    # if not verify_api_key(payload.merchant_id, payload.api_key): raise 401

    from datetime import datetime, timedelta, timezone
    from jose import jwt as jose_jwt

    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    token = jose_jwt.encode(
        {"sub": payload.merchant_id, "email": f"{payload.merchant_id}@merchant.local", "role": "merchant", "exp": expire},
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    return {"access_token": token, "token_type": "bearer"}


# ------------------------------------------------------------------
# Merchant Rules (row-level secured)
# ------------------------------------------------------------------

@router.post("/rules", status_code=status.HTTP_201_CREATED)
def create_or_update_merchant_rule(
    payload: MerchantRuleSchema,
    current_merchant: MerchantTokenData = Depends(get_current_merchant),
):
    """
    Creates or updates a pricing rule for the authenticated merchant.
    Merchants CANNOT modify other merchants' rules.
    """
    merchant_id = current_merchant.merchant_id

    with get_conn() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO merchant_rules (
                    merchant_id, product_id, merchant_cost, min_margin_pct,
                    map_price, strategy, strategy_value, webhook_url, auto_apply_enabled
                ) VALUES (%s, %s::uuid, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (merchant_id, product_id) DO UPDATE SET
                    merchant_cost = EXCLUDED.merchant_cost,
                    min_margin_pct = EXCLUDED.min_margin_pct,
                    map_price = EXCLUDED.map_price,
                    strategy = EXCLUDED.strategy,
                    strategy_value = EXCLUDED.strategy_value,
                    webhook_url = EXCLUDED.webhook_url,
                    auto_apply_enabled = EXCLUDED.auto_apply_enabled,
                    updated_at = NOW();
                """,
                (
                    merchant_id, payload.product_id, payload.merchant_cost,
                    payload.min_margin_pct, payload.map_price, payload.strategy,
                    payload.strategy_value, payload.webhook_url, payload.auto_apply_enabled,
                ),
            )
            conn.commit()
            return {
                "status": "success",
                "message": "Merchant dynamic pricing rule configured successfully.",
                "merchant_id": merchant_id,
            }
        except Exception as e:
            conn.rollback()
            raise HTTPException(status_code=500, detail=f"Database execution failed: {e}")


@router.get("/rules")
def list_my_rules(
    current_merchant: MerchantTokenData = Depends(get_current_merchant),
):
    """Returns all pricing rules belonging to the authenticated merchant."""
    merchant_id = current_merchant.merchant_id

    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, product_id, merchant_cost, min_margin_pct, map_price,
                   strategy, strategy_value, webhook_url, auto_apply_enabled,
                   is_active, created_at, updated_at
            FROM merchant_rules
            WHERE merchant_id = %s
            ORDER BY updated_at DESC;
            """,
            (merchant_id,),
        )
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        return {
            "status": "success",
            "merchant_id": merchant_id,
            "count": len(rows),
            "data": [dict(zip(columns, row)) for row in rows],
        }


@router.get("/rules/{rule_id}")
def get_rule_by_id(
    rule_id: str,
    current_merchant: MerchantTokenData = Depends(get_current_merchant),
):
    """Returns a single rule if owned by the authenticated merchant."""
    merchant_id = current_merchant.merchant_id

    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT * FROM merchant_rules
            WHERE id = %s::uuid AND merchant_id = %s;
            """,
            (rule_id, merchant_id),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Rule not found or access denied.")
        return {"status": "success", "data": dict(row)}


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rule(
    rule_id: str,
    current_merchant: MerchantTokenData = Depends(get_current_merchant),
):
    """Soft-deletes a rule by setting is_active = FALSE (merchant-scoped)."""
    merchant_id = current_merchant.merchant_id

    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE merchant_rules
            SET is_active = FALSE, updated_at = NOW()
            WHERE id = %s::uuid AND merchant_id = %s;
            """,
            (rule_id, merchant_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Rule not found or access denied.")
        conn.commit()
    return {"status": "success", "message": "Rule deactivated"}


@router.get("/evaluate/{product_id}")
def simulate_counter_strategy(
    product_id: str,
    competitor_price: float,
    current_merchant: MerchantTokenData = Depends(get_current_merchant),
):
    """
    Simulates counter-strategy for the merchant's own rules only.
    Does NOT reveal other merchants' cost/margin data.
    """
    merchant_id = current_merchant.merchant_id

    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT 1 FROM merchant_rules
            WHERE merchant_id = %s AND product_id = %s::uuid AND is_active = TRUE
            LIMIT 1;
            """,
            (merchant_id, product_id),
        )
        if not cursor.fetchone():
            raise HTTPException(
                status_code=404,
                detail="No active rule found for this product under your merchant account.",
            )

    evaluations = evaluate_merchant_rules_for_product(product_id, competitor_price)
    if not evaluations:
        raise HTTPException(status_code=404, detail="Evaluation engine returned no data.")

    # Filter to only this merchant's evaluation
    my_eval = [
        e for e in evaluations.get("merchant_evaluations", [])
        if e["merchant_id"] == merchant_id
    ]
    return {"status": "success", "merchant_id": merchant_id, "data": my_eval}


# ------------------------------------------------------------------
# Admin Override (for support / superadmin access)
# ------------------------------------------------------------------

@router.get("/admin/all-rules", dependencies=[Depends(get_current_user)])
def admin_list_all_rules():
    """Admin-only: lists all merchant rules across the platform."""
    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM merchant_rules ORDER BY updated_at DESC LIMIT 500;")
        return {"status": "success", "count": cursor.rowcount, "data": cursor.fetchall()}
