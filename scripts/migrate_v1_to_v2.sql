-- scripts/migrate_v1_to_v2.sql
-- ==========================================
-- Migration: v1 (original) -> v2 (improved)
-- Run this against your existing database.
-- All statements are idempotent (safe to re-run).
-- ==========================================

BEGIN;

-- ==========================================
-- 1. Vendors: compliance & scraping columns
-- ==========================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='vendors' AND column_name='scraping_allowed') THEN
        ALTER TABLE vendors ADD COLUMN scraping_allowed BOOLEAN DEFAULT TRUE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='vendors' AND column_name='scrape_rpm') THEN
        ALTER TABLE vendors ADD COLUMN scrape_rpm INTEGER DEFAULT 6;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='vendors' AND column_name='stock_selector') THEN
        ALTER TABLE vendors ADD COLUMN stock_selector VARCHAR(255);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='vendors' AND column_name='stock_text_present') THEN
        ALTER TABLE vendors ADD COLUMN stock_text_present VARCHAR(50) DEFAULT 'in stock';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='vendors' AND column_name='updated_at') THEN
        ALTER TABLE vendors ADD COLUMN updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW();
    END IF;
END $$;

-- ==========================================
-- 2. New table: Scrape Dead Letter Queue
-- ==========================================
CREATE TABLE IF NOT EXISTS scrape_dlq (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    url TEXT NOT NULL,
    error_message TEXT,
    payload TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ==========================================
-- 3. New table: Match Feedback (for threshold tuning)
-- ==========================================
CREATE TABLE IF NOT EXISTS match_feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    offer_id UUID REFERENCES vendor_offers(id) ON DELETE CASCADE,
    admin_decision VARCHAR(20) NOT NULL CHECK (admin_decision IN ('approved', 'rejected', 'corrected')),
    original_confidence NUMERIC(4, 3),
    corrected_product_id UUID REFERENCES products(id),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ==========================================
-- 4. New table: Merchant API Keys (for row-level auth)
-- ==========================================
CREATE TABLE IF NOT EXISTS merchant_api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id VARCHAR(255) NOT NULL UNIQUE,
    api_key_hash VARCHAR(255) NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_used_at TIMESTAMP WITH TIME ZONE
);

-- ==========================================
-- 5. Performance indexes
-- ==========================================
CREATE INDEX IF NOT EXISTS idx_offers_stale_priority 
    ON vendor_offers(last_scraped_at ASC NULLS FIRST) 
    WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_scrape_dlq_created 
    ON scrape_dlq(created_at);

CREATE INDEX IF NOT EXISTS idx_match_feedback_offer 
    ON match_feedback(offer_id);

CREATE INDEX IF NOT EXISTS idx_merchant_rules_merchant 
    ON merchant_rules(merchant_id) 
    WHERE is_active = TRUE;

-- ==========================================
-- 6. Data backfill / defaults
-- ==========================================

-- Set all existing vendors to allow scraping by default
UPDATE vendors 
SET scraping_allowed = TRUE, 
    scrape_rpm = 6, 
    stock_text_present = 'in stock',
    updated_at = NOW()
WHERE scraping_allowed IS NULL;

-- Ensure all existing vendor_offers have a valid match_status
UPDATE vendor_offers 
SET match_status = 'matched' 
WHERE match_status IS NULL;

-- ==========================================
-- 7. Verify migration
-- ==========================================
DO $$
DECLARE
    vendor_cols INT;
    dlq_exists BOOLEAN;
    feedback_exists BOOLEAN;
BEGIN
    SELECT COUNT(*) INTO vendor_cols 
    FROM information_schema.columns 
    WHERE table_name = 'vendors' 
      AND column_name IN ('scraping_allowed', 'scrape_rpm', 'stock_selector', 'stock_text_present', 'updated_at');

    SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'scrape_dlq') INTO dlq_exists;
    SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'match_feedback') INTO feedback_exists;

    IF vendor_cols < 5 THEN
        RAISE EXCEPTION 'Migration incomplete: vendors table missing columns.';
    END IF;
    IF NOT dlq_exists THEN
        RAISE EXCEPTION 'Migration incomplete: scrape_dlq table missing.';
    END IF;
    IF NOT feedback_exists THEN
        RAISE EXCEPTION 'Migration incomplete: match_feedback table missing.';
    END IF;

    RAISE NOTICE 'Migration v1->v2 completed successfully.';
END $$;

COMMIT;
