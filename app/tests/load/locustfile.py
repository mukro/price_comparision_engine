# tests/load/locustfile.py
"""
Locust load testing for Price Comparison Engine.

Usage:
    locust -f tests/load/locustfile.py --host=http://localhost:8000
    # Then open http://localhost:8089 to configure users and spawn rate

Or headless:
    locust -f tests/load/locustfile.py --host=http://localhost:8000         --users 100 --spawn-rate 10 --run-time 5m --headless
"""
import random
import uuid

from locust import HttpUser, between, task


class PriceEngineUser(HttpUser):
    """Simulates a typical user browsing price comparisons."""

    wait_time = between(1, 5)  # Think time between requests
    host = "http://localhost:8000"

    # Sample product IDs for realistic load (replace with real UUIDs from your DB)
    PRODUCT_IDS = [
        "550e8400-e29b-41d4-a716-446655440000",
        "550e8400-e29b-41d4-a716-446655440001",
        "550e8400-e29b-41d4-a716-446655440002",
    ]

    SEARCH_QUERIES = [
        "iphone 15",
        "sony headphones",
        "macbook pro",
        "samsung galaxy",
        "nike shoes",
        "dell laptop",
        "airpods",
        "playstation 5",
    ]

    def on_start(self):
        """Called when a user starts."""
        self.client.get("/health", name="Health Check")

    @task(5)
    def search_products(self):
        """Most common action: searching for products."""
        query = random.choice(self.SEARCH_QUERIES)
        with self.client.get(
            "/api/v1/products/search",
            params={"q": query, "limit": 20, "offset": 0},
            catch_response=True,
            name="Search Products",
        ) as response:
            if response.status_code == 200:
                data = response.json()
                if data and len(data) > 0:
                    # Store a random product ID for subsequent tasks
                    self.product_id = data[0]["id"]
                    response.success()
                else:
                    response.failure("Empty search results")
            else:
                response.failure(f"Status {response.status_code}")

    @task(3)
    def get_price_grid(self):
        """View price comparison grid for a product."""
        product_id = getattr(self, "product_id", random.choice(self.PRODUCT_IDS))
        with self.client.get(
            f"/api/v1/products/{product_id}/grid",
            catch_response=True,
            name="Price Grid",
        ) as response:
            if response.status_code == 200:
                response.success()
            elif response.status_code == 404:
                response.success()  # Product not found is acceptable
            else:
                response.failure(f"Status {response.status_code}")

    @task(2)
    def get_price_history(self):
        """View historical price trends."""
        product_id = getattr(self, "product_id", random.choice(self.PRODUCT_IDS))
        with self.client.get(
            f"/api/v1/products/{product_id}/history",
            params={"days": 30},
            catch_response=True,
            name="Price History",
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"Status {response.status_code}")

    @task(2)
    def get_buying_insights(self):
        """Get AI buy-timing recommendation."""
        product_id = getattr(self, "product_id", random.choice(self.PRODUCT_IDS))
        with self.client.get(
            f"/api/v1/products/{product_id}/insights",
            catch_response=True,
            name="Buying Insights",
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"Status {response.status_code}")

    @task(1)
    def get_alternatives(self):
        """Find cheaper alternatives."""
        product_id = getattr(self, "product_id", random.choice(self.PRODUCT_IDS))
        with self.client.get(
            f"/api/v1/products/{product_id}/alternatives",
            params={"limit": 3},
            catch_response=True,
            name="Feature Alternatives",
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"Status {response.status_code}")

    @task(1)
    def create_price_alert(self):
        """Create a price drop alert (lower frequency)."""
        product_id = getattr(self, "product_id", random.choice(self.PRODUCT_IDS))
        email = f"loadtest_{uuid.uuid4().hex[:8]}@example.com"
        with self.client.post(
            "/api/v1/alerts",
            json={
                "email": email,
                "product_id": product_id,
                "target_price": round(random.uniform(10.0, 500.0), 2),
            },
            catch_response=True,
            name="Create Alert",
        ) as response:
            if response.status_code in (200, 201):
                response.success()
            elif response.status_code == 404:
                response.success()  # Product not found is acceptable
            else:
                response.failure(f"Status {response.status_code}")


class AdminUser(HttpUser):
    """Simulates admin panel usage (much lower frequency)."""

    wait_time = between(10, 30)
    weight = 1  # 1 admin for every 10 regular users

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.token = None

    def on_start(self):
        """Login as admin and store JWT."""
        response = self.client.post(
            "/api/v1/admin/auth/login",
            json={"email": "admin@example.com", "password": "changeme"},
            name="Admin Login",
        )
        if response.status_code == 200:
            self.token = response.json()["access_token"]
        else:
            print(f"Admin login failed: {response.status_code}")

    @task(1)
    def get_pending_matches(self):
        """Admin reviews pending product matches."""
        if not self.token:
            return
        with self.client.get(
            "/api/v1/admin/pending-matches",
            headers={"Authorization": f"Bearer {self.token}"},
            catch_response=True,
            name="Admin Pending Matches",
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"Status {response.status_code}")

    @task(1)
    def get_compliance_settings(self):
        """Admin checks compliance configuration."""
        if not self.token:
            return
        with self.client.get(
            "/api/v1/admin/compliance/settings",
            headers={"Authorization": f"Bearer {self.token}"},
            catch_response=True,
            name="Admin Compliance Settings",
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"Status {response.status_code}")
