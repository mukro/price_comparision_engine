-- Enable necessary extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";
CREATE EXTENSION IF NOT EXISTS "vector";

-- ==========================================
-- 1. Vendors Table
-- ==========================================
CREATE TABLE IF NOT EXISTS vendors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(100) NOT NULL,
    domain VARCHAR(100) NOT NULL UNIQUE,
    affiliate_tag VARCHAR(100),
    -- CSS selectors used by the Playwright scraper for this vendor's product pages.
    title_selector VARCHAR(255) DEFAULT '.product-title',
    price_selector VARCHAR(255) DEFAULT '.price',
    is_active BOOLEAN DEFAULT TRUE,
    respects_robots_txt BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ==========================================
-- 2. Master Products Table
-- ==========================================
CREATE TABLE IF NOT EXISTS products (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title VARCHAR(255) NOT NULL,
    brand VARCHAR(100),
    model_code VARCHAR(100),
    image_url TEXT,
    specifications JSONB DEFAULT '{}'::jsonb,
    title_embedding vector(384), -- local sentence-transformers (all-MiniLM-L6-v2)
    text_search_tsv tsvector,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Keep text_search_tsv in sync automatically (used by hybrid_product_search)
CREATE OR REPLACE FUNCTION products_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.text_search_tsv :=
        setweight(to_tsvector('english', coalesce(NEW.title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(NEW.brand, '')), 'B') ||
        setweight(to_tsvector('english', coalesce(NEW.model_code, '')), 'C');
    NEW.updated_at := NOW();
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_products_tsv ON products;
CREATE TRIGGER trg_products_tsv
    BEFORE INSERT OR UPDATE OF title, brand, model_code ON products
    FOR EACH ROW EXECUTE FUNCTION products_tsv_trigger();

-- ==========================================
-- 3. Vendor Offers Table
-- ==========================================
CREATE TABLE IF NOT EXISTS vendor_offers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    vendor_id UUID NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
    vendor_product_id VARCHAR(255) NOT NULL,
    raw_title VARCHAR(255) NOT NULL,
    product_url TEXT NOT NULL,
    affiliate_url TEXT,
    current_price NUMERIC(10, 2) NOT NULL,
    currency VARCHAR(3) DEFAULT 'USD',
    in_stock BOOLEAN DEFAULT TRUE,
    is_priority BOOLEAN DEFAULT FALSE, -- flagged "hot deal" items get scraped more often
    match_status VARCHAR(20) DEFAULT 'matched' CHECK (match_status IN ('matched', 'pending_review', 'rejected')),
    confidence_score NUMERIC(4, 3),
    last_scraped_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    CONSTRAINT unique_vendor_offer UNIQUE (vendor_id, vendor_product_id)
);

-- ==========================================
-- 4. Price History (append-only log of every scraped price point)
-- ==========================================
CREATE TABLE IF NOT EXISTS price_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    offer_id UUID NOT NULL REFERENCES vendor_offers(id) ON DELETE CASCADE,
    price NUMERIC(10, 2) NOT NULL,
    in_stock BOOLEAN DEFAULT TRUE,
    recorded_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ==========================================
-- 5. User Price Drop Alerts
-- ==========================================
CREATE TABLE IF NOT EXISTS user_alerts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_email VARCHAR(255) NOT NULL,
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    target_price NUMERIC(10, 2) NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    last_triggered_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    CONSTRAINT unique_user_product UNIQUE (user_email, product_id)
);

-- ==========================================
-- 6. Alert Audit Logs
-- ==========================================
CREATE TABLE IF NOT EXISTS alert_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_id UUID NOT NULL REFERENCES user_alerts(id) ON DELETE CASCADE,
    triggered_price NUMERIC(10, 2) NOT NULL,
    sent_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ==========================================
-- 7. B2B Merchant Dynamic Pricing Rules
-- ==========================================
CREATE TABLE IF NOT EXISTS merchant_rules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id VARCHAR(255) NOT NULL,
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    merchant_cost NUMERIC(10, 2) NOT NULL,
    min_margin_pct NUMERIC(5, 2) DEFAULT 15.00,
    map_price NUMERIC(10, 2) NULL,
    strategy VARCHAR(50) DEFAULT 'undercut_by_fixed',
    strategy_value NUMERIC(10, 2) DEFAULT 1.00,
    webhook_url TEXT NULL,
    auto_apply_enabled BOOLEAN DEFAULT FALSE,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(merchant_id, product_id)
);

-- ==========================================
-- 8. Price Audit Logs (every automated repricing decision, incl. circuit-breaker trips)
-- ==========================================
CREATE TABLE IF NOT EXISTS price_audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id VARCHAR(255) NOT NULL,
    product_id UUID NOT NULL REFERENCES products(id),
    old_price NUMERIC(10, 2) NOT NULL,
    new_price NUMERIC(10, 2) NOT NULL,
    trigger_event VARCHAR(100) NOT NULL,
    circuit_breaker_tripped BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ==========================================
-- 9. User Savings Telemetry
-- ==========================================
CREATE TABLE IF NOT EXISTS user_savings_telemetry (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id VARCHAR(255) NULL,
    product_id UUID NOT NULL REFERENCES products(id),
    original_price NUMERIC(10, 2) NOT NULL,
    purchased_price NUMERIC(10, 2) NOT NULL,
    savings_amount NUMERIC(10, 2) GENERATED ALWAYS AS (original_price - purchased_price) STORED,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ==========================================
-- Indexes
-- ==========================================
CREATE INDEX IF NOT EXISTS idx_products_title_trgm ON products USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_products_embedding ON products USING hnsw (title_embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_products_tsv ON products USING gin (text_search_tsv);
CREATE INDEX IF NOT EXISTS idx_offers_product_price ON vendor_offers(product_id, current_price);
CREATE INDEX IF NOT EXISTS idx_offers_stale ON vendor_offers(last_scraped_at) WHERE in_stock = TRUE;
CREATE INDEX IF NOT EXISTS idx_price_history_offer_time ON price_history(offer_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_user_alerts_product ON user_alerts(product_id) WHERE is_active = TRUE;
