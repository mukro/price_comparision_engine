# tests/test_admin_api.py
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from jose import jwt

from app.config import settings
from app.main import app

client = TestClient(app)

TEST_SECRET_KEY = settings.JWT_SECRET_KEY
TEST_ALGORITHM = settings.JWT_ALGORITHM


def create_mock_jwt(user_id: str, email: str, role: str) -> str:
    payload = {"sub": user_id, "email": email, "role": role}
    return jwt.encode(payload, TEST_SECRET_KEY, algorithm=TEST_ALGORITHM)


@pytest.fixture
def admin_token():
    return create_mock_jwt("user-admin-123", "admin@example.com", "admin")


@pytest.fixture
def regular_user_token():
    return create_mock_jwt("user-regular-456", "user@example.com", "user")


# ==========================================
# Authentication & authorization
# ==========================================

def test_pending_matches_unauthenticated():
    response = client.get("/api/v1/admin/pending-matches")
    assert response.status_code == 401


def test_pending_matches_forbidden_role(regular_user_token):
    headers = {"Authorization": f"Bearer {regular_user_token}"}
    response = client.get("/api/v1/admin/pending-matches", headers=headers)
    assert response.status_code == 403
    assert "Admin credentials required" in response.json()["detail"]


def test_login_success():
    response = client.post(
        "/api/v1/admin/auth/login",
        json={"email": settings.ADMIN_EMAIL, "password": settings.ADMIN_PASSWORD},
    )
    assert response.status_code == 200
    assert "access_token" in response.json()


def test_login_bad_credentials():
    response = client.post(
        "/api/v1/admin/auth/login",
        json={"email": settings.ADMIN_EMAIL, "password": "wrong-password"},
    )
    assert response.status_code == 401


# ==========================================
# GET /pending-matches
# ==========================================

@patch("app.api.admin.get_conn")
def test_pending_matches_success(mock_get_conn, admin_token):
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [
        {
            "offer_id": "offer-uuid-1",
            "vendor_title": "Sony WH-1000XM5 Wireless Headphones",
            "current_price": 348.00,
            "confidence_score": 0.74,
            "vendor_name": "ExampleStore",
            "suggested_product_id": "prod-uuid-1",
            "suggested_product_title": "Sony WH-1000XM5 Headphones - Black",
        }
    ]
    mock_conn.cursor.return_value = mock_cursor
    mock_get_conn.return_value.__enter__.return_value = mock_conn

    headers = {"Authorization": f"Bearer {admin_token}"}
    response = client.get("/api/v1/admin/pending-matches", headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["data"][0]["offer_id"] == "offer-uuid-1"


# ==========================================
# POST /review-match
# ==========================================

@patch("app.api.admin.invalidate_grid_cache")
@patch("app.api.admin.get_conn")
def test_review_match_approve_success(mock_get_conn, mock_invalidate, admin_token):
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = {"product_id": "prod-uuid-1"}
    mock_conn.cursor.return_value = mock_cursor
    mock_get_conn.return_value.__enter__.return_value = mock_conn

    headers = {"Authorization": f"Bearer {admin_token}"}
    payload = {"offer_id": "offer-uuid-1", "approved": True, "correct_product_id": "prod-uuid-1"}

    response = client.post("/api/v1/admin/review-match", json=payload, headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    mock_invalidate.assert_called_once_with("prod-uuid-1")
