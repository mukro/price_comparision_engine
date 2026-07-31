# app/api/partner.py
"""
B2B Merchant Partner Onboarding & Feed Management API.

Tiers:
  - Free: Manual feed upload via dashboard
  - Basic: API key auth, daily feed push
  - Premium: Real-time webhooks, priority listing
  - Enterprise: Dedicated support, custom integration

All endpoints require partner API key (X-API-Key header) except registration.
"""
import hashlib
import secrets
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Request, status
from pydantic import BaseModel, Field, validator
from psycopg2.extras import RealDictCursor

from app.db_sync import get_conn
from app.core.telemetry_metrics import PARTNER_FEED_COUNTER

router = APIRouter(prefix="/api/v1/partner", tags=["Merchant Partner Feed"])


# ==========================================
# Schemas
# ==========================================

class PartnerRegistrationSchema(BaseModel):
    company_name: str = Field(..., min_length=2, max_length=255)
    legal_name: Optional[str] = Field(None, max_length=255)
    domain: str = Field(..., regex=r"^[a-zA-Z0-9][a-zA-Z0-9-]{1,61}[a-zA-Z0-9]\.[a-zA-Z]{2,}$")
    website_url: Optional[str] = Field(None, max_length=500)
    primary_email: str = Field(..., regex=r"^[^@]+@[^@]+\.[^@]+$")
    primary_phone: Optional[str] = Field(None, max_length=20)
    gst_number: Optional[str] = Field(None, max_length=20)
    pan_number: Optional[str] = Field(None, max_length=20)
    plan_type: str = Field(default="free", regex=r"^(free|basic|premium|enterprise)$")


class PartnerOfferItemSchema(BaseModel):
    vendor_product_id: str = Field(..., min_length=1, max_length=255)
    product_name: str = Field(..., min_length=1, max_length=500)
    brand: Optional[str] = Field(None, max_length=100)
    model_code: Optional[str] = Field(None, max_length=100)
    price: float = Field(..., gt=0)
    currency: str = Field(default="INR", max_length=10)
    in_stock: bool = Field(default=True)
    stock_quantity: Optional[int] = Field(None, ge=0)
    offer_url: str = Field(..., max_length=1000)
    image_url: Optional[str] = Field(None, max_length=1000)
    offer_valid_until: Optional[datetime] = None

    # Optional: structured specs for better matching
    specifications: Optional[dict] = Field(default_factory=dict)


class PartnerFeedSchema(BaseModel):
    feed_type: str = Field(default="full", regex=r"^(full|delta|price_update|stock_update)$")
    items: List[PartnerOfferItemSchema] = Field(..., max_length=10000)


class PartnerResponseSchema(BaseModel):
    partner_id: str
    company_name: str
    domain: str
    plan_type: str
    onboarding_status: str
    api_key: Optional[str] = None  # Only shown once at registration
    webhook_url: Optional[str] = None
    created_at: datetime


class FeedSubmissionResponseSchema(BaseModel):
    submission_id: str
    status: str
    items_received: int
    items_accepted: int
    items_rejected: int
    errors: List[dict]


# ==========================================
# Auth Dependency
# ==========================================

def _hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def get_partner_from_api_key(x_api_key: str = Header(..., alias="X-API-Key")) -> dict:
    """Validates API key and returns partner record."""
    if not x_api_key or len(x_api_key) < 32:
        raise HTTPException(status_code=401, detail="Invalid API key format")

    key_hash = _hash_api_key(x_api_key)

    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT * FROM merchant_partners
            WHERE api_key_hash = %s AND is_active = TRUE AND onboarding_status = 'approved'
            LIMIT 1;
            """,
            (key_hash,),
        )
        partner = cursor.fetchone()

    if not partner:
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")

    return dict(partner)


# ==========================================
# Endpoints
# ==========================================

@router.post("/register", response_model=PartnerResponseSchema, status_code=status.HTTP_201_CREATED)
def register_partner(payload: PartnerRegistrationSchema):
    """
    Register as a merchant partner. Returns API key (shown ONCE).
    KYC verification required before feed submissions are accepted.
    """
    # Generate API key (shown once, never stored plaintext)
    raw_api_key = f"pk_live_{secrets.token_hex(32)}"
    api_key_hash = _hash_api_key(raw_api_key)

    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cursor.execute(
                """
                INSERT INTO merchant_partners (
                    company_name, legal_name, domain, website_url,
                    primary_email, primary_phone, gst_number, pan_number,
                    plan_type, api_key_hash, onboarding_status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
                RETURNING *;
                """,
                (
                    payload.company_name, payload.legal_name, payload.domain,
                    payload.website_url, payload.primary_email, payload.primary_phone,
                    payload.gst_number, payload.pan_number, payload.plan_type,
                    api_key_hash,
                ),
            )
            partner = cursor.fetchone()
            conn.commit()

            # Send welcome email (async)
            from app.tasks import send_partner_welcome_email
            send_partner_welcome_email.delay(partner["primary_email"], partner["company_name"])

            response = dict(partner)
            response["api_key"] = raw_api_key  # Show ONCE
            return response

        except Exception as e:
            conn.rollback()
            if "unique" in str(e).lower():
                raise HTTPException(status_code=409, detail="Domain already registered")
            raise HTTPException(status_code=500, detail=f"Registration failed: {e}")


@router.get("/profile", response_model=PartnerResponseSchema)
def get_partner_profile(partner: dict = Depends(get_partner_from_api_key)):
    """Get current partner profile (no API key in response)."""
    partner.pop("api_key_hash", None)
    return partner


@router.patch("/profile")
def update_partner_profile(
    webhook_url: Optional[str] = None,
    partner: dict = Depends(get_partner_from_api_key),
):
    """Update partner settings (webhook URL, etc.)."""
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE merchant_partners
            SET webhook_url = COALESCE(%s, webhook_url),
                updated_at = NOW()
            WHERE id = %s::uuid;
            """,
            (webhook_url, partner["id"]),
        )
        conn.commit()
    return {"status": "success", "message": "Profile updated"}


