-- ==============================================================================
-- PCE Database Initialization Script
-- Run this FIRST before schema.sql to enable all required extensions
-- ==============================================================================

-- ==============================================================================
-- 1. CORE EXTENSIONS
-- ==============================================================================

-- UUID generation
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Fuzzy text search (trigram similarity)
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- Vector embeddings for AI semantic search (pgvector)
CREATE EXTENSION IF NOT EXISTS "vector";

-- HStore for key-value storage (used by some ORMs)
CREATE EXTENSION IF NOT EXISTS "hstore";

-- ==============================================================================
-- 2. CUSTOM TYPES
-- ==============================================================================

-- Affiliate click status
DO $$ BEGIN
    CREATE TYPE affiliate_status AS ENUM ('clicked', 'converted', 'expired', 'disputed', 'unmatched');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Notification status
DO $$ BEGIN
    CREATE TYPE notification_status AS ENUM ('pending', 'sent', 'delivered', 'failed');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Notification priority
DO $$ BEGIN
    CREATE TYPE notification_priority AS ENUM ('normal', 'high');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Wallet transaction type
DO $$ BEGIN
    CREATE TYPE wallet_txn_type AS ENUM ('credit', 'debit', 'refund');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Merchant claim verification method
DO $$ BEGIN
    CREATE TYPE verification_method AS ENUM ('dns_txt', 'email', 'manual_review');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Merchant claim status
DO $$ BEGIN
    CREATE TYPE claim_status AS ENUM ('pending', 'verified', 'rejected');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Offer source
DO $$ BEGIN
    CREATE TYPE offer_source AS ENUM ('scraped', 'partner_feed', 'merchant_api', 'ocr_submission');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- OCR submission status
DO $$ BEGIN
    CREATE TYPE ocr_status AS ENUM ('pending', 'approved', 'rejected');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ==============================================================================
-- 3. DATABASE CONFIGURATION
-- ==============================================================================

-- Optimize for analytical workloads (price history, click tracking)
ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements';
ALTER SYSTEM SET work_mem = '64MB';
ALTER SYSTEM SET maintenance_work_mem = '512MB';
ALTER SYSTEM SET effective_cache_size = '2GB';

-- Enable query statistics extension
CREATE EXTENSION IF NOT EXISTS "pg_stat_statements";

-- ==============================================================================
-- 4. HELPER FUNCTIONS
-- ==============================================================================

-- Auto-update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Auto-update text search vector for products
CREATE OR REPLACE FUNCTION products_tsv_trigger()
RETURNS TRIGGER AS $$
BEGIN
    NEW.text_search_tsv :=
        setweight(to_tsvector('english', COALESCE(NEW.title, '')), 'A') ||
        setweight(to_tsvector('english', COALESCE(NEW.brand, '')), 'B') ||
        setweight(to_tsvector('english', COALESCE(NEW.model_code, '')), 'C');
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Generate unique click tracking ID
CREATE OR REPLACE FUNCTION generate_click_id()
RETURNS VARCHAR(64) AS $$
DECLARE
    chars TEXT := 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
    result VARCHAR(64) := '';
    i INTEGER := 0;
BEGIN
    FOR i IN 1..32 LOOP
        result := result || substr(chars, floor(random() * length(chars) + 1)::int, 1);
    END LOOP;
    RETURN result;
END;
$$ LANGUAGE plpgsql;

-- Calculate price drop percentage
CREATE OR REPLACE FUNCTION calculate_price_drop(old_price NUMERIC, new_price NUMERIC)
RETURNS NUMERIC AS $$
BEGIN
    IF old_price IS NULL OR old_price = 0 THEN
        RETURN 0;
    END IF;
    RETURN ROUND(((old_price - new_price) / old_price) * 100, 2);
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- Check if a date falls within an Indian sale period
CREATE OR REPLACE FUNCTION is_sale_period(check_date DATE DEFAULT CURRENT_DATE)
RETURNS BOOLEAN AS $$
BEGIN
    RETURN check_date IN (
        -- Republic Day
        SELECT generate_series(
            make_date(EXTRACT(YEAR FROM check_date)::int, 1, 23),
            make_date(EXTRACT(YEAR FROM check_date)::int, 1, 29),
            INTERVAL '1 day'
        )::date
        UNION
        -- Independence Day / Prime Day
        SELECT generate_series(
            make_date(EXTRACT(YEAR FROM check_date)::int, 8, 12),
            make_date(EXTRACT(YEAR FROM check_date)::int, 8, 20),
            INTERVAL '1 day'
        )::date
        UNION
        -- Diwali / Big Billion Days
        SELECT generate_series(
            make_date(EXTRACT(YEAR FROM check_date)::int, 10, 15),
            make_date(EXTRACT(YEAR FROM check_date)::int, 11, 5),
            INTERVAL '1 day'
        )::date
        UNION
        -- Black Friday
        SELECT generate_series(
            make_date(EXTRACT(YEAR FROM check_date)::int, 11, 27),
            make_date(EXTRACT(YEAR FROM check_date)::int, 12, 2),
            INTERVAL '1 day'
        )::date
        UNION
        -- Year End
        SELECT generate_series(
            make_date(EXTRACT(YEAR FROM check_date)::int, 12, 20),
            make_date(EXTRACT(YEAR FROM check_date)::int, 12, 31),
            INTERVAL '1 day'
        )::date
    );
END;
$$ LANGUAGE plpgsql IMMUTABLE;
