"""
PCE AutoBuy Agent — Autonomous Purchasing Orchestrator
LangGraph state machine with credit gate, payment pre-auth, and order placement.
"""
import logging
from datetime import datetime, timedelta
from typing import TypedDict, Optional, Dict, Any, Literal
from enum import Enum

from celery import Task
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.config import settings
from app.database import SessionLocal

logger = logging.getLogger(__name__)

# ==============================================================================
# Enums & Constants
# ==============================================================================

class AutoBuyStatus(str, Enum):
    INITIATED = "initiated"
    CREDIT_CHECK_PASSED = "credit_check_passed"
    CREDIT_CHECK_FAILED = "credit_check_failed"
    PAYMENT_AUTHORIZED = "payment_authorized"
    PAYMENT_FAILED = "payment_failed"
    ORDER_PLACED = "order_placed"
    VENDOR_ERROR = "vendor_error"
    SUCCESS = "success"
    CANCELLED = "cancelled"
    RETRY_SCHEDULED = "retry_scheduled"

MAX_RETRIES = 3
RETRY_DELAYS = [120, 300, 600]  # 2min, 5min, 10min
PRICE_TOLERANCE_PCT = 5.0  # Abort if price rises >5% from trigger

# ==============================================================================
# LangGraph State
# ==============================================================================

class AutoBuyState(TypedDict):
    rule_id: str
    user_id: str
    product_id: str
    offer_id: str
    trigger_type: Literal["price_drop", "restock", "scheduled", "manual"]
    trigger_price: float

    # Fetched data
    rule: Optional[Dict]
    user_profile: Optional[Dict]
    credit_profile: Optional[Dict]
    offer: Optional[Dict]
    payment_method: Optional[Dict]
    address: Optional[Dict]

    # Gate results
    credit_check_passed: Optional[bool]
    credit_check_reason: Optional[str]

    # Payment
    payment_authorized: Optional[bool]
    payment_txn_id: Optional[str]

    # Order
    order_placed: Optional[bool]
    vendor_order_id: Optional[str]
    purchase_order_id: Optional[str]

    # Error handling
    error_code: Optional[str]
    error_message: Optional[str]
    retry_count: int

    # Final
    status: str
    completed_at: Optional[datetime]

# ==============================================================================
# Main AutoBuy Task
# ==============================================================================

@celery_app.task(
    bind=True,
    name="app.tasks_autobuy.execute_auto_buy",
    max_retries=MAX_RETRIES,
    default_retry_delay=120,
    soft_time_limit=300,
    time_limit=600,
)
def execute_auto_buy(self: Task, rule_id: str) -> Dict[str, Any]:
    """
    Main AutoBuy execution entry point.
    Implements the full LangGraph workflow: trigger -> credit gate -> payment -> order.
    """
    logger.info(f"[AUTOBUY] Starting execution for rule {rule_id}")

    db = SessionLocal()
    state: AutoBuyState = {
        "rule_id": rule_id,
        "user_id": "",
        "product_id": "",
        "offer_id": "",
        "trigger_type": "price_drop",
        "trigger_price": 0.0,
        "rule": None,
        "user_profile": None,
        "credit_profile": None,
        "offer": None,
        "payment_method": None,
        "address": None,
        "credit_check_passed": None,
        "credit_check_reason": None,
        "payment_authorized": None,
        "payment_txn_id": None,
        "order_placed": None,
        "vendor_order_id": None,
        "purchase_order_id": None,
        "error_code": None,
        "error_message": None,
        "retry_count": 0,
        "status": AutoBuyStatus.INITIATED.value,
        "completed_at": None,
    }

    try:
        # ── NODE 1: Load Rule & Context ──
        state = _node_load_rule(state, db)
        if state["status"] != AutoBuyStatus.INITIATED.value:
            return _finalize(state, db)

        # ── NODE 2: Credit Gate ──
        state = _node_credit_gate(state, db)
        if state["status"] == AutoBuyStatus.CREDIT_CHECK_FAILED.value:
            return _finalize(state, db)

        # ── NODE 3: Verify Current Price ──
        state = _node_verify_price(state, db)
        if state["status"] != AutoBuyStatus.CREDIT_CHECK_PASSED.value:
            return _finalize(state, db)

        # ── NODE 4: Payment Pre-Authorization ──
        state = _node_payment_preauth(state, db)
        if state["status"] == AutoBuyStatus.PAYMENT_FAILED.value:
            return _finalize(state, db)

        # ── NODE 5: Place Order ──
        state = _node_place_order(state, db)
        if state["status"] in (AutoBuyStatus.VENDOR_ERROR.value, AutoBuyStatus.PAYMENT_FAILED.value):
            return _finalize(state, db)

        # ── NODE 6: Success ──
        state["status"] = AutoBuyStatus.SUCCESS.value
        state["completed_at"] = datetime.utcnow()

        return _finalize(state, db)

    except Exception as e:
        logger.exception(f"[AUTOBUY] Fatal error for rule {rule_id}: {e}")
        state["status"] = AutoBuyStatus.VENDOR_ERROR.value
        state["error_code"] = "UNEXPECTED_ERROR"
        state["error_message"] = str(e)
        return _finalize(state, db)
    finally:
        db.close()


