# 🚀 Price Comparison Engine v2 — Complete Installation & Execution Guide

> **Last Updated:** 2026-07-30 | **Target:** Local Dev → Docker → AWS Production

---

## 📋 Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Environment Validation](#2-environment-validation)
3. [Repository Setup](#3-repository-setup)
4. [Environment Configuration](#4-environment-configuration)
5. [Database Setup & Migrations](#5-database-setup--migrations)
6. [Dependency Installation](#6-dependency-installation)
7. [Local Execution (No Docker)](#7-local-execution-no-docker)
8. [Docker Compose (Full Stack)](#8-docker-compose-full-stack)
9. [Layer-by-Layer Verification](#9-layer-by-layer-verification)
10. [AWS Production Deployment](#10-aws-production-deployment)
11. [Post-Deployment Health Checks](#11-post-deployment-health-checks)
12. [Troubleshooting Matrix](#12-troubleshooting-matrix)

---

## 1. Prerequisites

### System Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| OS | Linux/macOS/Windows WSL2 | Ubuntu 22.04 LTS |
| CPU | 4 cores | 8 cores |
| RAM | 8 GB | 16 GB |
| Disk | 20 GB free | 50 GB SSD |
| Python | 3.11 | 3.11.8 |
| Docker | 24.0+ | 25.0+ |
| Docker Compose | 2.20+ | 2.24+ |

### Required Tools

```bash
# Verify installations
python3 --version          # Should print 3.11.x
docker --version           # Should print 24.x+
docker compose version     # Should print 2.x+
git --version              # Should print 2.x+
terraform --version        # Should print 1.5+
aws --version              # Should print 2.x+
```

### Install Missing Tools

```bash
# Ubuntu/Debian
sudo apt update && sudo apt install -y python3.11 python3.11-venv python3-pip git curl

# macOS (Homebrew)
brew install python@3.11 git curl terraform awscli

# Docker (all platforms)
# https://docs.docker.com/engine/install/

# Terraform
# https://developer.hashicorp.com/terraform/install

# AWS CLI
# https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html
```

---

## 2. Environment Validation

Run this diagnostic script before proceeding:

```bash
#!/bin/bash
# save as: check_env.sh

echo "=== Environment Diagnostic ==="
echo ""

# Python
echo "✓ Python: $(python3 --version 2>/dev/null || echo '❌ NOT FOUND')"
echo "✓ pip: $(pip3 --version 2>/dev/null || echo '❌ NOT FOUND')"

# Docker
echo "✓ Docker: $(docker --version 2>/dev/null || echo '❌ NOT FOUND')"
echo "✓ Docker Compose: $(docker compose version 2>/dev/null || echo '❌ NOT FOUND')"

# Check Docker daemon
docker ps >/dev/null 2>&1 && echo "✓ Docker daemon: RUNNING" || echo "❌ Docker daemon: NOT RUNNING"

# Tools
echo "✓ Git: $(git --version 2>/dev/null || echo '❌ NOT FOUND')"
echo "✓ Terraform: $(terraform --version 2>/dev/null | head -1 || echo '❌ NOT FOUND')"
echo "✓ AWS CLI: $(aws --version 2>/dev/null || echo '❌ NOT FOUND')"

# Memory
echo "✓ Memory: $(free -h 2>/dev/null | awk '/^Mem:/{print $2}' || echo 'N/A')"

# Disk
echo "✓ Disk Free: $(df -h . 2>/dev/null | tail -1 | awk '{print $4}')"

echo ""
echo "=== Diagnostic Complete ==="
```

**Expected output:** All checks should show ✅ or version numbers. Fix any ❌ before continuing.

---

## 3. Repository Setup

```bash
# 1. Clone your repository
git clone https://github.com/mukro/price_comparision_engine.git
cd price_comparision_engine

# 2. Create a new branch for v2
git checkout -b v2-production

# 3. Extract the v2 improvements archive
# (Download price_engine_v2_improvements.zip and place it here)
unzip price_engine_v2_improvements.zip -d ./

# 4. Verify file structure
ls -la
# Expected:
#   app/
#   docker-compose.yml
#   Dockerfile
#   requirements.txt
#   scripts/
#   tests/
#   infra/
#   monitoring/
#   docs/
#   Makefile

# 5. Stage changes
git add .
git status
```

---

## 4. Environment Configuration

### 4.1 Create `.env` File

```bash
cp .env.example .env
```

### 4.2 Edit `.env` — Complete Template

```bash
# ==========================================
# 1. DATABASE (PostgreSQL 16 + pgvector)
# ==========================================
POSTGRES_USER=price_engine
POSTGRES_PASSWORD=$(openssl rand -hex 32)        # Generate strong password
POSTGRES_HOST=db                                 # "db" for Docker, "localhost" for local
POSTGRES_PORT=5432
POSTGRES_DB=price_comparison

# ==========================================
# 2. REDIS (Cache + Celery Broker)
# ==========================================
REDIS_PASSWORD=$(openssl rand -hex 32)
REDIS_URL=redis://:${REDIS_PASSWORD}@redis:6379/0

# ==========================================
# 3. EMAIL (SendGrid)
# ==========================================
SENDGRID_API_KEY=SG.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
FROM_EMAIL=alerts@yourdomain.com

# ==========================================
# 4. AUTHENTICATION
# ==========================================
# Generate: python -c "import secrets; print(secrets.token_hex(32))"
JWT_SECRET_KEY=$(openssl rand -hex 32)
JWT_ALGORITHM=HS256
JWT_EXPIRE_MINUTES=60

# Admin credentials
ADMIN_EMAIL=admin@yourdomain.com
# Generate hash: python -c "from passlib.hash import bcrypt; print(bcrypt.hash('YourStrongPass123!'))"
ADMIN_PASSWORD_HASH=$2b$12$...

# ==========================================
# 5. CORS
# ==========================================
ALLOWED_ORIGINS=https://app.yourdomain.com,https://admin.yourdomain.com

# ==========================================
# 6. SCRAPING GOVERNANCE
# ==========================================
SCRAPING_ENABLED=true
DEFAULT_SCRAPE_RPM=6
ENFORCE_ROBOTS_TXT=true
ENFORCE_DOMAIN_ALLOWLIST=true
SCRAPER_USER_AGENT=Mozilla/5.0 (compatible; PriceComparisonBot/1.0; +https://yourdomain.com/bot)
SCRAPER_PROXY_URL=                                # Optional: http://proxy:8080
BROWSER_MAX_PAGES=4
SCRAPER_TIMEOUT_MS=15000

# ==========================================
# 7. AWS (for production archival)
# ==========================================
AWS_REGION=ap-south-1
S3_ARCHIVE_BUCKET=your-archive-bucket-name
S3_ARCHIVE_PREFIX=price-history-archives/
RETENTION_DAYS=90
ARCHIVE_BATCH_SIZE=10000
```

### 4.3 Validate `.env`

```bash
# Check no empty critical values
grep -E "^(JWT_SECRET_KEY|POSTGRES_PASSWORD|ADMIN_PASSWORD_HASH)=" .env | grep -v "changeme"
# Should return lines with actual values
```

---

## 5. Database Setup & Migrations

### 5.1 Option A: Docker PostgreSQL (Recommended for Dev)

```bash
# Start only the database
docker compose up -d db redis

# Wait for health check
docker compose ps
# db and redis should show "healthy"

# Verify connection
docker compose exec db psql -U $POSTGRES_USER -d $POSTGRES_DB -c "SELECT version();"
```

### 5.2 Option B: Local PostgreSQL

```bash
# Install pgvector extension
sudo apt install postgresql-16-pgvector

# Create database
sudo -u postgres psql -c "CREATE DATABASE price_comparison;"
sudo -u postgres psql -c "CREATE USER price_engine WITH PASSWORD 'your_password';"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE price_comparison TO price_engine;"

# Enable pgvector
sudo -u postgres psql -d price_comparison -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

### 5.3 Run Migrations

```bash
# Method 1: SQL migration (idempotent)
docker compose exec -T db psql -U $POSTGRES_USER -d $POSTGRES_DB < scripts/migrate_v1_to_v2.sql

# Method 2: Python migration runner (tracks applied migrations)
pip install asyncpg
python scripts/run_migrations.py --direction up

# Method 3: Programmatic (from Python)
python -c "
import asyncio
from scripts.run_migrations import run_migrations
asyncio.run(run_migrations('up'))
"
```

### 5.4 Verify Migration Success

```bash
docker compose exec db psql -U $POSTGRES_USER -d $POSTGRES_DB -c "\dt"
# Should show: products, vendors, vendor_offers, price_history, scrape_dlq, match_feedback, merchant_api_keys, schema_migrations
```

---

## 6. Dependency Installation

### 6.1 Python Virtual Environment

```bash
# Create venv
python3 -m venv venv

# Activate
source venv/bin/activate        # Linux/macOS
# OR
venv\Scripts\activate         # Windows

# Upgrade pip
pip install --upgrade pip

# Install all dependencies
pip install -r requirements.txt

# Verify key packages
python -c "import fastapi; print(f'FastAPI: {fastapi.__version__}')"
python -c "import protego; print('Protego: OK')"
python -c "import sentence_transformers; print('SentenceTransformers: OK')"
python -c "import playwright; print('Playwright: OK')"
```

### 6.2 Install Playwright Browsers

```bash
playwright install chromium
playwright install-deps chromium   # System dependencies for headless browser
```

### 6.3 Verify ML Model Download

```bash
# Pre-download model (avoids cold-start delay)
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"
# Should complete without errors
```

---

## 7. Local Execution (No Docker)

> ⚠️ **Note:** This requires PostgreSQL and Redis running locally.

### 7.1 Start Services

```bash
# Terminal 1: Start API
source venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Terminal 2: Start Celery Worker
source venv/bin/activate
celery -A app.celery_app:celery_app worker --loglevel=info --concurrency=4

# Terminal 3: Start Celery Beat (scheduler)
source venv/bin/activate
celery -A app.celery_app:celery_app beat --loglevel=info
```

### 7.2 Quick Health Check

```bash
# API health
curl http://localhost:8000/health
# Expected: {"status":"healthy"}

# OpenAPI docs
curl http://localhost:8000/docs
# Should return HTML (open in browser)

# Search endpoint
curl "http://localhost:8000/api/v1/products/search?q=iphone&limit=5"
# Expected: JSON array of products
```

---

## 8. Docker Compose (Full Stack)

### 8.1 Build & Start Everything

```bash
# Build all images
docker compose build

# Start full stack (detached)
docker compose up -d

# Watch logs
docker compose logs -f api
# OR specific service:
docker compose logs -f celery_worker
```

### 8.2 Verify All Services

```bash
# Check all containers are healthy
docker compose ps

# Expected output:
# NAME                STATUS          PORTS
# price_api           Up (healthy)    0.0.0.0:8000->8000/tcp
# price_celery_worker Up (healthy)
# price_celery_beat   Up (healthy)
# price_db            Up (healthy)    127.0.0.1:5432->5432/tcp
# price_redis         Up (healthy)
```

### 8.3 Scale Workers (Optional)

```bash
# Scale Celery workers to 4 instances
docker compose up -d --scale celery_worker=4
```

---

## 9. Layer-by-Layer Verification

### 9.1 Layer 1: Database Connectivity

```bash
# From API container
docker compose exec api python -c "
from app.db_sync import get_conn
with get_conn() as conn:
    cur = conn.cursor()
    cur.execute('SELECT version();')
    print(cur.fetchone()[0])
"

# From host (if port exposed)
psql postgresql://price_engine:password@localhost:5432/price_comparison -c "SELECT 1;"
```

### 9.2 Layer 2: Redis Connectivity

```bash
# From API container
docker compose exec api python -c "
from app.db_sync import redis_client
redis_client.set('test', 'ok')
print(redis_client.get('test'))
"

# Expected output: ok
```

### 9.3 Layer 3: Compliance / robots.txt

```bash
# Test Protego integration
python -c "
from app.core.compliance import _is_allowed_by_robots
allowed, delay = _is_allowed_by_robots('google.com')
print(f'Allowed: {allowed}, Crawl-delay: {delay}')
"
```

### 9.4 Layer 4: ML Model / Embeddings

```bash
# Test sentence transformer
docker compose exec api python -c "
from app.core.matcher import get_embedding
emb = get_embedding('iPhone 15 Pro Max 256GB')
print(f'Embedding dimension: {len(emb)}')
"

# Expected: Embedding dimension: 384
```

### 9.5 Layer 5: Playwright / Scraping

```bash
# Test browser launch
docker compose exec celery_worker python -c "
import asyncio
from playwright.async_api import async_playwright
async def test():
    p = await async_playwright().start()
    browser = await p.chromium.launch()
    page = await browser.new_page()
    await page.goto('https://example.com')
    title = await page.title()
    await browser.close()
    print(f'Page title: {title}')
asyncio.run(test())
"
```

### 9.6 Layer 6: Celery Task Pipeline

```bash
# Trigger a test task
docker compose exec api python -c "
from app.tasks import process_vendor_scrape
result = process_vendor_scrape.delay({
    'vendor_id': 'test-vendor',
    'vendor_product_id': 'test-123',
    'product_url': 'https://example.com/product',
    'title_selector': 'h1',
    'price_selector': '.price'
})
print(f'Task ID: {result.id}')
"

# Check task status
docker compose exec api celery -A app.celery_app:celery_app inspect active
```

### 9.7 Layer 7: Email Delivery

```bash
# Test email worker
docker compose exec api python -c "
from app.workers.email_worker import send_price_drop_email
sent = send_price_drop_email(
    to_email='test@example.com',
    product_title='Test Product',
    new_price=99.99,
    buy_url='https://example.com'
)
print(f'Email sent: {sent}')
"
```

### 9.8 Layer 8: API End-to-End

```bash
# 1. Admin login
curl -X POST http://localhost:8000/api/v1/admin/auth/login   -H "Content-Type: application/json"   -d '{"email":"admin@yourdomain.com","password":"YourStrongPass123!"}'
# Save the access_token from response

# 2. Check compliance settings
curl http://localhost:8000/api/v1/admin/compliance/settings   -H "Authorization: Bearer <token>"

# 3. Search products
curl "http://localhost:8000/api/v1/products/search?q=laptop&limit=5"

# 4. Create price alert
curl -X POST http://localhost:8000/api/v1/alerts   -H "Content-Type: application/json"   -d '{"email":"user@example.com","product_id":"550e8400-e29b-41d4-a716-446655440000","target_price":500}'
```

---

## 10. AWS Production Deployment

### 10.1 Pre-Deployment Checklist

```bash
# 1. AWS credentials configured
aws sts get-caller-identity
# Should return your Account, Arn, UserId

# 2. Terraform backend ready
aws s3 ls s3://price-engine-tfstate 2>/dev/null || echo "Create bucket first"

# 3. Docker image built and tested locally
docker build -t price-comparison:test .
docker run --rm price-comparison:test python -c "import app.main; print('OK')"
```

### 10.2 Bootstrap Terraform

```bash
cd infra/aws

# Create S3 bucket for state (one-time)
aws s3 mb s3://price-engine-tfstate --region ap-south-1
aws s3api put-bucket-versioning   --bucket price-engine-tfstate   --versioning-configuration Status=Enabled

# Create DynamoDB for state locking (one-time)
aws dynamodb create-table   --table-name terraform-locks   --attribute-definitions AttributeName=LockID,AttributeType=S   --key-schema AttributeName=LockID,KeyType=HASH   --billing-mode PAY_PER_REQUEST   --region ap-south-1

# Initialize Terraform
terraform init
```

### 10.3 Deploy

```bash
# Create terraform.tfvars
cat > terraform.tfvars <<EOF
aws_region        = "ap-south-1"
environment       = "prod"
project_name      = "price-comparison"
domain_name       = "api.yourdomain.com"
allowed_origins   = "https://app.yourdomain.com"
sendgrid_api_key  = "SG.xxx..."
db_instance_class = "db.t3.micro"
redis_node_type   = "cache.t3.micro"
scraping_enabled  = true
enforce_robots_txt = true
EOF

# Plan and apply
terraform plan -out=tfplan
terraform apply tfplan
```

### 10.4 Push Docker Image to ECR

```bash
# Login to ECR
aws ecr get-login-password --region ap-south-1 |   docker login --username AWS --password-stdin $(terraform output -raw ecr_repository_url)

# Build, tag, push
docker build -t price-comparison:latest .
docker tag price-comparison:latest $(terraform output -raw ecr_repository_url):latest
docker push $(terraform output -raw ecr_repository_url):latest

# Force ECS redeployment
aws ecs update-service --cluster price-comparison --service api --force-new-deployment
aws ecs update-service --cluster price-comparison --service worker --force-new-deployment
```

---

## 11. Post-Deployment Health Checks

### 11.1 Infrastructure Verification

```bash
# Check ECS services
aws ecs describe-services   --cluster price-comparison   --services api worker beat   --query "services[*].{Name:name,Status:status,Running:runningCount,Desired:desiredCount}"

# Check RDS
aws rds describe-db-instances   --db-instance-identifier price-comparison-db   --query "DBInstances[0].DBInstanceStatus"
# Expected: "available"

# Check ElastiCache
aws elasticache describe-replication-groups   --replication-group-id price-comparison-redis   --query "ReplicationGroups[0].Status"
# Expected: "available"
```

### 11.2 Application Verification

```bash
ALB_URL=$(terraform output -raw alb_dns_name)

# Health check
curl -f https://$ALB_URL/health

# API docs
curl -f https://$ALB_URL/docs

# Search (with rate limit awareness)
curl -f "https://$ALB_URL/api/v1/products/search?q=iphone&limit=5"
```

### 11.3 Monitoring Setup

```bash
# Import Grafana dashboard
curl -X POST http://your-grafana:3000/api/dashboards/db   -H "Content-Type: application/json"   -d @monitoring/grafana/dashboard.json

# Verify Prometheus targets
curl http://localhost:9090/api/v1/targets | jq '.data.activeTargets[].labels.job'
```

---

## 12. Troubleshooting Matrix

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ModuleNotFoundError: No module named 'protego'` | Missing dependency | `pip install protego` |
| `ImportError: cannot import name 'RealDictCursor'` | Missing `psycopg2-binary` | `pip install psycopg2-binary` |
| `playwright._impl._errors.Error: Browser not found` | Browsers not installed | `playwright install chromium` |
| `Connection refused` to db/redis | Services not started | `docker compose up -d db redis` |
| `FATAL: database "price_comparison" does not exist` | DB not created | Run `init.sql` or migrations |
| `celery_worker` exits immediately | Missing broker connection | Check `REDIS_URL` in `.env` |
| `scraping_enabled` is false | Kill-switch active | Toggle via admin API or `.env` |
| `robots.txt disallows scraping` | robots.txt blocks bot | Check `ENFORCE_ROBOTS_TXT` setting |
| `Rate limit exceeded` | Too many requests | Increase `scrape_rpm` or wait |
| `SSL certificate error` | Self-signed cert | Use `--insecure` for curl or proper cert |
| `ECS task stuck in PENDING` | No Fargate capacity | Check VPC NAT Gateway, subnets |
| `CannotPullContainerError` | ECR auth expired | Re-run `aws ecr get-login-password` |
| `price_history` table too large | No archival running | Run `archive_old_prices.py` |
| High memory usage | Browser per scrape | Verify browser pool in `scraper_worker.py` |

---

## 🎯 Quick Start Cheat Sheet

```bash
# 1. Setup (one-time)
git clone https://github.com/mukro/price_comparision_engine.git
cd price_comparision_engine
git checkout -b v2-production
unzip price_engine_v2_improvements.zip -d ./
cp .env.example .env
# Edit .env with real values

# 2. Build & Run
docker compose up -d

# 3. Migrate
docker compose exec -T db psql -U price_engine -d price_comparison < scripts/migrate_v1_to_v2.sql

# 4. Verify
curl http://localhost:8000/health
curl http://localhost:8000/docs

# 5. Test
pytest tests/test_compliance.py -v
locust -f tests/load/locustfile.py --host=http://localhost:8000

# 6. Deploy (AWS)
cd infra/aws && terraform apply
```

---

## 📞 Need Help?

- **API Docs:** `http://localhost:8000/docs`
- **Health Check:** `http://localhost:8000/health`
- **Celery Monitor:** `celery -A app.celery_app:celery_app flower` (install flower)
- **Logs:** `docker compose logs -f [service]`
- **DB Console:** `docker compose exec db psql -U price_engine -d price_comparison`