@router.post("/feed", response_model=FeedSubmissionResponseSchema)
def submit_partner_feed(
    payload: PartnerFeedSchema,
    request: Request,
    partner: dict = Depends(get_partner_from_api_key),
):
    """
    Submit product feed. Supports full, delta, price_update, stock_update.

    Rate limits by plan:
      - Free: 1 feed/day, max 100 items
      - Basic: 4 feeds/day, max 1,000 items
      - Premium: Unlimited feeds, max 10,000 items
      - Enterprise: Unlimited
    """
    # Rate limit check
    plan_limits = {"free": (1, 100), "basic": (4, 1000), "premium": (9999, 10000), "enterprise": (9999, 10000)}
    max_feeds, max_items = plan_limits.get(partner["plan_type"], (1, 100))

    if len(payload.items) > max_items:
        raise HTTPException(
            status_code=429,
            detail=f"Plan limit: max {max_items} items per feed. Upgrade at partners@yourdomain.com"
        )

    # Check daily feed count
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COUNT(*) FROM merchant_feed_submissions
            WHERE partner_id = %s::uuid AND created_at >= NOW() - INTERVAL '1 day';
            """,
            (partner["id"],),
        )
        daily_count = cursor.fetchone()[0]

    if daily_count >= max_feeds:
        raise HTTPException(
            status_code=429,
            detail=f"Daily feed limit ({max_feeds}) reached. Upgrade your plan or try tomorrow."
        )

    # Create submission record
    submission_id = None
    errors = []
    accepted = 0
    rejected = 0

    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cursor.execute(
                """
                INSERT INTO merchant_feed_submissions (
                    partner_id, feed_type, items_count, status, source_ip, user_agent
                ) VALUES (%s::uuid, %s, %s, 'processing', %s::inet, %s)
                RETURNING id;
                """,
                (partner["id"], payload.feed_type, len(payload.items),
                 request.client.host, request.headers.get("user-agent", "")),
            )
            submission_id = cursor.fetchone()["id"]

            # Process each item
            for idx, item in enumerate(payload.items):
                try:
                    # Upsert vendor (create if not exists)
                    cursor.execute(
                        """
                        INSERT INTO vendors (name, domain, is_active, scraping_allowed, data_source)
                        VALUES (%s, %s, TRUE, FALSE, 'merchant_partner')
                        ON CONFLICT (domain) DO UPDATE SET
                            name = EXCLUDED.name,
                            is_active = TRUE,
                            updated_at = NOW()
                        RETURNING id;
                        """,
                        (partner["company_name"], partner["domain"]),
                    )
                    vendor_id = cursor.fetchone()["id"]

                    # Resolve or create product
                    from app.core.matcher import get_embedding, extract_specifications
                    embedding = get_embedding(item.product_name)
                    specs = extract_specifications(item.product_name)
                    if item.specifications:
                        specs.update(item.specifications)

                    cursor.execute(
                        """
                        INSERT INTO products (title, brand, model_code, specifications, title_embedding)
                        VALUES (%s, %s, %s, %s, %s::vector)
                        ON CONFLICT (title) DO UPDATE SET
                            brand = COALESCE(EXCLUDED.brand, products.brand),
                            model_code = COALESCE(EXCLUDED.model_code, products.model_code)
                        RETURNING id;
                        """,
                        (item.product_name, item.brand, item.model_code,
                         json.dumps(specs) if specs else None, embedding),
                    )
                    product_id = cursor.fetchone()["id"]

                    # Upsert vendor offer
                    cursor.execute(
                        """
                        INSERT INTO vendor_offers (
                            product_id, vendor_id, vendor_product_id, raw_title,
                            product_url, current_price, currency, in_stock,
                            match_status, confidence_score, last_scraped_at,
                            data_source, verification_score, data_provenance, expires_at
                        ) VALUES (
                            %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s,
                            'matched', 0.95, NOW(),
                            'merchant_partner', 90,
                            %s, %s
                        )
                        ON CONFLICT (vendor_id, vendor_product_id) DO UPDATE SET
                            current_price = EXCLUDED.current_price,
                            in_stock = EXCLUDED.in_stock,
                            last_scraped_at = NOW(),
                            verification_score = 90,
                            expires_at = EXCLUDED.expires_at;
                        """,
                        (
                            product_id, vendor_id, item.vendor_product_id, item.product_name,
                            item.offer_url, item.price, item.currency, item.in_stock,
                            json.dumps({"partner_id": str(partner["id"]), "feed_type": payload.feed_type}),
                            item.offer_valid_until,
                        ),
                    )
                    accepted += 1

                except Exception as e:
                    rejected += 1
                    errors.append({"index": idx, "product": item.vendor_product_id, "error": str(e)})

            # Update submission record
            cursor.execute(
                """
                UPDATE merchant_feed_submissions
                SET status = 'completed',
                    items_accepted = %s,
                    items_rejected = %s,
                    processing_completed_at = NOW(),
                    error_message = %s
                WHERE id = %s::uuid;
                """,
                (accepted, rejected, json.dumps(errors) if errors else None, submission_id),
            )

            # Update partner last_feed_received_at
            cursor.execute(
                "UPDATE merchant_partners SET last_feed_received_at = NOW() WHERE id = %s::uuid;",
                (partner["id"],),
            )

            conn.commit()
            PARTNER_FEED_COUNTER.inc()

        except Exception as e:
            conn.rollback()
            if submission_id:
                cursor.execute(
                    "UPDATE merchant_feed_submissions SET status = 'failed', error_message = %s WHERE id = %s::uuid;",
                    (str(e), submission_id),
                )
                conn.commit()
            raise HTTPException(status_code=500, detail=f"Feed processing failed: {e}")

    return FeedSubmissionResponseSchema(
        submission_id=str(submission_id),
        status="completed" if not errors else "completed_with_errors",
        items_received=len(payload.items),
        items_accepted=accepted,
        items_rejected=rejected,
        errors=errors,
    )


@router.get("/feed/history")
def get_feed_history(
    limit: int = 20,
    partner: dict = Depends(get_partner_from_api_key),
):
    """Get submission history for this partner."""
    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT id, feed_type, items_count, items_accepted, items_rejected,
                   status, created_at, processing_completed_at
            FROM merchant_feed_submissions
            WHERE partner_id = %s::uuid
            ORDER BY created_at DESC
            LIMIT %s;
            """,
            (partner["id"], limit),
        )
        return {"status": "success", "data": cursor.fetchall()}