# ==============================================================================
# LangGraph Nodes
# ==============================================================================

def _node_load_rule(state: AutoBuyState, db: Session) -> AutoBuyState:
    """Fetch the AutoBuy rule and associated data."""
    try:
        rule_row = db.execute(text("""
            SELECT abr.*, p.title as product_title
            FROM auto_buy_rules abr
            JOIN products p ON abr.product_id = p.id
            WHERE abr.id = :rule_id AND abr.is_active = TRUE
        """), {"rule_id": state["rule_id"]}).fetchone()

        if not rule_row:
            return _fail(state, "RULE_NOT_FOUND", "AutoBuy rule not found or inactive")

        state["rule"] = dict(rule_row._mapping)
        state["user_id"] = str(rule_row.user_id)
        state["product_id"] = str(rule_row.product_id)
        state["trigger_price"] = float(rule_row.trigger_price or 0)

        # Find best offer for this product
        offer_row = db.execute(text("""
            SELECT vo.*, v.name as vendor_name, v.domain, v.title_selector, v.price_selector
            FROM vendor_offers vo
            JOIN vendors v ON vo.vendor_id = v.id
            WHERE vo.product_id = :product_id
              AND vo.in_stock = TRUE
              AND (vo.merchant_id IS NULL OR vo.merchant_id = :preferred_vendor)
            ORDER BY vo.current_price ASC
            LIMIT 1
        """), {
            "product_id": state["product_id"],
            "preferred_vendor": rule_row.trigger_vendor_id or "",
        }).fetchone()

        if not offer_row:
            return _fail(state, "NO_OFFER", "No in-stock offer found for product")

        state["offer_id"] = str(offer_row.id)
        state["offer"] = dict(offer_row._mapping)
        state["trigger_price"] = float(offer_row.current_price)

        logger.info(f"[AUTOBUY] Rule loaded: user={state['user_id']}, product={state['product_id']}, price=₹{state['trigger_price']}")
        return state

    except Exception as e:
        return _fail(state, "LOAD_ERROR", str(e))


