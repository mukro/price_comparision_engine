# app/api/ocr_submission.py
"""
User OCR Submission API — Designed for on-device OCR mobile apps.

Privacy-First Design:
  - Mobile app does ALL OCR processing locally
  - Server receives ONLY structured text data (price, product name, vendor)
  - NO raw images, NO GPS coordinates, NO device IDs transmitted
  - One-way hashed device identifier for reputation tracking
  - Geo-fenced to approximate location (geohash, not lat/lng)

Flow:
  1. User opens seller app/website, sees product price
  2. User takes screenshot (stays on device)
  3. Mobile app runs OCR locally (ML Kit / Tesseract / CoreML)
  4. App extracts: price, product name, vendor, stock status
  5. App sends structured JSON to this endpoint
  6. Server validates, assigns verification score, stores in user_submissions
  7. After community validation, merged into vendor_offers
"""
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Header, Request, status
from pydantic import BaseModel, Field, validator
from psycopg2.extras import RealDictCursor

from app.config import settings
from app.db_sync import get_conn
from app.core.compliance import get_domain_from_url

logger = logging.getLogger("ocr_submission")

router = APIRouter(prefix="/api/v1/submissions", tags=["User OCR Submissions"])


# ==========================================
# Schemas
# ==========================================

class OCRSubmissionSchema(BaseModel):
    """
    Structured data from mobile app on-device OCR.
    NO images, NO GPS, NO PII.
    """
    # Extracted price data
    price: float = Field(..., gt=0, description="Extracted price value")
    currency: str = Field(default="INR", max_length=10, description="Currency code")

    # Product identification
    product_name: str = Field(..., min_length=2, max_length=500, description="Product name from OCR")
    brand: Optional[str] = Field(None, max_length=100, description="Brand name if visible")

    # Vendor identification
    vendor_domain: str = Field(..., max_length=255, description="Domain of seller (e.g., amazon.in)")
    vendor_app_name: Optional[str] = Field(None, max_length=100, description="App name if different from domain")

    # Stock status
    in_stock: Optional[bool] = Field(None, description="True if 'in stock' detected, False if 'out of stock'")
    stock_text: Optional[str] = Field(None, max_length=100, description="Raw stock text from OCR")

    # Offer details
    offer_url: Optional[str] = Field(None, max_length=1000, description="Deep link or URL if available")
    mrp_price: Optional[float] = Field(None, gt=0, description="Maximum retail price if shown")
    discount_percent: Optional[float] = Field(None, ge=0, le=100, description="Discount percentage if shown")

    # OCR metadata (from mobile app)
    ocr_confidence: float = Field(..., ge=0.0, le=1.0, description="ML model confidence score")
    ocr_engine: Optional[str] = Field(None, max_length=50, description="OCR engine used: tesseract, mlkit, coreml")

    # Privacy-preserving location
    geo_hash: Optional[str] = Field(None, max_length=12, description="Geohash of location (precision ~5km)")

    # Device (anonymized)
    device_hash: str = Field(..., min_length=32, max_length=64, description="SHA-256 hash of device_id (NOT raw device_id)")
    device_os: Optional[str] = Field(None, max_length=20, description="iOS or Android")
    app_version: Optional[str] = Field(None, max_length=20, description="Mobile app version")

    # Screenshot deduplication (optional)
    screenshot_hash: Optional[str] = Field(None, max_length=64, description="SHA-256 of screenshot pixels (for dedup)")

    # Timestamp from device
    captured_at: Optional[datetime] = Field(None, description="When screenshot was taken (device time)")

    @validator('vendor_domain')
    def normalize_domain(cls, v):
        v = v.lower().strip()
        if v.startswith('www.'):
            v = v[4:]
        if v.startswith('https://'):
            v = v[8:]
        if v.startswith('http://'):
            v = v[7:]
        if '/' in v:
            v = v.split('/')[0]
        return v

    @validator('geo_hash')
    def validate_geohash(cls, v):
        if v and len(v) < 4:
            raise ValueError("Geohash must be at least 4 characters (city-level precision)")
        return v


class OCRSubmissionResponseSchema(BaseModel):
    submission_id: str
    status: str
    verification_score: int
    message: str
    estimated_approval_time: str = "Within 24 hours"


class OCRValidationResultSchema(BaseModel):
    submission_id: str
    status: str
    verification_score: int
    community_confirmations: int
    community_rejections: int
    merged_into_offer_id: Optional[str] = None


# ==========================================
# Verification Logic
# ==========================================