@router.get("/feed/{submission_id}")
def get_feed_details(
    submission_id: str,
    partner: dict = Depends(get_partner_from_api_key),
):
    """Get detailed status of a specific feed submission."""
    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT * FROM merchant_feed_submissions
            WHERE id = %s::uuid AND partner_id = %s::uuid;
            """,
            (submission_id, partner["id"]),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Submission not found")
        return {"status": "success", "data": dict(row)}


# ==========================================
# Admin endpoints for partner management
# ==========================================

from app.api.admin import require_admin

@router.get("/admin/list", dependencies=[Depends(require_admin)])
def admin_list_partners(status: Optional[str] = None):
    """Admin: list all merchant partners."""
    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        query = "SELECT * FROM merchant_partners"
        params = []
        if status:
            query += " WHERE onboarding_status = %s"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT 500;"
        cursor.execute(query, params)
        return {"status": "success", "count": cursor.rowcount, "data": cursor.fetchall()}


@router.post("/admin/{partner_id}/approve", dependencies=[Depends(require_admin)])
def admin_approve_partner(partner_id: str):
    """Admin: approve partner after KYC verification."""
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE merchant_partners
            SET onboarding_status = 'approved',
                kyc_verified = TRUE,
                kyc_verified_at = NOW(),
                updated_at = NOW()
            WHERE id = %s::uuid;
            """,
            (partner_id,),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Partner not found")
        conn.commit()
    return {"status": "success", "message": "Partner approved"}


@router.post("/admin/{partner_id}/suspend", dependencies=[Depends(require_admin)])
def admin_suspend_partner(partner_id: str, reason: str):
    """Admin: suspend partner (e.g., for ToS violation)."""
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE merchant_partners
            SET is_active = FALSE,
                onboarding_status = 'suspended',
                updated_at = NOW()
            WHERE id = %s::uuid;
            """,
            (partner_id,),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Partner not found")
        conn.commit()
    return {"status": "success", "message": f"Partner suspended: {reason}"}
