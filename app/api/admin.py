# app/api/admin.py
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel, EmailStr

from app.config import settings
from app.db_sync import get_conn, invalidate_grid_cache

router = APIRouter(prefix="/api/v1/admin", tags=["Admin"])

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/admin/auth/login")


class TokenData(BaseModel):
    user_id: str
    email: EmailStr
    role: str


class LoginSchema(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ReviewDecisionSchema(BaseModel):
    offer_id: str
    approved: bool
    correct_product_id: Optional[str] = None


def get_current_user(token: str = Depends(oauth2_scheme)) -> TokenData:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        return TokenData(user_id=payload["sub"], email=payload["email"], role=payload["role"])
    except (JWTError, KeyError):
        raise credentials_exception


def require_admin(current_user: TokenData = Depends(get_current_user)) -> TokenData:
    if current_user.role not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Forbidden: Admin credentials required.")
    return current_user


# ------------------------------------------------------------------
# Auth
# ------------------------------------------------------------------
# NOTE: single hardcoded admin credential from settings, meant to unblock
# local dev/demos without a full user table. Replace with a real `users`
# table + hashed passwords (e.g. passlib/bcrypt) before shipping this to
# more than one admin or to production.

@router.post("/auth/login", response_model=TokenResponse)
def login(payload: LoginSchema):
    if payload.email != settings.ADMIN_EMAIL or payload.password != settings.ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    token = jwt.encode(
        {"sub": "admin-1", "email": payload.email, "role": "admin", "exp": expire},
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    return TokenResponse(access_token=token)


# ------------------------------------------------------------------
# Moderation (HITL review queue) -- now actually protected
# ------------------------------------------------------------------

@router.get("/pending-matches", dependencies=[Depends(require_admin)])
def get_pending_matches():
    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT
                vo.id AS offer_id, vo.raw_title AS vendor_title, vo.current_price,
                vo.confidence_score, v.name AS vendor_name,
                p.id AS suggested_product_id, p.title AS suggested_product_title
            FROM vendor_offers vo
            JOIN vendors v ON vo.vendor_id = v.id
            JOIN products p ON vo.product_id = p.id
            WHERE vo.match_status = 'pending_review'
            ORDER BY vo.last_scraped_at DESC
            LIMIT 50;
            """
        )
        rows = cursor.fetchall()
    return {"status": "success", "count": len(rows), "data": rows}


@router.post("/review-match", dependencies=[Depends(require_admin)])
def review_match(decision: ReviewDecisionSchema):
    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        try:
            if decision.approved:
                if decision.correct_product_id:
                    cursor.execute(
                        "UPDATE vendor_offers SET product_id = %s::uuid, match_status = 'matched' WHERE id = %s::uuid RETURNING product_id;",
                        (decision.correct_product_id, decision.offer_id),
                    )
                else:
                    cursor.execute(
                        "UPDATE vendor_offers SET match_status = 'matched' WHERE id = %s::uuid RETURNING product_id;",
                        (decision.offer_id,),
                    )
            else:
                cursor.execute(
                    "SELECT raw_title, current_price FROM vendor_offers WHERE id = %s::uuid;",
                    (decision.offer_id,),
                )
                offer = cursor.fetchone()
                if not offer:
                    raise HTTPException(status_code=404, detail="Offer not found")
                cursor.execute(
                    "INSERT INTO products (title) VALUES (%s) RETURNING id;",
                    (offer["raw_title"],),
                )
                new_product_id = cursor.fetchone()["id"]
                cursor.execute(
                    "UPDATE vendor_offers SET product_id = %s, match_status = 'matched' WHERE id = %s::uuid RETURNING product_id;",
                    (new_product_id, decision.offer_id),
                )

            updated_row = cursor.fetchone()
            conn.commit()

            if updated_row:
                invalidate_grid_cache(str(updated_row["product_id"]))

            return {"status": "success"}
        except HTTPException:
            conn.rollback()
            raise
        except Exception as e:
            conn.rollback()
            raise HTTPException(status_code=500, detail=f"Review failed: {e}")
