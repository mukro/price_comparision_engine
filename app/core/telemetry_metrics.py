# app/core/telemetry_metrics.py
from fastapi import FastAPI
from prometheus_client import Counter, Histogram
from prometheus_fastapi_instrumentator import Instrumentator

# Custom Enterprise Metrics
SCRAPE_JOBS_TOTAL = Counter(
    "scrape_jobs_total",
    "Total number of web scraping jobs executed",
    ["vendor_id", "status"],
)

PRICE_REPRICING_EVENTS = Counter(
    "repricing_events_total",
    "Total number of dynamic repricing triggers fired",
    ["merchant_id", "circuit_breaker_tripped"],
)

MATCHING_CONFIDENCE_HISTOGRAM = Histogram(
    "entity_resolution_confidence_score",
    "Distribution of confidence scores for vendor match jobs",
    buckets=[0.1, 0.3, 0.5, 0.7, 0.85, 0.95, 1.0],
)


def setup_prometheus_metrics(app: FastAPI) -> None:
    """Exposes a /metrics endpoint for Grafana/Prometheus to scrape. Called from app/main.py startup."""
    Instrumentator().instrument(app).expose(app, endpoint="/metrics")
