# app/tasks_credit_scoring.py
"""
Celery tasks for nightly user credit score recalculation.
Runs at 2:00 AM daily via Celery Beat.
"""
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any

from celery import shared_task, Task
from celery.exceptions import MaxRetriesExceededError, SoftTimeLimitExceeded
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.config import settings
from app.database import SessionLocal

logger = logging.getLogger(__name__)

# ==============================================================================
# Constants
# ==============================================================================

BATCH_SIZE = 500           # Users per batch to avoid long transactions
MAX_RETRIES = 3
RETRY_DELAY = 300          # 5 minutes
SOFT_TIME_LIMIT = 3600     # 1 hour per batch
HARD_TIME_LIMIT = 7200     # 2 hours absolute max

# Score thresholds for notifications
SCORE_DROP_THRESHOLD = 100  # Alert if score drops by 100+ points
SCORE_FLAG_THRESHOLD = 500  # Alert if score falls below 500


# ==============================================================================
# Main Batch Task
# ==============================================================================

@celery_app.task(
    bind=True,
    name="app.tasks_credit_scoring.recalculate_all_credit_scores",
    max_retries=MAX_RETRIES,
    default_retry_delay=RETRY_DELAY,
    soft_time_limit=SOFT_TIME_LIMIT,
    time_limit=HARD_TIME_LIMIT,
)
def recalculate_all_credit_scores(self: Task) -> Dict[str, Any]:
    """
    Nightly batch: recalculate trust scores for all active users.

    Process:
    1. Fetch all active user IDs in batches
    2. For each user, call the SQL recalculate function
    3. Track score changes and flag anomalies
    4. Send summary report

    Returns:
        {
            "status": "success",
            "total_users": 15000,
            "processed": 15000,
            "failed": 0,
            "score_drops": 23,
            "newly_flagged": 5,
            "duration_seconds": 420,
            "batches": 30
        }
    """
    start_time = datetime.utcnow()
    db: Session = SessionLocal()

    stats = {
        "status": "success",
        "total_users": 0,
        "processed": 0,
        "failed": 0,
        "score_drops": 0,
        "newly_flagged": 0,
        "score_increases": 0,
        "duration_seconds": 0,
        "batches": 0,
        "errors": [],
    }

    try:
        # Step 1: Get total count
        total_result = db.execute(text("""
            SELECT COUNT(*) as total FROM users WHERE is_active = TRUE
        """))
        stats["total_users"] = total_result.scalar()

        if stats["total_users"] == 0:
            logger.info("[CREDIT_BATCH] No active users to process.")
            return stats

        logger.info(f"[CREDIT_BATCH] Starting batch for {stats['total_users']} users")

        # Step 2: Get all user IDs
        user_ids_result = db.execute(text("""
            SELECT id FROM users WHERE is_active = TRUE ORDER BY created_at
        """))
        all_user_ids = [row[0] for row in user_ids_result]

        # Step 3: Process in batches
        total_batches = (len(all_user_ids) + BATCH_SIZE - 1) // BATCH_SIZE
        stats["batches"] = total_batches

        for batch_num in range(total_batches):
            batch_start = batch_num * BATCH_SIZE
            batch_end = min(batch_start + BATCH_SIZE, len(all_user_ids))
            batch_ids = all_user_ids[batch_start:batch_end]

            logger.info(f"[CREDIT_BATCH] Processing batch {batch_num + 1}/{total_batches} ({len(batch_ids)} users)")

            batch_stats = _process_batch(db, batch_ids)

            stats["processed"] += batch_stats["processed"]
            stats["failed"] += batch_stats["failed"]
            stats["score_drops"] += batch_stats["score_drops"]
            stats["newly_flagged"] += batch_stats["newly_flagged"]
            stats["score_increases"] += batch_stats["score_increases"]

            if batch_stats["errors"]:
                stats["errors"].extend(batch_stats["errors"])

            # Commit after each batch to avoid long-running transactions
            db.commit()

        # Step 4: Generate summary
        duration = (datetime.utcnow() - start_time).total_seconds()
        stats["duration_seconds"] = round(duration, 2)

        logger.info(
            f"[CREDIT_BATCH] Complete: {stats['processed']}/{stats['total_users']} users, "
            f"{stats['score_drops']} drops, {stats['newly_flagged']} flagged, "
            f"{stats['failed']} failed, {duration:.1f}s"
        )

        # Step 5: Send summary notification (optional)
        _send_batch_summary(stats)

        return stats

    except SoftTimeLimitExceeded:
        logger.error("[CREDIT_BATCH] Soft time limit exceeded. Partial results committed.")
        stats["status"] = "partial"
        db.commit()
        raise self.retry(countdown=RETRY_DELAY)

    except Exception as e:
        logger.exception(f"[CREDIT_BATCH] Fatal error: {e}")
        stats["status"] = "failed"
        stats["errors"].append(str(e))
        db.rollback()

        if self.request.retries < MAX_RETRIES:
            raise self.retry(exc=e, countdown=RETRY_DELAY * (self.request.retries + 1))
        raise MaxRetriesExceededError(f"Failed after {MAX_RETRIES} retries: {e}")

    finally:
        db.close()