def _node_credit_gate(state: AutoBuyState, db: Session) -> AutoBuyState:
    """
    Creditworthiness gate — the "credit rating" check.
    All hard gates must pass before proceeding.
    """
    user_id = state["user_id"]
    offer_price = state["trigger_price"]

    try:
        # 1. Fetch credit profile
        credit = db.execute(text("""
            SELECT trust_score, auto_buy_eligible, is_flagged, kyc_boost_score,
                   payment_reliability_score, purchase_history_score
            FROM user_credit_profiles
            WHERE user_id = :user_id
        """), {"user_id": user_id}).fetchone()

        if not credit:
            return _fail_gate(state, "CREDIT_NO_PROFILE", "No credit profile. Run nightly batch first.")

        trust_score, eligible, flagged, kyc_boost = credit

        # 2. Hard gate: eligibility
        if not eligible:
            return _fail_gate(state, "CREDIT_NOT_ELIGIBLE",
                f"Trust score {trust_score}, eligible={eligible}. Minimum 650 + KYC required.")

        # 3. Hard gate: flagged
        if flagged:
            return _fail_gate(state, "CREDIT_FLAGGED", "Account is flagged for review")

        # 4. Fetch user profile limits
        user_prof = db.execute(text("""
            SELECT max_auto_buy_order_value, daily_auto_buy_limit, monthly_auto_buy_limit,
                   auto_buy_enabled
            FROM user_profiles
            WHERE user_id = :user_id
        """), {"user_id": user_id}).fetchone()

        if not user_prof or not user_prof.auto_buy_enabled:
            return _fail_gate(state, "AUTOBUY_DISABLED", "AutoBuy is disabled for this user")

        # 5. Order value limit
        if offer_price > user_prof.max_auto_buy_order_value:
            return _fail_gate(state, "LIMIT_ORDER_VALUE",
                f"₹{offer_price} exceeds max order value ₹{user_prof.max_auto_buy_order_value}")

        # 6. Daily limit
        today_spent = db.execute(text("""
            SELECT COALESCE(SUM(order_value), 0)
            FROM purchase_orders
            WHERE user_id = :user_id AND triggered_by = 'auto_buy_agent'
            AND placed_at >= CURRENT_DATE
        """), {"user_id": user_id}).scalar() or 0

        if today_spent + offer_price > user_prof.daily_auto_buy_limit:
            return _fail_gate(state, "LIMIT_DAILY",
                f"Daily limit ₹{user_prof.daily_auto_buy_limit} exceeded (today: ₹{today_spent})")

        # 7. Monthly limit
        month_spent = db.execute(text("""
            SELECT COALESCE(SUM(order_value), 0)
            FROM purchase_orders
            WHERE user_id = :user_id AND triggered_by = 'auto_buy_agent'
            AND placed_at >= DATE_TRUNC('month', CURRENT_DATE)
        """), {"user_id": user_id}).scalar() or 0

        if month_spent + offer_price > user_prof.monthly_auto_buy_limit:
            return _fail_gate(state, "LIMIT_MONTHLY",
                f"Monthly limit ₹{user_prof.monthly_auto_buy_limit} exceeded")

        # 8. Velocity check
        recent_orders = db.execute(text("""
            SELECT COUNT(*) FROM purchase_orders
            WHERE user_id = :user_id AND triggered_by = 'auto_buy_agent'
            AND placed_at >= NOW() - INTERVAL '24 hours'
        """), {"user_id": user_id}).scalar() or 0

        if recent_orders >= 5:
            return _fail_gate(state, "VELOCITY_LIMIT", "5+ AutoBuy orders in 24 hours")

        # 9. Consent check
        consent = db.execute(text("""
            SELECT consent_given FROM user_consents
            WHERE user_id = :user_id AND consent_type = 'auto_buy'
            ORDER BY created_at DESC LIMIT 1
        """), {"user_id": user_id}).fetchone()

        if not consent or not consent[0]:
            return _fail_gate(state, "NO_CONSENT", "User has not consented to AutoBuy")

        # 10. Payment method check
        pm = db.execute(text("""
            SELECT * FROM user_payment_methods
            WHERE (id = :preferred_pm OR is_default = TRUE)
              AND user_id = :user_id AND is_active = TRUE
            LIMIT 1
        """), {
            "preferred_pm": state["rule"].get("preferred_payment_method_id") or "",
            "user_id": user_id,
        }).fetchone()

        if not pm:
            return _fail_gate(state, "NO_PAYMENT_METHOD", "No active payment method on file")

        state["payment_method"] = dict(pm._mapping)

        # 11. Address check
        addr = db.execute(text("""
            SELECT * FROM user_addresses
            WHERE (id = :preferred_addr OR is_default = TRUE)
              AND user_id = :user_id
            LIMIT 1
        """), {
            "preferred_addr": state["rule"].get("preferred_address_id") or "",
            "user_id": user_id,
        }).fetchone()

        if not addr:
            return _fail_gate(state, "NO_ADDRESS", "No shipping address on file")

        state["address"] = dict(addr._mapping)

        # All gates passed
        state["credit_check_passed"] = True
        state["credit_check_reason"] = (
            f"Score {trust_score}, KYC={kyc_boost}, "
            f"daily=₹{today_spent}/{user_prof.daily_auto_buy_limit}, "
            f"orders_24h={recent_orders}"
        )
        state["credit_profile"] = {
            "trust_score": trust_score,
            "kyc_boost": kyc_boost,
            "payment_reliability": credit.payment_reliability_score,
            "purchase_history": credit.purchase_history_score,
        }
        state["status"] = AutoBuyStatus.CREDIT_CHECK_PASSED.value

        logger.info(f"[AUTOBUY] Credit gate PASSED for user {user_id}: {state['credit_check_reason']}")
        return state

    except Exception as e:
        return _fail_gate(state, "CREDIT_GATE_ERROR", str(e))


