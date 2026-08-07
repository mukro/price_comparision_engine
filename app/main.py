# app/main.py
"""
Price Comparison Engine API v2.0
Monetization-ready: affiliate tracking, partner feeds, sponsored listings,
user auth, watchlists, and push notifications.
"""
from app.core.telemetry_metrics import setup_prometheus_metrics
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import db
from app.api import (
    admin,
    affiliate,  # NEW: click tracking + conversion webhooks
    alerts,
    auth,  # NEW: user JWT auth
    merchant,
    partner_feed,  # NEW: Tier 3 partner feed ingestion
    products,
    sponsored,  # NEW: promoted listings + merchant wallet
    watchlist,  # NEW: user watchlists + price drop alerts
)
from app.config import settings

app = FastAPI(
    title="Price Comparison Engine API",
    version="2.0.0",
    description=(
        "REST API for multi-vendor product search, price grids, "
        "affiliate click tracking, merchant partner feeds, "
        "sponsored placements, and price-drop alerts."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    await db.init_db_pool()
    await db.init_redis()


@app.on_event("shutdown")
async def shutdown():
    await db.close_db_pool()
    await db.close_redis()

# ------------------------------------------------------------------
# Admin Panel (static files)
# ------------------------------------------------------------------
app.mount("/admin", StaticFiles(directory="app/frontend/admin", html=True), name="admin")

# ------------------------------------------------------------------
# API Routers (order matters for path resolution)
# ------------------------------------------------------------------

# 1. Auth (no prefix — handles /auth/*)
app.include_router(auth.router)

# 2. Products (public search + detail)
app.include_router(products.router)

# 3. Affiliate (public click tracking redirect)
app.include_router(affiliate.router)

# 4. Watchlist (user-scoped, requires auth)
app.include_router(watchlist.router)

# 5. Partner Feed (merchant API key auth)
app.include_router(partner_feed.router)

# 6. Sponsored + Merchant Wallet (merchant JWT auth)
app.include_router(sponsored.router)

# 7. B2B Merchant Pricing Engine (merchant JWT auth)
app.include_router(merchant.router)

# 8. Admin (admin JWT auth)
app.include_router(admin.router)

# 9. Alerts (email-based price drop alerts — legacy + new push)
app.include_router(alerts.router)

# ------------------------------------------------------------------
# Observability
# ------------------------------------------------------------------
setup_prometheus_metrics(app)


@app.get("/health")
async def health_check():
    """Liveness probe for orchestrators."""
    return {
        "status": "healthy",
        "version": "2.0.0",
        "features": [
            "affiliate_tracking",
            "partner_feeds",
            "sponsored_listings",
            "user_auth",
            "watchlists",
            "push_notifications",
        ],
    }
