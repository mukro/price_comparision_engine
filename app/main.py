# app/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import db
from app.api import admin, alerts, merchant, products
from app.config import settings
from app.core.telemetry_metrics import setup_prometheus_metrics

app = FastAPI(
    title="Price Comparison Engine API",
    version="1.0.0",
    description="REST API for multi-vendor product searching, price grids, and historical trend tracking.",
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


# Routers
app.include_router(products.router)
app.include_router(merchant.router)
app.include_router(admin.router)
app.include_router(alerts.router)

# Exposes GET /metrics for Prometheus/Grafana
setup_prometheus_metrics(app)


@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": "1.0.0"}
