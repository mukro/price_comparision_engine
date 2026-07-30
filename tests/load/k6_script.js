// tests/load/k6_script.js
/**
 * k6 load testing script for Price Comparison Engine.
 *
 * Usage:
 *   k6 run --env BASE_URL=http://localhost:8000 tests/load/k6_script.js
 *
 * Or with more virtual users:
 *   k6 run --vus 100 --duration 5m --env BASE_URL=http://localhost:8000 tests/load/k6_script.js
 *
 * Or with stages (ramp up, sustain, ramp down):
 *   k6 run --env BASE_URL=http://localhost:8000 tests/load/k6_script.js
 *   (stages are defined in the options below)
 */

import http from "k6/http";
import { check, sleep, group } from "k6";
import { Rate, Trend, Counter } from "k6/metrics";

// ==========================================
// Custom Metrics
// ==========================================
const searchLatency = new Trend("search_latency");
const gridLatency = new Trend("grid_latency");
const errorRate = new Rate("errors");
const successfulSearches = new Counter("successful_searches");

// ==========================================
// Configuration
// ==========================================
export const options = {
  stages: [
    { duration: "2m", target: 50 },   // Ramp up to 50 users
    { duration: "5m", target: 50 },   // Stay at 50 users
    { duration: "2m", target: 100 },  // Ramp up to 100 users
    { duration: "5m", target: 100 },  // Stay at 100 users
    { duration: "2m", target: 0 },    // Ramp down
  ],
  thresholds: {
    http_req_duration: ["p(95)<500"],   // 95% of requests under 500ms
    http_req_failed: ["rate<0.05"],      // Error rate under 5%
    search_latency: ["p(95)<300"],       // Search under 300ms
    grid_latency: ["p(95)<400"],         // Grid under 400ms
  },
};

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";

// Sample data
const SEARCH_QUERIES = [
  "iphone 15",
  "sony headphones",
  "macbook pro",
  "samsung galaxy",
  "nike shoes",
  "dell laptop",
  "airpods",
  "playstation 5",
];

const PRODUCT_IDS = [
  "550e8400-e29b-41d4-a716-446655440000",
  "550e8400-e29b-41d4-a716-446655440001",
  "550e8400-e29b-41d4-a716-446655440002",
];

function randomChoice(arr) {
  return arr[Math.floor(Math.random() * arr.length)];
}

function randomInt(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

// ==========================================
// Health Check
// ==========================================
export function setup() {
  const res = http.get(`${BASE_URL}/health`);
  check(res, {
    "health check status is 200": (r) => r.status === 200,
    "health check response is healthy": (r) => r.json("status") === "healthy",
  });
  return { baseUrl: BASE_URL };
}

// ==========================================
// Main Scenario: Browsing User
// ==========================================
export default function (data) {
  const baseUrl = data.baseUrl;

  group("Search Flow", () => {
    // 1. Search for products
    const query = encodeURIComponent(randomChoice(SEARCH_QUERIES));
    const searchRes = http.get(
      `${baseUrl}/api/v1/products/search?q=${query}&limit=20&offset=0`,
      { tags: { name: "SearchProducts" } }
    );

    searchLatency.add(searchRes.timings.duration);

    const searchOk = check(searchRes, {
      "search status is 200": (r) => r.status === 200,
      "search returns array": (r) => Array.isArray(r.json()),
    });

    if (searchOk) {
      successfulSearches.add(1);
    } else {
      errorRate.add(1);
    }

    sleep(randomInt(1, 3));

    // 2. Get price grid for a random product
    const productId = randomChoice(PRODUCT_IDS);
    const gridRes = http.get(
      `${baseUrl}/api/v1/products/${productId}/grid`,
      { tags: { name: "PriceGrid" } }
    );

    gridLatency.add(gridRes.timings.duration);

    check(gridRes, {
      "grid status is 200 or 404": (r) => r.status === 200 || r.status === 404,
      "grid response has product_id": (r) =>
        r.status === 404 || r.json("product_id") !== undefined,
    });

    sleep(randomInt(1, 3));

    // 3. Get price history
    const historyRes = http.get(
      `${baseUrl}/api/v1/products/${productId}/history?days=30`,
      { tags: { name: "PriceHistory" } }
    );

    check(historyRes, {
      "history status is 200": (r) => r.status === 200,
      "history has data array": (r) => Array.isArray(r.json("history")),
    });

    sleep(randomInt(2, 5));
  });

  group("Insights Flow", () => {
    const productId = randomChoice(PRODUCT_IDS);

    // 4. Get buying insights
    const insightsRes = http.get(
      `${baseUrl}/api/v1/products/${productId}/insights`,
      { tags: { name: "BuyingInsights" } }
    );

    check(insightsRes, {
      "insights status is 200": (r) => r.status === 200,
      "insights has recommendation": (r) =>
        r.json("data") && r.json("data").action !== undefined,
    });

    sleep(randomInt(1, 2));

    // 5. Get alternatives
    const altRes = http.get(
      `${baseUrl}/api/v1/products/${productId}/alternatives?limit=3`,
      { tags: { name: "Alternatives" } }
    );

    check(altRes, {
      "alternatives status is 200": (r) => r.status === 200,
    });

    sleep(randomInt(2, 5));
  });
}

// ==========================================
// Alert Creation Scenario (lower frequency)
// ==========================================
export function alertScenario(data) {
  const baseUrl = data.baseUrl;
  const productId = randomChoice(PRODUCT_IDS);
  const email = `k6test_${randomInt(1000, 9999)}@example.com`;

  const payload = JSON.stringify({
    email: email,
    product_id: productId,
    target_price: parseFloat((Math.random() * 500 + 10).toFixed(2)),
  });

  const res = http.post(`${baseUrl}/api/v1/alerts`, payload, {
    headers: { "Content-Type": "application/json" },
    tags: { name: "CreateAlert" },
  });

  check(res, {
    "alert creation status is 200": (r) => r.status === 200,
    "alert has alert_id": (r) => r.json("alert_id") !== undefined,
  });

  sleep(randomInt(5, 10));
}

// ==========================================
// Admin Scenario (very low frequency)
// ==========================================
export function adminScenario(data) {
  const baseUrl = data.baseUrl;

  // Login
  const loginRes = http.post(
    `${baseUrl}/api/v1/admin/auth/login`,
    JSON.stringify({
      email: "admin@example.com",
      password: "changeme",
    }),
    {
      headers: { "Content-Type": "application/json" },
      tags: { name: "AdminLogin" },
    }
  );

  const loginOk = check(loginRes, {
    "admin login status is 200": (r) => r.status === 200,
    "admin login returns token": (r) => r.json("access_token") !== undefined,
  });

  if (!loginOk) {
    errorRate.add(1);
    return;
  }

  const token = loginRes.json("access_token");
  const authHeader = { Authorization: `Bearer ${token}` };

  // Get pending matches
  const pendingRes = http.get(`${baseUrl}/api/v1/admin/pending-matches`, {
    headers: authHeader,
    tags: { name: "AdminPendingMatches" },
  });

  check(pendingRes, {
    "pending matches status is 200": (r) => r.status === 200,
  });

  sleep(randomInt(5, 10));

  // Get compliance settings
  const complianceRes = http.get(`${baseUrl}/api/v1/admin/compliance/settings`, {
    headers: authHeader,
    tags: { name: "AdminComplianceSettings" },
  });

  check(complianceRes, {
    "compliance settings status is 200": (r) => r.status === 200,
  });

  sleep(randomInt(10, 20));
}