def _node_verify_price(state: AutoBuyState, db: Session) -> AutoBuyState:
    """Re-verify current price hasn't spiked since trigger detection."""
    try:
        current = db.execute(text("""
            SELECT current_price FROM vendor_offers WHERE id = :offer_id
        """), {"offer_id": state["offer_id"]}).scalar()

        if not current:
            return _fail(state, "PRICE_UNAVAILABLE", "Offer no longer exists")

        trigger = state["trigger_price"]
        if current > trigger * (1 + PRICE_TOLERANCE_PCT / 100):
            return _fail(state, "PRICE_SPIKE",
                f"Price spiked from ₹{trigger} to ₹{current} (> {PRICE_TOLERANCE_PCT}% tolerance)")

        # Update trigger price to current (might be lower — good for user)
        state["trigger_price"] = float(current)

        logger.info(f"[AUTOBUY] Price verified: ₹{current} (trigger was ₹{trigger})")
        return state

    except Exception as e:
        return _fail(state, "PRICE_VERIFY_ERROR", str(e))


def _node_payment_preauth(state: AutoBuyState, db: Session) -> AutoBuyState:
    """Pre-authorize payment with gateway. Hold funds, don't capture yet."""
    try:
        pm = state["payment_method"]
        amount = state["trigger_price"]

        # ── PAYMENT GATEWAY INTEGRATION (Razorpay example) ──
        # In production, replace with actual gateway SDK call
        # import razorpay
        # client = razorpay.Client(auth=(settings.RAZORPAY_KEY, settings.RAZORPAY_SECRET))
        # response = client.payment.create({
        #     "amount": int(amount * 100),  # paise
        #     "currency": "INR",
        #     "method": "card",
        #     "token": pm["gateway_token"],
        #     "customer_id": pm["gateway_customer_id"],
        #     "capture": False,
        # })

        # ── MOCK for development ──
        gateway_response = _mock_payment_auth(pm, amount)

        if gateway_response["status"] != "authorized":
            return _payment_fail(state, "AUTH_DECLINED",
                gateway_response.get("error_description", "Payment authorization declined"))

        state["payment_authorized"] = True
        state["payment_txn_id"] = gateway_response["id"]

        # Record transaction
        txn_result = db.execute(text("""
            INSERT INTO payment_transactions
            (user_id, payment_method_id, amount, currency, gateway,
             gateway_txn_id, gateway_status, status, created_at)
            VALUES (:user_id, :pm_id, :amount, 'INR', :gateway,
                    :txn_id, 'authorized', 'pending', NOW())
            RETURNING id
        """), {
            "user_id": state["user_id"],
            "pm_id": pm["id"],
            "amount": amount,
            "gateway": pm["gateway"],
            "txn_id": gateway_response["id"],
        }).fetchone()

        db.commit()

        logger.info(f"[AUTOBUY] Payment authorized: txn={gateway_response['id']}, amount=₹{amount}")
        return state

    except Exception as e:
        db.rollback()
        return _payment_fail(state, "PAYMENT_ERROR", str(e))