def _process_batch(db: Session, user_ids: List[str]) -> Dict[str, int]:
    """
    Process a single batch of users.
    Returns batch statistics.
    """
    batch_stats = {
        "processed": 0,
        "failed": 0,
        "score_drops": 0,
        "newly_flagged": 0,
        "score_increases": 0,
        "errors": [],
    }

    # Get previous scores for comparison
    prev_scores = {}
    if user_ids:
        placeholders = ','.join([f"'{uid}'" for uid in user_ids])
        result = db.execute(text(f"""
            SELECT user_id, trust_score, is_flagged 
            FROM user_credit_profiles 
            WHERE user_id IN ({placeholders})
        """))
        for row in result:
            prev_scores[str(row[0])] = {"trust_score": row[1], "is_flagged": row[2]}

    for user_id in user_ids:
        try:
            # Call the SQL scoring function
            db.execute(text("SELECT recalculate_user_trust_score(:user_id)"), {"user_id": user_id})
            batch_stats["processed"] += 1

            # Check for score changes
            if user_id in prev_scores:
                new_result = db.execute(text("""
                    SELECT trust_score, is_flagged 
                    FROM user_credit_profiles 
                    WHERE user_id = :user_id
                """), {"user_id": user_id}).fetchone()

                if new_result:
                    old_score = prev_scores[user_id]["trust_score"] or 300
                    new_score = new_result[0]
                    old_flagged = prev_scores[user_id]["is_flagged"] or False
                    new_flagged = new_result[1]

                    if old_score - new_score >= SCORE_DROP_THRESHOLD:
                        batch_stats["score_drops"] += 1
                        logger.warning(
                            f"[CREDIT_BATCH] Score drop: user={user_id} "
                            f"{old_score} -> {new_score}"
                        )

                    if new_score > old_score:
                        batch_stats["score_increases"] += 1

                    if new_flagged and not old_flagged:
                        batch_stats["newly_flagged"] += 1
                        logger.warning(f"[CREDIT_BATCH] Newly flagged: user={user_id}")

                    # Alert if score falls below threshold
                    if new_score < SCORE_FLAG_THRESHOLD and old_score >= SCORE_FLAG_THRESHOLD:
                        _alert_low_score(user_id, new_score)

        except Exception as e:
            batch_stats["failed"] += 1
            batch_stats["errors"].append(f"user={user_id}: {str(e)}")
            logger.error(f"[CREDIT_BATCH] Failed for user {user_id}: {e}")
            # Don't rollback — continue with next user

    return batch_stats


def _alert_low_score(user_id: str, score: int):
    """Send alert when a user's score drops below threshold."""
    logger.warning(f"[CREDIT_ALERT] User {user_id} score dropped to {score} (below {SCORE_FLAG_THRESHOLD})")
    # TODO: Integrate with push notification or email system
    # from app.workers.email_worker import send_admin_alert
    # send_admin_alert.delay(
    #     subject=f"User Score Alert: {user_id}",
    #     body=f"User trust score dropped to {score}. AutoBuy eligibility may be affected."
    # )


def _send_batch_summary(stats: Dict[str, Any]):
    """Send summary of batch job to admin."""
    logger.info(f"[CREDIT_BATCH_SUMMARY] {stats}")
    # TODO: Send to admin dashboard or Slack
    # Could write to a batch_logs table for dashboard display


# ==============================================================================
# Individual User Score Update (Real-time)
# ==============================================================================

@celery_app.task(
    bind=True,
    name="app.tasks_credit_scoring.update_user_credit_score",
    max_retries=2,
    default_retry_delay=60,
)
def update_user_credit_score(self: Task, user_id: str) -> Dict[str, Any]:
    """
    Real-time credit score update for a single user.
    Triggered after: purchase, payment, dispute, KYC verification.

    Args:
        user_id: UUID of the user to recalculate

    Returns:
        {"user_id": "...", "old_score": 720, "new_score": 745, "changed": True}
    """
    db: Session = SessionLocal()

    try:
        # Get old score
        old_result = db.execute(text("""
            SELECT trust_score FROM user_credit_profiles WHERE user_id = :user_id
        """), {"user_id": user_id}).fetchone()
        old_score = old_result[0] if old_result else 300

        # Recalculate
        db.execute(text("SELECT recalculate_user_trust_score(:user_id)"), {"user_id": user_id})
        db.commit()

        # Get new score
        new_result = db.execute(text("""
            SELECT trust_score, auto_buy_eligible FROM user_credit_profiles WHERE user_id = :user_id
        """), {"user_id": user_id}).fetchone()
        new_score = new_result[0] if new_result else 300
        eligible = new_result[1] if new_result else False

        logger.info(
            f"[CREDIT_UPDATE] user={user_id} {old_score} -> {new_score} "
            f"(eligible={eligible})"
        )

        return {
            "user_id": user_id,
            "old_score": old_score,
            "new_score": new_score,
            "changed": old_score != new_score,
            "auto_buy_eligible": eligible,
        }

    except Exception as e:
        db.rollback()
        logger.error(f"[CREDIT_UPDATE] Failed for user {user_id}: {e}")
        if self.request.retries < 2:
            raise self.retry(exc=e)
        raise

    finally:
        db.close()


