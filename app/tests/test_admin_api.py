from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from jose import jwt

from app.config import settings
from app.db import get_db_connection, get_redis_client
from extras.main1 import app

# Test client instance
client = TestClient(app)

# Test Configuration Secrets
TEST_SECRET_KEY = getattr(settings, "JWT_SECRET_KEY", "super-secret-key-change-me")
TEST_ALGORITHM = getattr(settings, "JWT_ALGORITHM", "HS256")


# ==========================================
# HELPER FUNCTIONS FOR JWT MOCKING
# ==========================================

def create_mock_jwt(user_id: str, email: str, role: str) -> str:
    """Generates a signed JWT bearer token for testing different user roles."""
    payload = {
        "sub": user_id,
        "email": email,
        "role": role
    }
    return jwt.encode(payload, TEST_SECRET_KEY, algorithm=TEST_ALGORITHM)


@pytest.fixture
def admin_token():
    """Fixture providing a valid admin JWT token."""
    return create_mock_jwt("user-admin-123", "admin@example.com", "admin")


@pytest.fixture
def regular_user_token():
    """Fixture providing a valid non-admin JWT token."""
    return create_mock_jwt("user-regular-456", "user@example.com", "user")


@pytest.fixture
def mock_db_conn():
    """Mocks the PostgreSQL database connection and cursor."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    return mock_conn, mock_cursor


@pytest.fixture
def mock_redis():
    """Mocks the Redis client instance."""
    return MagicMock()


# ==========================================
# 1. AUTHENTICATION & AUTHORIZATION TESTS
# ==========================================

def test_pending_matches_unauthenticated():
    """Requests without an Authorization header should return 401 Unauthorized."""
    response = client.get("/api/v1/admin/pending-matches")
    assert response.status_code == 401
    assert response.json()["detail"] == "Not authenticated"


def test_pending_matches_forbidden_role(regular_user_token):
    """Authenticated non-admin users should return 403 Forbidden."""
    headers = {"Authorization": f"Bearer {regular_user_token}"}
    response = client.get("/api/v1/admin/pending-matches", headers=headers)
    
    assert response.status_code == 403
    assert "Admin credentials required" in response.json()["detail"]


# ==========================================
# 2. GET /pending-matches ENDPOINT TESTS
# ==========================================

def test_pending_matches_success(admin_token, mock_db_conn):
    """Admin users should successfully retrieve the pending review queue."""
    mock_conn, mock_cursor = mock_db_conn

    # Simulated DB response
    mock_cursor.fetchall.return_value = [
        {
            "offer_id": "offer-uuid-1",
            "raw_title": "Sony WH-1000XM5 Wireless Headphones",
            "current_price": 348.00,
            "product_url": "https://example.com/p/123",
            "confidence_score": 0.74,
            "suggested_product_id": "prod-uuid-1",
            "suggested_product_title": "Sony WH-1000XM5 Headphones - Black",
            "suggested_product_brand": "Sony"
        }
    ]

    # Override FastAPI DB dependency
    app.dependency_overrides[get_db_connection] = lambda: mock_conn

    headers = {"Authorization": f"Bearer {admin_token}"}
    response = client.get("/api/v1/admin/pending-matches?limit=10", headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["data"][0]["offer_id"] == "offer-uuid-1"

    # Reset dependency overrides
    app.dependency_overrides.clear()


# ==========================================
# 3. POST /review-match ENDPOINT TESTS
# ==========================================

@patch("app.api.admin.override_product_match")
def test_review_single_match_approve_success(mock_override_func, admin_token, mock_redis):
    """Admin can approve a pending match and trigger Redis cache invalidation."""
    mock_override_func.return_value = {"matched_product_id": "prod-uuid-1", "status": "matched"}

    # Override Redis dependency
    app.dependency_overrides[get_redis_client] = lambda: mock_redis

    headers = {"Authorization": f"Bearer {admin_token}"}
    payload = {
        "offer_id": "offer-uuid-1",
        "target_product_id": "prod-uuid-1",
        "action": "approve"
    }

    response = client.post("/api/v1/admin/review-match", json=payload, headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    
    # Ensure matcher override function was called with reviewer details
    mock_override_func.assert_called_once_with(
        offer_id="offer-uuid-1",
        action="approve",
        target_product_id="prod-uuid-1",
        reviewer_id="user-admin-123"
    )

    # Ensure Redis cache key was deleted
    mock_redis.delete.assert_called_once_with("cache:grid:prod-uuid-1")

    app.dependency_overrides.clear()


def test_review_single_match_invalid_action(admin_token):
    """Submitting an unsupported action returns 400 Bad Request."""
    headers = {"Authorization": f"Bearer {admin_token}"}
    payload = {
        "offer_id": "offer-uuid-1",
        "action": "invalid_action_name"
    }

    response = client.post("/api/v1/admin/review-match", json=payload, headers=headers)

    assert response.status_code == 400
    assert "Invalid action" in response.json()["detail"]