def _node_place_order(state: AutoBuyState, db: Session) -> AutoBuyState:
    """Place order with vendor. This is vendor-specific."""
    try:
        offer = state["offer"]
        addr = state["address"]

        # ── VENDOR ORDER API INTEGRATION ──
        # Each vendor has a different API. This is a generic placeholder.
        # In production, implement per-vendor adapters:
        # - Amazon Product Advertising API
        # - Flipkart Affiliate API
        # - Direct merchant APIs via Partner Feed

        vendor_response = _mock_vendor_order(offer, addr, state["payment_txn_id"])

        if vendor_response["status"] != "success":
            # Void payment authorization
            _void_payment(state["payment_txn_id"])
            return _order_fail(state, "VENDOR_REJECTED",
                vendor_response.get("error", "Vendor rejected order"))

        state["order_placed"] = True
        state["vendor_order_id"] = vendor_response["order_id"]

        # Create purchase order
        order_result = db.execute(text("""
            INSERT INTO purchase_orders
            (user_id, offer_id, product_id, vendor_id, order_value, currency,
             quantity, status, payment_method_id, payment_gateway,
             payment_gateway_txn_id, vendor_order_id, triggered_by,
             auto_buy_rule_id, placed_at, created_at)
            VALUES (:user_id, :offer_id, :product_id, :vendor_id, :value, 'INR',
                    :qty, 'order_placed', :pm_id, :gateway,
                    :txn_id, :vendor_order_id, 'auto_buy_agent',
                    :rule_id, NOW(), NOW())
            RETURNING id
        """), {
            "user_id": state["user_id"],
            "offer_id": state["offer_id"],
            "product_id": state["product_id"],
            "vendor_id": offer["vendor_id"],
            "value": state["trigger_price"],
            "qty": state["rule"].get("max_quantity", 1),
            "pm_id": state["payment_method"]["id"],
            "gateway": state["payment_method"]["gateway"],
            "txn_id": state["payment_txn_id"],
            "vendor_order_id": vendor_response["order_id"],
            "rule_id": state["rule_id"],
        }).fetchone()

        state["purchase_order_id"] = str(order_result[0])

        # Update payment transaction with order_id and capture
        db.execute(text("""
            UPDATE payment_transactions
            SET order_id = :order_id, status = 'success', gateway_status = 'captured'
            WHERE gateway_txn_id = :txn_id
        """), {
            "order_id": state["purchase_order_id"],
            "txn_id": state["payment_txn_id"],
        })

        # Update offer click count (for analytics)
        db.execute(text("""
            UPDATE vendor_offers
            SET click_count = click_count + 1, updated_at = NOW()
            WHERE id = :offer_id
        """), {"offer_id": state["offer_id"]})

        db.commit()

        # Trigger real-time credit score update
        from app.tasks_credit_scoring import update_user_credit_score
        update_user_credit_score.delay(state["user_id"])

        logger.info(
            f"[AUTOBUY] Order placed: purchase_order={state['purchase_order_id']}, "
            f"vendor_order={vendor_response['order_id']}"
        )
        return state

    except Exception as e:
        db.rollback()
        _void_payment(state.get("payment_txn_id"))
        return _order_fail(state, "ORDER_EXCEPTION", str(e))


# ==============================================================================
# Helper Functions
# ==============================================================================

def _fail(state: AutoBuyState, code: str, message: str) -> AutoBuyState:
    state["error_code"] = code
    state["error_message"] = message
    state["status"] = AutoBuyStatus.VENDOR_ERROR.value
    state["completed_at"] = datetime.utcnow()
    logger.warning(f"[AUTOBUY] Failed [{code}]: {message}")
    return state


def _fail_gate(state: AutoBuyState, code: str, message: str) -> AutoBuyState:
    state["credit_check_passed"] = False
    state["credit_check_reason"] = message
    state["error_code"] = code
    state["error_message"] = message
    state["status"] = AutoBuyStatus.CREDIT_CHECK_FAILED.value
    state["completed_at"] = datetime.utcnow()
    logger.warning(f"[AUTOBUY] Credit gate failed [{code}]: {message}")
    return state


