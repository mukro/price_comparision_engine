# ==========================================
# Multi-Stage Dockerfile for Price Comparison Engine
# Stage 1: Builder (compiles deps, downloads ML model)
# Stage 2: Runtime (slim, no build tools)
# ==========================================

# ------------------------------------------
# STAGE 1: Builder
# ------------------------------------------
FROM python:3.11-slim-bookworm AS builder

WORKDIR /build

# Install system build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies into a virtual environment
# This keeps the runtime image clean
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Pre-download the sentence-transformers model so workers don't download on cold start
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# Install Playwright browsers (Chromium only, skip deps — we'll install them in runtime)
RUN playwright install chromium
RUN playwright install-deps chromium

# ------------------------------------------
# STAGE 2: Runtime
# ------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

LABEL maintainer="your-email@domain.com"
LABEL description="Price Comparison Engine - FastAPI + Celery + Playwright"

WORKDIR /app

# Install ONLY runtime system dependencies
# (No gcc, no build-essential, no python3-dev)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy pre-downloaded Playwright browsers
COPY --from=builder /root/.cache/ms-playwright /root/.cache/ms-playwright

# Copy pre-downloaded ML model cache
COPY --from=builder /root/.cache/torch /root/.cache/torch
COPY --from=builder /root/.cache/huggingface /root/.cache/huggingface

# Create non-root user for security
RUN groupadd -r appgroup && useradd -r -g appgroup appuser \
    && mkdir -p /app /tmp/playwright \
    && chown -R appuser:appgroup /app /tmp/playwright

# Copy application code
COPY --chown=appuser:appgroup . /app

# Switch to non-root user
USER appuser

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Expose API port
EXPOSE 8000

# Default command (overridden by docker-compose for workers/beat)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