def _calculate_verification_score(submission: OCRSubmissionSchema) -> int:
    """
    Calculate initial verification score based on submission quality.
    Higher score = faster approval.
    """
    score = 0

    # OCR confidence (0-40 points)
    score += int(submission.ocr_confidence * 40)

    # Completeness (0-20 points)
    if submission.brand:
        score += 5
    if submission.in_stock is not None:
        score += 5
    if submission.offer_url:
        score += 5
    if submission.mrp_price:
        score += 5

    # Device reputation (0-20 points) — checked against historical accuracy
    score += _get_device_reputation(submission.device_hash)

    # Geo consistency (0-10 points)
    if submission.geo_hash:
        score += 10

    # Screenshot dedup (0-10 points)
    if submission.screenshot_hash:
        score += 10

    return min(score, 100)


def _get_device_reputation(device_hash: str) -> int:
    """Check historical accuracy of this device."""
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT 
                COUNT(*) FILTER (WHERE status = 'approved') as approved,
                COUNT(*) FILTER (WHERE status = 'rejected') as rejected
            FROM user_submissions
            WHERE device_hash = %s;
            """,
            (device_hash,),
        )
        row = cursor.fetchone()
        approved = row[0] or 0
        rejected = row[1] or 0
        total = approved + rejected

        if total < 5:
            return 5  # New contributor
        accuracy = approved / total
        if accuracy >= 0.95:
            return 20  # Trusted contributor
        elif accuracy >= 0.80:
            return 10
        elif accuracy >= 0.60:
            return 5
        else:
            return 0  # Low reputation


def _check_duplicate_screenshot(screenshot_hash: str, vendor_domain: str) -> bool:
    """Check if this exact screenshot was already submitted."""
    if not screenshot_hash:
        return False
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT 1 FROM user_submissions
            WHERE screenshot_hash = %s AND vendor_domain = %s
            AND submitted_at > NOW() - INTERVAL '24 hours'
            LIMIT 1;
            """,
            (screenshot_hash, vendor_domain),
        )
        return cursor.fetchone() is not None