def _payment_fail(state: AutoBuyState, code: str, message: str) -> AutoBuyState:
    state["payment_authorized"] = False
    state["error_code"] = code
    state["error_message"] = message
    state["status"] = AutoBuyStatus.PAYMENT_FAILED.value
    state["completed_at"] = datetime.utcnow()
    logger.warning(f"[AUTOBUY] Payment failed [{code}]: {message}")
    return state


def _order_fail(state: AutoBuyState, code: str, message: str) -> AutoBuyState:
    state["order_placed"] = False
    state["error_code"] = code
    state["error_message"] = message
    state["status"] = AutoBuyStatus.VENDOR_ERROR.value
    state["completed_at"] = datetime.utcnow()
    logger.warning(f"[AUTOBUY] Order failed [{code}]: {message}")
    return state


def _finalize(state: AutoBuyState, db: Session) -> Dict[str, Any]:
    """Persist execution log and return result."""
    try:
        db.execute(text("""
            INSERT INTO auto_buy_executions
            (rule_id, user_id, product_id, offer_id, trigger_type, trigger_price,
             status, trust_score_at_execution, credit_check_passed,
             credit_check_reason, payment_method_id, payment_txn_id,
             order_id, vendor_order_id, error_code, error_message,
             retry_count, created_at, completed_at)
            VALUES (:rule_id, :user_id, :product_id, :offer_id, :trigger_type, :trigger_price,
                    :status, :trust_score, :credit_passed, :credit_reason,
                    :pm_id, :txn_id, :order_id, :vendor_order_id,
                    :error_code, :error_message, :retry_count, NOW(), :completed_at)
        """), {
            "rule_id": state["rule_id"],
            "user_id": state["user_id"],
            "product_id": state["product_id"],
            "offer_id": state["offer_id"] or None,
            "trigger_type": state["trigger_type"],
            "trigger_price": state["trigger_price"],
            "status": state["status"],
            "trust_score": state.get("credit_profile", {}).get("trust_score"),
            "credit_passed": state["credit_check_passed"],
            "credit_reason": state["credit_check_reason"],
            "pm_id": state["payment_method"]["id"] if state["payment_method"] else None,
            "txn_id": state["payment_txn_id"],
            "order_id": state["purchase_order_id"],
            "vendor_order_id": state["vendor_order_id"],
            "error_code": state["error_code"],
            "error_message": state["error_message"],
            "retry_count": state["retry_count"],
            "completed_at": state["completed_at"],
        })
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"[AUTOBUY] Failed to log execution: {e}")

    result = {
        "rule_id": state["rule_id"],
        "status": state["status"],
        "user_id": state["user_id"],
        "product_id": state["product_id"],
        "order_id": state["purchase_order_id"],
        "vendor_order_id": state["vendor_order_id"],
        "payment_authorized": state["payment_authorized"],
        "credit_check_passed": state["credit_check_passed"],
        "error_code": state["error_code"],
        "error_message": state["error_message"],
        "completed_at": state["completed_at"].isoformat() if state["completed_at"] else None,
    }

    logger.info(f"[AUTOBUY] Finalized: status={state['status']}, order={state.get('purchase_order_id')}")
    return result


def _void_payment(txn_id: Optional[str]) -> None:
    """Void/cancel a payment authorization."""
    if not txn_id:
        return
    logger.info(f"[AUTOBUY] Voiding payment authorization: {txn_id}")
    # TODO: Integrate with payment gateway
    # razorpay_client.payment.fetch(txn_id).refund({"amount": ...})


def _mock_payment_auth(pm: Dict, amount: float) -> Dict:
    """Mock payment gateway for development."""
    import uuid
    return {
        "id": f"pay_{uuid.uuid4().hex[:16]}",
        "status": "authorized",
        "amount": int(amount * 100),
        "currency": "INR",
        "method": pm.get("method_type", "card"),
    }


def _mock_vendor_order(offer: Dict, addr: Dict, payment_token: str) -> Dict:
    """Mock vendor order API for development."""
    import uuid
    return {
        "status": "success",
        "order_id": f"VO_{uuid.uuid4().hex[:12].upper()}",
        "vendor": offer.get("vendor_name", "Unknown"),
        "estimated_delivery": "3-5 business days",
    }


