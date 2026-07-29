# SPS_VS -- Rectified Build

This is a rebuilt, working version of the original upload. See
`SPS_VS_Code_Audit.md` (sent earlier) for the full list of findings; this
file just summarizes what changed and how to run it.

## Quick start

```bash
cp .env.example .env      # then fill in real values
docker compose up --build
```

- API: http://localhost:8000/docs
- Grafana: http://localhost:3000
- Prometheus: http://localhost:9090

Get an admin token first (uses `ADMIN_EMAIL`/`ADMIN_PASSWORD` from `.env`):
```bash
curl -X POST http://localhost:8000/api/v1/admin/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"changeme_use_a_strong_password"}'
```
Use the returned `access_token` as `Authorization: Bearer <token>` on `/api/v1/admin/*` and `/api/v1/merchant/*` routes.

## What changed, by category

**Fixed (app couldn't start before):**
- `app/api/admin.py` now defines its `router` and real endpoints (login, pending-matches, review-match), all with JWT auth actually enforced.
- `app/db.py` / `app/db_sync.py` replace the broken imports of a nonexistent `db_pool`, split cleanly into async (FastAPI) vs. pooled sync (Celery/core) layers.
- `app/config.py` now defines `JWT_SECRET_KEY`/`JWT_ALGORITHM`/etc.
- `app/tasks.py` and `app/celery_app.py` rebuilt so Celery actually boots (removed the `@app.task` NameError and the imports of nonexistent `scraper`/`extras` modules).
- `docker-compose.yml` — renamed from `docker-composer.yml`, `prometheus`/`grafana` correctly nested under `services:`.
- `Dockerfile` — renamed from `DockerFile` (case mismatch would fail the build on Linux).

**Schema fixes:**
- Added the missing `price_history` table (the old `/history` endpoint queried a table that never existed).
- Alert notifications now query `user_alerts` (the actual table) instead of the nonexistent `price_alerts`.
- Added `text_search_tsv` (kept in sync via trigger) so hybrid search works.
- Added `vendors.title_selector` / `price_selector` / `is_active`, and `vendor_offers.is_priority`, needed by the real scraper.

**The core feature that was a stub is now real:**
- Celery Beat now schedules the actual Playwright scraper (`app/workers/scraper_worker.py` + `app/tasks.py: process_vendor_scrape`) instead of the old mock that inserted a hardcoded "Sony WH-1000XM5 @ $348" for every product.
- Price-drop emails are now actually sent via `email_worker.send_price_drop_email` instead of a `print()` stub.
- The repricing circuit breaker is now called from the merchant evaluation flow before webhooks fire.
- Every price update now writes a `price_history` row, powering the `/history` endpoint and buy-timing insights.

**Security:**
- Admin and merchant endpoints require a valid admin JWT (previously open to anyone).
- CORS origins are now configurable via `ALLOWED_ORIGINS` instead of `["*"]` + credentials.
- All credentials moved to `.env` (see `.env.example`); nothing is hardcoded in source or committed compose files.
- **Please rotate the SendGrid/DB/Redis credentials from the original uploaded `.env`** if that file has been shared or committed anywhere.

**Removed (dead/duplicate code):**
- Top-level `celery/` directory (unused legacy drafts with a hardcoded plaintext DB password).
- `app/scheduler.py` (disconnected third scraping path, hardcoded proxy credentials).
- `app/api/alerts_api.py` (dead duplicate of `alerts.py` -- merged into one router with create + delete).
- `app/core/cache.py` (dead duplicate of `app/cache.py` that awaited a non-async Redis client).
- `app/tasks_scraper.py` and `app/workers/worker.py` (merged into `app/workers/scraper_worker.py` / `app/tasks.py`).
- The hardcoded-mock `scrape_single_vendor_product` and the no-op `save_price_to_db` print stub.

**Performance:**
- All Celery/core DB access now goes through a pooled `psycopg2` connection pool (`app/db_sync.py`) instead of opening a new connection per call.
- The sentence-transformers model now lazy-loads on first use rather than at import time, so it never loads inside the API process.

## What I left as-is / didn't touch

- `app/frontend/*.tsx` — unchanged. **Note:** `AdminReviewQueue.tsx` calls `/admin/pending-matches` and `/admin/review-match` without an `Authorization` header, so it will now get `401`s until it's updated to attach the admin bearer token from the login flow above.
- `app/frontend/arch_summary.txt` mentions OpenAI embeddings; the actual (and rebuilt) implementation uses local `sentence-transformers` (`all-MiniLM-L6-v2`), matching the `vector(384)` column -- that doc was aspirational/stale, not a bug in code.
- The single-admin login (`ADMIN_EMAIL`/`ADMIN_PASSWORD` in settings) is dev/demo-grade. Before real use, replace with a proper `users` table, hashed passwords, and per-merchant scoped auth for the `/merchant/*` routes (right now any admin can see/edit any merchant's rules).
- Legal/scraping-compliance posture (honest User-Agent, no stealth evasion) -- see the audit doc; still recommend a robots.txt check and per-domain rate limiting before scraping vendors at scale, and legal review of your vendor list.