def _check_price_outlier(price: float, vendor_domain: str, product_name: str) -> bool:
    """Check if price is suspiciously different from recent submissions."""
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT AVG(extracted_price) as avg_price, STDDEV(extracted_price) as stddev
            FROM user_submissions
            WHERE vendor_domain = %s
              AND extracted_product_name %s
              AND status = 'approved'
              AND submitted_at > NOW() - INTERVAL '7 days';
            """,
            (vendor_domain, product_name),
        )
        row = cursor.fetchone()
        if not row or row[0] is None:
            return False  # No historical data

        avg_price = float(row[0])
        stddev = float(row[1]) if row[1] else avg_price * 0.1

        # Flag if > 3 standard deviations from mean
        if abs(price - avg_price) > (3 * stddev):
            return True
    return False


# ==========================================
# Endpoints
# ==========================================

@router.post("/ocr", response_model=OCRSubmissionResponseSchema, status_code=status.HTTP_201_CREATED)
def submit_ocr_data(payload: OCRSubmissionSchema, request: Request):
    """
    Submit price data extracted via on-device OCR.

    Privacy guarantee: This endpoint accepts ONLY structured text data.
    Raw screenshots NEVER leave the user's device.
    """
    # 1. Check for duplicate screenshot
    if _check_duplicate_screenshot(payload.screenshot_hash, payload.vendor_domain):
        raise HTTPException(
            status_code=409,
            detail="This screenshot appears to have been submitted recently. Please verify the price has changed."
        )

    # 2. Check for price outlier (potential gaming)
    is_outlier = _check_price_outlier(payload.price, payload.vendor_domain, payload.product_name)

    # 3. Calculate verification score
    verification_score = _calculate_verification_score(payload)

    # Reduce score for outliers
    if is_outlier:
        verification_score = max(0, verification_score - 30)

    # 4. Auto-approve high-confidence submissions
    auto_approve = verification_score >= 85
    status_value = "approved" if auto_approve else "pending"

    # 5. Store submission
    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cursor.execute(
                """
                INSERT INTO user_submissions (
                    user_id, vendor_domain,
                    extracted_price, extracted_currency, extracted_product_name,
                    extracted_stock_status, geo_hash, submitted_at,
                    verification_score, device_os, app_version,
                    ocr_confidence, ocr_engine, screenshot_hash,
                    status, device_hash
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id;
                """,
                (
                    payload.device_hash,  # Use device_hash as anonymized user_id
                    payload.vendor_domain,
                    payload.price,
                    payload.currency,
                    payload.product_name,
                    payload.in_stock,
                    payload.geo_hash,
                    verification_score,
                    payload.device_os,
                    payload.app_version,
                    payload.ocr_confidence,
                    payload.ocr_engine,
                    payload.screenshot_hash,
                    status_value,
                    payload.device_hash,
                ),
            )
            submission_id = cursor.fetchone()["id"]

            # 6. If auto-approved, merge into vendor_offers
            if auto_approve:
                _merge_submission_to_offers(cursor, submission_id, payload)

            conn.commit()

            # 7. Trigger community validation for pending submissions
            if not auto_approve:
                from app.tasks import trigger_community_validation
                trigger_community_validation.delay(str(submission_id))

            return OCRSubmissionResponseSchema(
                submission_id=str(submission_id),
                status=status_value,
                verification_score=verification_score,
                message="Price submitted successfully!" + (" Auto-approved due to high confidence." if auto_approve else " Pending community validation."),
            )

        except Exception as e:
            conn.rollback()
            logger.error(f"OCR submission failed: {e}")
            raise HTTPException(status_code=500, detail="Submission processing failed")


def _merge_submission_to_offers(cursor, submission_id: str, payload: OCRSubmissionSchema):
    """Merge an approved OCR submission into the main vendor_offers table."""
    from app.core.matcher import get_embedding, extract_specifications

    # Resolve or create vendor
    cursor.execute(
        """
        INSERT INTO vendors (name, domain, is_active, scraping_allowed, data_source)
        VALUES (%s, %s, TRUE, FALSE, 'user_ocr')
        ON CONFLICT (domain) DO UPDATE SET
            name = EXCLUDED.name,
            is_active = TRUE,
            updated_at = NOW()
        RETURNING id;
        """,
        (payload.vendor_domain, payload.vendor_domain),
    )
    vendor_id = cursor.fetchone()["id"]

    # Resolve or create product
    embedding = get_embedding(payload.product_name)
    specs = extract_specifications(payload.product_name)

    cursor.execute(
        """
        INSERT INTO products (title, brand, specifications, title_embedding)
        VALUES (%s, %s, %s, %s::vector)
        ON CONFLICT (title) DO UPDATE SET
            brand = COALESCE(EXCLUDED.brand, products.brand)
        RETURNING id;
        """,
        (payload.product_name, payload.brand, json.dumps(specs) if specs else None, embedding),
    )
    product_id = cursor.fetchone()["id"]

    # Upsert vendor offer
    cursor.execute(
        """
        INSERT INTO vendor_offers (
            product_id, vendor_id, vendor_product_id, raw_title,
            product_url, current_price, currency, in_stock,
            match_status, confidence_score, last_scraped_at,
            data_source, verification_score, data_provenance,
            geo_hash, submitted_by_user_id
        ) VALUES (
            %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s,
            'matched', 0.85, NOW(),
            'user_ocr', %s,
            %s, %s, %s
        )
        ON CONFLICT (vendor_id, vendor_product_id) DO UPDATE SET
            current_price = EXCLUDED.current_price,
            in_stock = EXCLUDED.in_stock,
            last_scraped_at = NOW(),
            verification_score = EXCLUDED.verification_score,
            data_provenance = EXCLUDED.data_provenance;
        """,
        (
            product_id, vendor_id,
            f"user_ocr_{submission_id}",
            payload.product_name,
            payload.offer_url or f"https://{payload.vendor_domain}",
            payload.price, payload.currency, payload.in_stock,
            payload.verification_score,
            json.dumps({
                "submission_id": str(submission_id),
                "ocr_confidence": payload.ocr_confidence,
                "ocr_engine": payload.ocr_engine,
                "captured_at": payload.captured_at.isoformat() if payload.captured_at else None,
            }),
            payload.geo_hash,
            payload.device_hash,
        ),
    )

    # Update submission as merged
    cursor.execute(
        """
        UPDATE user_submissions
        SET status = 'merged',
            merged_offer_id = (SELECT id FROM vendor_offers WHERE vendor_id = %s::uuid AND product_id = %s::uuid LIMIT 1),
            merged_at = NOW()
        WHERE id = %s::uuid;
        """,
        (vendor_id, product_id, submission_id),
    )


@router.get("/{submission_id}/status", response_model=OCRValidationResultSchema)
def get_submission_status(submission_id: str):
    """Check validation status of a user submission."""
    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT 
                us.id, us.status, us.verification_score,
                us.merged_offer_id,
                COUNT(DISTINCT uv.upvote) FILTER (WHERE uv.upvote = TRUE) as confirmations,
                COUNT(DISTINCT uv.upvote) FILTER (WHERE uv.upvote = FALSE) as rejections
            FROM user_submissions us
            LEFT JOIN user_validation_votes uv ON uv.submission_id = us.id
            WHERE us.id = %s::uuid
            GROUP BY us.id;
            """,
            (submission_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Submission not found")

        return OCRValidationResultSchema(
            submission_id=str(row["id"]),
            status=row["status"],
            verification_score=row["verification_score"],
            community_confirmations=row["confirmations"] or 0,
            community_rejections=row["rejections"] or 0,
            merged_into_offer_id=str(row["merged_offer_id"]) if row["merged_offer_id"] else None,
        )


@router.get("/leaderboard")
def get_contributor_leaderboard(geo_hash: Optional[str] = None, limit: int = 50):
    """
    Public leaderboard of top contributors.
    Shows anonymized device hashes (first 8 chars only).
    """
    with get_conn() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        query = """
            SELECT 
                LEFT(device_hash, 8) as contributor_id,
                COUNT(*) as total_submissions,
                COUNT(*) FILTER (WHERE status = 'approved') as approved,
                COUNT(*) FILTER (WHERE status = 'rejected') as rejected,
                ROUND(
                    COUNT(*) FILTER (WHERE status = 'approved') * 100.0 / NULLIF(COUNT(*), 0),
                    1
                ) as accuracy_rate
            FROM user_submissions
        """
        params = []
        if geo_hash:
            query += " WHERE geo_hash LIKE %s || '%%'"
            params.append(geo_hash)
        query += """
            GROUP BY device_hash
            HAVING COUNT(*) >= 5
            ORDER BY accuracy_rate DESC, total_submissions DESC
            LIMIT %s;
        """
        params.append(limit)
        cursor.execute(query, params)
        return {"status": "success", "data": cursor.fetchall()}


# ==========================================
# Community Validation (voting)
# ==========================================

class ValidationVoteSchema(BaseModel):
    submission_id: str
    upvote: bool  # True = confirm price is correct, False = reject
    device_hash: str  # Voter's anonymized device hash


@router.post("/validate")
def submit_validation_vote(payload: ValidationVoteSchema):
    """
    Community validation: users confirm or reject price submissions.
    Prevents gaming by requiring voters to have submission history.
    """
    # Prevent self-voting
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM user_submissions WHERE id = %s::uuid AND device_hash = %s;",
            (payload.submission_id, payload.device_hash),
        )
        if cursor.fetchone():
            raise HTTPException(status_code=403, detail="Cannot validate your own submission")

        # Require voter to have at least 3 approved submissions
        cursor.execute(
            """
            SELECT COUNT(*) FROM user_submissions
            WHERE device_hash = %s AND status = 'approved';
            """,
            (payload.device_hash,),
        )
        voter_credibility = cursor.fetchone()[0]
        if voter_credibility < 3:
            raise HTTPException(
                status_code=403,
                detail="You need at least 3 approved submissions to validate others."
            )

        # Record vote
        cursor.execute(
            """
            INSERT INTO user_validation_votes (submission_id, device_hash, upvote, created_at)
            VALUES (%s::uuid, %s, %s, NOW())
            ON CONFLICT (submission_id, device_hash) DO UPDATE SET
                upvote = EXCLUDED.upvote,
                created_at = NOW();
            """,
            (payload.submission_id, payload.device_hash, payload.upvote),
        )

        # Check if submission has enough votes to auto-approve/reject
        cursor.execute(
            """
            SELECT 
                COUNT(*) FILTER (WHERE upvote = TRUE) as confirms,
                COUNT(*) FILTER (WHERE upvote = FALSE) as rejects
            FROM user_validation_votes
            WHERE submission_id = %s::uuid;
            """,
            (payload.submission_id,),
        )
        votes = cursor.fetchone()
        confirms = votes[0]
        rejects = votes[1]

        # Auto-approve if 5+ confirms and 80% confirmation rate
        if confirms >= 5 and confirms / (confirms + rejects) >= 0.8:
            cursor.execute(
                """
                UPDATE user_submissions
                SET status = 'approved', verification_score = LEAST(verification_score + 20, 100)
                WHERE id = %s::uuid AND status = 'pending';
                """,
                (payload.submission_id,),
            )
        # Auto-reject if 5+ rejects and 80% rejection rate
        elif rejects >= 5 and rejects / (confirms + rejects) >= 0.8:
            cursor.execute(
                """
                UPDATE user_submissions
                SET status = 'rejected', verification_score = GREATEST(verification_score - 30, 0)
                WHERE id = %s::uuid AND status = 'pending';
                """,
                (payload.submission_id,),
            )

        conn.commit()

    return {"status": "success", "message": "Vote recorded"}
