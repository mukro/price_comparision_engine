FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for Playwright & Postgres driver
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browser binaries and OS-level dependencies
RUN playwright install --with-deps chromium

COPY . .

EXPOSE 8000
