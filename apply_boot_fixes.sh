#!/bin/bash
# ==========================================================
# PCE Backend Boot Fix Script
# Run this from the repo root:  ./apply_boot_fixes.sh
# ==========================================================

set -e

echo "[1/6] Applying tasks_new.py fixes..."
cp app/tasks_new.py app/tasks_new.py.bak
cp /mnt/agents/output/tasks_new.py app/tasks_new.py

echo "[2/6] Creating app/database.py..."
cp /mnt/agents/output/database.py app/database.py

echo "[3/6] Applying config.py fixes..."
cp app/config.py app/config.py.bak
cp /mnt/agents/output/config.py app/config.py

echo "[4/6] Applying celery_app.py fixes..."
cp app/celery_app.py app/celery_app.py.bak
cp /mnt/agents/output/celery_app.py app/celery_app.py

echo "[5/6] Applying requirements.txt fixes..."
cp requirements.txt requirements.txt.bak
cp /mnt/agents/output/requirements.txt requirements.txt

echo "[6/6] Creating .env.example..."
cp /mnt/agents/output/.env.example .env.example

echo ""
echo "✅ All fixes applied. Backups saved as *.bak"
echo ""
echo "Next steps:"
echo "  1. cp .env.example .env   # then edit with real secrets"
echo "  2. pip install -r requirements.txt"
echo "  3. docker compose up -d db redis"
echo "  4. uvicorn app.main:app --reload"
echo "  5. celery -A app.celery_app:celery_app worker -Q default --loglevel=info"
echo "  6. celery -A app.celery_app:celery_app worker -Q high_priority --loglevel=info"
echo "  7. celery -A app.celery_app:celery_app beat --loglevel=info"