# ==============================================================================
# Score Recalculation Triggers (Event-driven)
# ==============================================================================

@celery_app.task(name="app.tasks_credit_scoring.on_payment_success")
def on_payment_success(user_id: str, order_id: str, amount: float):
    """Trigger score update after successful payment."""
    logger.info(f"[CREDIT_TRIGGER] Payment success: user={user_id} order={order_id} amount={amount}")
    update_user_credit_score.delay(user_id)


@celery_app.task(name="app.tasks_credit_scoring.on_order_delivered")
def on_order_delivered(user_id: str, order_id: str):
    """Trigger score update after order delivery."""
    logger.info(f"[CREDIT_TRIGGER] Order delivered: user={user_id} order={order_id}")
    update_user_credit_score.delay(user_id)


@celery_app.task(name="app.tasks_credit_scoring.on_order_refunded")
def on_order_refunded(user_id: str, order_id: str, refund_amount: float):
    """Trigger score update after refund (potential penalty)."""
    logger.info(f"[CREDIT_TRIGGER] Order refunded: user={user_id} order={order_id} refund={refund_amount}")
    update_user_credit_score.delay(user_id)


@celery_app.task(name="app.tasks_credit_scoring.on_kyc_verified")
def on_kyc_verified(user_id: str):
    """Trigger score update after KYC verification."""
    logger.info(f"[CREDIT_TRIGGER] KYC verified: user={user_id}")
    update_user_credit_score.delay(user_id)


@celery_app.task(name="app.tasks_credit_scoring.flag_user")
def flag_user(user_id: str, reason: str, admin_id: str = None):
    """
    Manually flag a user (admin action or fraud detection).
    Immediately updates credit profile and disables AutoBuy.
    """
    db: Session = SessionLocal()

    try:
        db.execute(text("""
            UPDATE user_credit_profiles
            SET is_flagged = TRUE,
                flag_reason = :reason,
                flagged_at = NOW(),
                updated_at = NOW()
            WHERE user_id = :user_id
        """), {"user_id": user_id, "reason": reason})

        # Also disable AutoBuy at profile level
        db.execute(text("""
            UPDATE user_profiles
            SET auto_buy_enabled = FALSE,
                updated_at = NOW()
            WHERE user_id = :user_id
        """), {"user_id": user_id})

        # Log admin action if applicable
        if admin_id:
            db.execute(text("""
                INSERT INTO admin_audit_logs (admin_id, action, entity_type, entity_id, new_value, created_at)
                VALUES (:admin_id, 'flag_user', 'user_credit_profiles', :user_id, :reason, NOW())
            """), {"admin_id": admin_id, "user_id": user_id, "reason": f'"{reason}"'})

        db.commit()

        logger.warning(f"[CREDIT_FLAG] User {user_id} flagged: {reason}")

        # Trigger real-time score recalculation
        update_user_credit_score.delay(user_id)

        return {"user_id": user_id, "flagged": True, "reason": reason}

    except Exception as e:
        db.rollback()
        logger.error(f"[CREDIT_FLAG] Failed to flag user {user_id}: {e}")
        raise

    finally:
        db.close()


@celery_app.task(name="app.tasks_credit_scoring.unflag_user")
def unflag_user(user_id: str, admin_id: str = None):
    """Remove flag from user (admin action)."""
    db: Session = SessionLocal()

    try:
        db.execute(text("""
            UPDATE user_credit_profiles
            SET is_flagged = FALSE,
                flag_reason = NULL,
                flagged_at = NULL,
                updated_at = NOW()
            WHERE user_id = :user_id
        """), {"user_id": user_id})

        if admin_id:
            db.execute(text("""
                INSERT INTO admin_audit_logs (admin_id, action, entity_type, entity_id, created_at)
                VALUES (:admin_id, 'unflag_user', 'user_credit_profiles', :user_id, NOW())
            """), {"admin_id": admin_id, "user_id": user_id})

        db.commit()

        logger.info(f"[CREDIT_UNFLAG] User {user_id} unflagged")
        update_user_credit_score.delay(user_id)

        return {"user_id": user_id, "flagged": False}

    except Exception as e:
        db.rollback()
        logger.error(f"[CREDIT_UNFLAG] Failed to unflag user {user_id}: {e}")
        raise

    finally:
        db.close()
