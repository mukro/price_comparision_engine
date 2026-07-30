# app/api/admin.py
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel, EmailStr

from app.config import settings
from app.db_sync import get_conn, invalidate_grid_cache

router = APIRouter(prefix="/api/v1/admin", tags=["Admin"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/admin/auth/login")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


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


class ComplianceToggleSchema(BaseModel):
    scraping_enabled: Optional[bool] = None
    enforce_robots_txt: Optional[bool] = None
    enforce_domain_allowlist: Optional[bool] = None
    default_scrape_rpm: Optional[int] = None


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

@router.post("/auth/login", response_model=TokenResponse)
def login(payload: LoginSchema):
    if payload.email != settings.ADMIN_EMAIL:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not pwd_context.verify(payload.password, settings.ADMIN_PASSWORD_HASH):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    token = jwt.encode(
        {"sub": "admin-1", "email": payload.email, "role": "admin", "exp": expire},
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    return TokenResponse(access_token=token)


# ------------------------------------------------------------------
# Moderation (HITL review queue)
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


# ------------------------------------------------------------------
# Compliance / Governance
# ------------------------------------------------------------------

@router.get("/compliance/settings", dependencies=[Depends(require_admin)])
def get_compliance_settings():
    """Returns current scraping governance configuration (non-sensitive)."""
    return {
        "scraping_enabled": settings.SCRAPING_ENABLED,
        "enforce_robots_txt": settings.ENFORCE_ROBOTS_TXT,
        "enforce_domain_allowlist": settings.ENFORCE_DOMAIN_ALLOWLIST,
        "default_scrape_rpm": settings.DEFAULT_SCRAPE_RPM,
        "scraper_user_agent": settings.SCRAPER_USER_AGENT,
    }


@router.post("/compliance/settings", dependencies=[Depends(require_admin)])
def update_compliance_settings(payload: ComplianceToggleSchema):
    """
    Updates runtime compliance settings in Redis (not persisted to .env).
    For permanent changes, edit .env and restart.
    """
    from app.db_sync import redis_client
    # Store overrides in Redis; tasks read these at runtime
    if payload.scraping_enabled is not None:
        redis_client.set("cfg:scraping_enabled", "1" if payload.scraping_enabled else "0")
    if payload.enforce_robots_txt is not None:
        redis_client.set("cfg:enforce_robots_txt", "1" if payload.enforce_robots_txt else "0")
    if payload.enforce_domain_allowlist is not None:
        redis_client.set("cfg:enforce_domain_allowlist", "1" if payload.enforce_domain_allowlist else "0")
    if payload.default_scrape_rpm is not None:
        redis_client.set("cfg:default_scrape_rpm", str(payload.default_scrape_rpm))
    return {"status": "success", "message": "Compliance settings updated in Redis. Restart workers to apply from env."}


@router.get("/compliance/domains", dependencies=[Depends(require_admin)])
def list_domain_compliance():
    """List all vendors with their scraping compliance flags."""
    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT id, name, domain, is_active, scraping_allowed, scrape_rpm,
                   respects_robots_txt, title_selector, price_selector, stock_selector
            FROM vendors
            ORDER BY domain;
            """
        )
        return {"status": "success", "data": cursor.fetchall()}


@router.patch("/compliance/domains/{vendor_id}", dependencies=[Depends(require_admin)])
def update_domain_compliance(vendor_id: str, scraping_allowed: bool, scrape_rpm: int):
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE vendors
            SET scraping_allowed = %s, scrape_rpm = %s, updated_at = NOW()
            WHERE id = %s::uuid;
            """,
            (scraping_allowed, scrape_rpm, vendor_id),
        )
        conn.commit()
    return {"status": "success", "message": f"Vendor {vendor_id} compliance updated."}