# ==============================================================================
# Trigger Scanner (Celery Beat scheduled task)
# ==============================================================================

@celery_app.task(
    name="app.tasks_autobuy.scan_auto_buy_triggers",
    soft_time_limit=120,
    time_limit=300,
)
def scan_auto_buy_triggers() -> Dict[str, Any]:
    """
    Scans all active AutoBuy rules and queues execution for triggered ones.
    Runs every 5 minutes via Celery Beat.
    """
    db = SessionLocal()
    triggered = []

    try:
        # Find rules where current price <= trigger_price
        rows = db.execute(text("""
            SELECT 
                abr.id as rule_id,
                abr.user_id,
                abr.product_id,
                abr.trigger_price,
                abr.trigger_drop_pct,
                vo.id as offer_id,
                vo.current_price,
                vo.in_stock,
                p.title as product_title
            FROM auto_buy_rules abr
            JOIN products p ON abr.product_id = p.id
            JOIN vendor_offers vo ON abr.product_id = vo.product_id
            WHERE abr.is_active = TRUE
              AND (abr.expiry_date IS NULL OR abr.expiry_date >= CURRENT_DATE)
              AND vo.in_stock = TRUE
              AND (
                  (abr.trigger_price IS NOT NULL AND vo.current_price <= abr.trigger_price)
                  OR abr.trigger_drop_pct IS NOT NULL
              )
            ORDER BY abr.created_at
        """)).fetchall()

        for row in rows:
            # Additional check: if trigger_drop_pct is set, verify actual drop
            if row.trigger_drop_pct is not None:
                prev_price = db.execute(text("""
                    SELECT price FROM price_history
                    WHERE offer_id = :offer_id
                    ORDER BY recorded_at DESC LIMIT 1 OFFSET 1
                """), {"offer_id": row.offer_id}).scalar()

                if prev_price and prev_price > 0:
                    drop_pct = ((prev_price - row.current_price) / prev_price) * 100
                    if drop_pct < row.trigger_drop_pct:
                        continue  # Drop not deep enough

            # Queue AutoBuy execution
            execute_auto_buy.delay(str(row.rule_id))
            triggered.append({
                "rule_id": str(row.rule_id),
                "user_id": str(row.user_id),
                "product": row.product_title,
                "price": float(row.current_price),
            })

            # Update trigger count
            db.execute(text("""
                UPDATE auto_buy_rules
                SET times_triggered = times_triggered + 1,
                    last_triggered_at = NOW()
                WHERE id = :id
            """), {"id": row.rule_id})

        db.commit()

        logger.info(f"[AUTOBUY_SCAN] Triggered {len(triggered)} rules")
        return {
            "status": "success",
            "triggered_count": len(triggered),
            "triggered": triggered,
        }

    except Exception as e:
        db.rollback()
        logger.exception(f"[AUTOBUY_SCAN] Error: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        db.close()


# ==============================================================================
# Retry Handler
# ==============================================================================

@celery_app.task(
    bind=True,
    name="app.tasks_autobuy.retry_auto_buy",
    max_retries=MAX_RETRIES,
)
def retry_auto_buy(self: Task, rule_id: str, previous_state: Dict) -> Dict[str, Any]:
    """
    Retry a failed AutoBuy execution with exponential backoff.
    """
    retry_count = previous_state.get("retry_count", 0) + 1

    if retry_count > MAX_RETRIES:
        logger.error(f"[AUTOBUY_RETRY] Max retries exceeded for rule {rule_id}")
        return {"status": "max_retries_exceeded", "rule_id": rule_id}

    delay = RETRY_DELAYS[min(retry_count - 1, len(RETRY_DELAYS) - 1)]

    logger.info(f"[AUTOBUY_RETRY] Scheduling retry {retry_count}/{MAX_RETRIES} for rule {rule_id} in {delay}s")

    # Re-queue with countdown
    execute_auto_buy.apply_async(args=[rule_id], countdown=delay)

    return {
        "status": "retry_scheduled",
        "rule_id": rule_id,
        "retry_count": retry_count,
        "next_attempt_in_seconds": delay,
    }
