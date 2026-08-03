-- ==============================================================================
-- PCE — Complete Database Schema v2.0
-- Monetization + AI/ML Ready
-- Requires: PostgreSQL 16 + pgvector + pg_trgm + uuid-ossp
-- Run AFTER init.sql
-- ==============================================================================

-- ==============================================================================
-- 1. VENDORS (Merchant / Domain Registry)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS vendors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    domain VARCHAR(255) NOT NULL UNIQUE,
    affiliate_tag VARCHAR(100),
    -- Scraping selectors
    title_selector VARCHAR(500) DEFAULT '.product-title',
    price_selector VARCHAR(500) DEFAULT '.price',
    stock_selector VARCHAR(500),
    stock_text_present VARCHAR(100) DEFAULT 'in stock',
    currency VARCHAR(3) DEFAULT 'INR',
    is_active BOOLEAN DEFAULT TRUE,
    respects_robots_txt BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE vendors IS 'Registered e-commerce domains with scraping configuration';
COMMENT ON COLUMN vendors.domain IS 'Unique domain identifier, also used as merchant_id for partner feeds';

-- ==============================================================================
-- 2. PRODUCTS (Master Catalog)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS products (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title VARCHAR(500) NOT NULL,
    brand VARCHAR(255),
    model_code VARCHAR(255),
    image_url TEXT,
    specifications JSONB DEFAULT '{}',
    category VARCHAR(255),
    subcategory VARCHAR(255),
    -- AI: Vector embedding for semantic search (all-MiniLM-L6-v2 = 384d)
    title_embedding VECTOR(384),
    -- Full-text search vector (auto-maintained by trigger)
    text_search_tsv TSVECTOR,
    -- AI: Product popularity score (updated by batch job)
    popularity_score NUMERIC(5, 4) DEFAULT 0.5,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE products IS 'Master product catalog with AI embeddings for semantic search';
COMMENT ON COLUMN products.title_embedding IS '384-dim vector from sentence-transformers/all-MiniLM-L6-v2';
COMMENT ON COLUMN products.popularity_score IS 'Normalized 0-1 score based on click volume and conversion rate';

-- Auto-update text search vector and timestamp
DROP TRIGGER IF EXISTS trg_products_tsv ON products;
CREATE TRIGGER trg_products_tsv
    BEFORE INSERT OR UPDATE OF title, brand, model_code ON products
    FOR EACH ROW EXECUTE FUNCTION products_tsv_trigger();

-- ==============================================================================
-- 3. VENDOR OFFERS (Price listings per product per vendor)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS vendor_offers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    vendor_id UUID NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
    vendor_product_id VARCHAR(255) NOT NULL,
    merchant_id VARCHAR(255),              -- NULL for scraped, set for partner feeds
    raw_title VARCHAR(500) NOT NULL,
    current_price NUMERIC(12, 2) NOT NULL,
    mrp NUMERIC(12, 2),                    -- Maximum Retail Price
    currency VARCHAR(3) DEFAULT 'INR',
    in_stock BOOLEAN DEFAULT TRUE,
    product_url TEXT NOT NULL,
    affiliate_url TEXT,
    -- Legacy priority flag
    is_priority BOOLEAN DEFAULT FALSE,
    -- Monetization: Sponsored listing
    is_sponsored BOOLEAN DEFAULT FALSE,
    sponsor_bid_id UUID,
    sponsor_rank_boost INT DEFAULT 0,
    -- Source tracking
    source offer_source DEFAULT 'scraped',
    partner_key_id UUID,
    feed_updated_at TIMESTAMPTZ,
    -- Analytics: Click & conversion counters
    click_count INT DEFAULT 0,
    conversion_count INT DEFAULT 0,
    conversion_revenue NUMERIC(12, 2) DEFAULT 0,
    -- Matching quality
    match_status VARCHAR(20) DEFAULT 'matched' CHECK (match_status IN ('matched', 'pending_review', 'rejected')),
    confidence_score NUMERIC(4, 3),
    last_scraped_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(vendor_id, vendor_product_id)
);

COMMENT ON TABLE vendor_offers IS 'Individual price listings from vendors. One product can have multiple offers.';
COMMENT ON COLUMN vendor_offers.merchant_id IS 'Set when data comes from partner feed instead of scraping';
COMMENT ON COLUMN vendor_offers.is_sponsored IS 'Paid placement — merchant bid for premium ranking';
COMMENT ON COLUMN vendor_offers.source IS 'Data provenance: scraped, partner_feed, merchant_api, ocr_submission';

-- ==============================================================================
-- 4. PRICE HISTORY (Time-series — PARTITIONED for scale)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS price_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    offer_id UUID NOT NULL REFERENCES vendor_offers(id) ON DELETE CASCADE,
    price NUMERIC(12, 2) NOT NULL,
    in_stock BOOLEAN DEFAULT TRUE,
    recorded_at TIMESTAMPTZ DEFAULT NOW()
) PARTITION BY RANGE (recorded_at);

-- Create monthly partitions for current and next year
-- In production, use pg_partman or a cron job to create future partitions
DO $$
DECLARE
    start_date DATE;
    end_date DATE;
    partition_name TEXT;
BEGIN
    FOR i IN 0..13 LOOP
        start_date := DATE_TRUNC('month', CURRENT_DATE + (i || ' months')::interval);
        end_date := start_date + INTERVAL '1 month';
        partition_name := 'price_history_' || TO_CHAR(start_date, 'YYYY_MM');

        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I PARTITION OF price_history
             FOR VALUES FROM (%L) TO (%L)',
            partition_name, start_date, end_date
        );

        -- Create BRIN index on each partition (efficient for time-series)
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON %I USING BRIN (recorded_at)',
            partition_name || '_brin', partition_name
        );
    END LOOP;
END $$;

COMMENT ON TABLE price_history IS 'Append-only time-series of all price changes. Partitioned monthly for query performance.';

-- ==============================================================================
-- 5. USER AUTHENTICATION (Foundation layer)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) UNIQUE NOT NULL,
    phone VARCHAR(20) UNIQUE,
    password_hash VARCHAR(255),
    auth_provider VARCHAR(50) DEFAULT 'local' CHECK (auth_provider IN ('local', 'google', 'phone_otp', 'apple')),
    auth_provider_id VARCHAR(255),
    full_name VARCHAR(255),
    avatar_url TEXT,
    fcm_token TEXT,                        -- Firebase Cloud Messaging token
    device_hashes TEXT[] DEFAULT '{}',
    is_active BOOLEAN DEFAULT TRUE,
    email_verified BOOLEAN DEFAULT FALSE,
    phone_verified BOOLEAN DEFAULT FALSE,
    -- Analytics
    last_login_at TIMESTAMPTZ,
    signup_source VARCHAR(50),             -- 'organic', 'referral', 'ad_campaign'
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE users IS 'End-user accounts. Email is primary key for local auth.';
COMMENT ON COLUMN users.device_hashes IS 'Array of SHA256 device hashes for cross-session attribution';

-- ==============================================================================
-- 6. AFFILIATE CLICK TRACKING (Revenue proof)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS affiliate_clicks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    click_id VARCHAR(64) UNIQUE NOT NULL DEFAULT generate_click_id(),
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    device_hash VARCHAR(64) NOT NULL,
    offer_id UUID NOT NULL,
    product_id UUID NOT NULL,
    vendor_id UUID NOT NULL,
    merchant_id VARCHAR(255),
    landing_url TEXT NOT NULL,
    ip_address INET,
    user_agent TEXT,
    referrer TEXT,
    -- Geo data (for analytics)
    country_code VARCHAR(2),
    city VARCHAR(100),
    clicked_at TIMESTAMPTZ DEFAULT NOW(),
    converted_at TIMESTAMPTZ,
    conversion_value NUMERIC(12, 2),
    commission_earned NUMERIC(12, 2),
    status affiliate_status DEFAULT 'clicked',
    -- AI: Fraud detection
    fraud_score NUMERIC(3, 2) DEFAULT 0,
    fraud_flags TEXT[] DEFAULT '{}'
) PARTITION BY RANGE (clicked_at);

-- Monthly partitions for affiliate clicks
DO $$
DECLARE
    start_date DATE;
    end_date DATE;
    partition_name TEXT;
BEGIN
    FOR i IN 0..13 LOOP
        start_date := DATE_TRUNC('month', CURRENT_DATE + (i || ' months')::interval);
        end_date := start_date + INTERVAL '1 month';
        partition_name := 'affiliate_clicks_' || TO_CHAR(start_date, 'YYYY_MM');

        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I PARTITION OF affiliate_clicks
             FOR VALUES FROM (%L) TO (%L)',
            partition_name, start_date, end_date
        );

        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON %I USING BRIN (clicked_at)',
            partition_name || '_brin', partition_name
        );
    END LOOP;
END $$;

COMMENT ON TABLE affiliate_clicks IS 'Every outbound click tracked with device fingerprinting. Core attribution table.';
COMMENT ON COLUMN affiliate_clicks.device_hash IS 'SHA256 of device ID — used for cross-session conversion matching';
COMMENT ON COLUMN affiliate_clicks.fraud_score IS 'ML model score: 0 = clean, 1 = definitely fraudulent';

-- ==============================================================================
-- 7. USER WATCHLIST (Retention engine)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS user_watchlist (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    target_price NUMERIC(12, 2),           -- NULL = notify on any drop
    notify_push BOOLEAN DEFAULT TRUE,
    notify_email BOOLEAN DEFAULT FALSE,
    last_notified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, product_id)
);

COMMENT ON TABLE user_watchlist IS 'Products users want price alerts for. Drives push notification re-engagement.';

-- ==============================================================================
-- 8. PUSH NOTIFICATION QUEUE (FCM integration)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS push_notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    fcm_token TEXT NOT NULL,
    title VARCHAR(255) NOT NULL,
    body TEXT NOT NULL,
    data JSONB DEFAULT '{}',               -- {product_id, offer_id, type: 'price_drop'}
    priority notification_priority DEFAULT 'normal',
    scheduled_at TIMESTAMPTZ DEFAULT NOW(),
    sent_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    failed_at TIMESTAMPTZ,
    error_message TEXT,
    status notification_status DEFAULT 'pending',
    retry_count INT DEFAULT 0
);

COMMENT ON TABLE push_notifications IS 'Outbound FCM notification queue. Workers poll pending rows.';

-- ==============================================================================
-- 9. USER ALERTS (Legacy email-based alerts)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS user_alerts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_email VARCHAR(255) NOT NULL,
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    target_price NUMERIC(12, 2) NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    last_triggered_at TIMESTAMPTZ,
    -- NEW: Link to authenticated users
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    notify_push BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_email, product_id)
);

COMMENT ON TABLE user_alerts IS 'Legacy email alerts. Migrating to user_watchlist for push notifications.';

-- ==============================================================================
-- 10. ALERT LOGS (Audit trail)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS alert_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_id UUID NOT NULL REFERENCES user_alerts(id) ON DELETE CASCADE,
    triggered_price NUMERIC(12, 2) NOT NULL,
    sent_at TIMESTAMPTZ DEFAULT NOW()
);

-- ==============================================================================
-- 11. MERCHANT RULES (B2B Dynamic Pricing Engine)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS merchant_rules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id VARCHAR(255) NOT NULL,
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    merchant_cost NUMERIC(12, 2) NOT NULL,
    min_margin_pct NUMERIC(5, 2) DEFAULT 15.00,
    map_price NUMERIC(12, 2),
    strategy VARCHAR(50) DEFAULT 'undercut_by_fixed' CHECK (strategy IN ('undercut_by_fixed', 'undercut_by_pct', 'match_lowest', 'premium_fixed', 'dynamic_margin')),
    strategy_value NUMERIC(12, 2) DEFAULT 1.00,
    webhook_url TEXT,
    auto_apply_enabled BOOLEAN DEFAULT FALSE,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(merchant_id, product_id)
);

COMMENT ON TABLE merchant_rules IS 'B2B pricing rules for merchant partners. Circuit breaker protects margins.';

-- ==============================================================================
-- 12. PRICE AUDIT LOGS (Circuit breaker tracking)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS price_audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id VARCHAR(255) NOT NULL,
    product_id UUID NOT NULL REFERENCES products(id),
    old_price NUMERIC(12, 2) NOT NULL,
    new_price NUMERIC(12, 2) NOT NULL,
    trigger_event VARCHAR(100) NOT NULL,
    circuit_breaker_tripped BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ==============================================================================
-- 13. USER SAVINGS TELEMETRY
-- ==============================================================================
CREATE TABLE IF NOT EXISTS user_savings_telemetry (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id VARCHAR(255),
    product_id UUID NOT NULL REFERENCES products(id),
    original_price NUMERIC(12, 2) NOT NULL,
    purchased_price NUMERIC(12, 2) NOT NULL,
    savings_amount NUMERIC(12, 2) GENERATED ALWAYS AS (original_price - purchased_price) STORED,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ==============================================================================
-- 14. OCR SUBMISSIONS (Community + AI-enhanced)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS ocr_submissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_hash VARCHAR(64) NOT NULL,
    product_name VARCHAR(500),
    brand VARCHAR(255),
    price NUMERIC(12, 2),
    currency VARCHAR(3) DEFAULT 'INR',
    mrp NUMERIC(12, 2),
    discount_pct NUMERIC(5, 2),
    vendor_domain VARCHAR(255),
    in_stock BOOLEAN,
    -- AI: MLKit raw confidence
    ocr_confidence NUMERIC(3, 2),
    ocr_engine VARCHAR(50),
    -- AI: LLM-extracted structured data
    ai_extracted_data JSONB,
    ai_confidence NUMERIC(3, 2),
    extraction_method VARCHAR(20) DEFAULT 'mlkit',
    -- AI: Auto-matched product
    matched_product_id UUID REFERENCES products(id),
    match_confidence NUMERIC(3, 2),
    match_method VARCHAR(20),
    -- AI: Fraud detection
    fraud_score NUMERIC(3, 2) DEFAULT 0,
    fraud_flags TEXT[] DEFAULT '{}',
    -- Metadata
    device_os VARCHAR(50),
    app_version VARCHAR(20),
    status ocr_status DEFAULT 'pending',
    reviewed_by UUID,
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE ocr_submissions IS 'Community-contributed price data via mobile OCR. AI-enhanced extraction and fraud detection.';
COMMENT ON COLUMN ocr_submissions.ai_extracted_data IS 'JSON output from LLM (GPT-4o-mini) structured extraction';
COMMENT ON COLUMN ocr_submissions.fraud_score IS 'ML model: bot detection, price outlier, submission velocity';

-- ==============================================================================
-- 15. PENDING MATCHES (Product deduplication queue)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS pending_matches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ocr_submission_id UUID REFERENCES ocr_submissions(id),
    suggested_product_id UUID REFERENCES products(id),
    confidence_score NUMERIC(5, 4),
    status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    reviewed_by UUID,
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ==============================================================================
-- 16. PARTNER API KEYS (Tier 3 Merchant Moat)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS partner_api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id VARCHAR(255) NOT NULL,
    api_key_hash VARCHAR(255) NOT NULL,
    api_key_prefix VARCHAR(8) NOT NULL,
    name VARCHAR(255) NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    rate_limit_rpm INT DEFAULT 60 CHECK (rate_limit_rpm > 0 AND rate_limit_rpm <= 10000),
    allowed_ips INET[],
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(merchant_id, name)
);

COMMENT ON TABLE partner_api_keys IS 'API keys for partner feed access. Hash stored, prefix shown for identification.';

-- ==============================================================================
-- 17. PARTNER FEED LOGS (Audit trail)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS partner_feed_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    partner_key_id UUID NOT NULL REFERENCES partner_api_keys(id),
    merchant_id VARCHAR(255) NOT NULL,
    products_received INT DEFAULT 0,
    products_accepted INT DEFAULT 0,
    products_rejected INT DEFAULT 0,
    errors JSONB DEFAULT '[]',
    processed_at TIMESTAMPTZ DEFAULT NOW()
);

-- ==============================================================================
-- 18. MERCHANT CLAIMS (Trust layer)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS merchant_claims (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id VARCHAR(255) NOT NULL,
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    domain VARCHAR(255) NOT NULL,
    verification_method verification_method NOT NULL,
    verification_token VARCHAR(255) NOT NULL,
    verified_at TIMESTAMPTZ,
    status claim_status DEFAULT 'pending',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE merchant_claims IS 'Merchants claim ownership of product listings via DNS TXT or email verification.';

-- ==============================================================================
-- 19. SPONSORED PLACEMENTS (Revenue engine)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS sponsored_placements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id VARCHAR(255) NOT NULL,
    product_id UUID REFERENCES products(id) ON DELETE CASCADE,
    keyword VARCHAR(255),
    bid_amount NUMERIC(12, 4) NOT NULL CHECK (bid_amount > 0),
    daily_budget NUMERIC(12, 2) NOT NULL CHECK (daily_budget > 0),
    daily_spend NUMERIC(12, 2) DEFAULT 0 CHECK (daily_spend <= daily_budget),
    total_spend NUMERIC(12, 2) DEFAULT 0,
    total_clicks INT DEFAULT 0,
    total_impressions INT DEFAULT 0,
    start_date DATE NOT NULL,
    end_date DATE,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT valid_date_range CHECK (end_date IS NULL OR end_date >= start_date)
);

COMMENT ON TABLE sponsored_placements IS 'CPC auction engine. Merchants bid for search result placement.';
COMMENT ON COLUMN sponsored_placements.daily_spend IS 'Auto-reset to 0 daily by Celery Beat. Auto-pause when >= daily_budget.';

-- ==============================================================================
-- 20. MERCHANT WALLETS
-- ==============================================================================
CREATE TABLE IF NOT EXISTS merchant_wallets (
    merchant_id VARCHAR(255) PRIMARY KEY,
    balance NUMERIC(12, 2) DEFAULT 0 CHECK (balance >= 0),
    auto_recharge_enabled BOOLEAN DEFAULT FALSE,
    auto_recharge_threshold NUMERIC(12, 2) DEFAULT 1000,
    auto_recharge_amount NUMERIC(12, 2) DEFAULT 5000,
    currency VARCHAR(3) DEFAULT 'INR',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ==============================================================================
-- 21. WALLET TRANSACTIONS
-- ==============================================================================
CREATE TABLE IF NOT EXISTS wallet_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id VARCHAR(255) NOT NULL REFERENCES merchant_wallets(merchant_id),
    type wallet_txn_type NOT NULL,
    amount NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
    description TEXT,
    reference_id VARCHAR(255),             -- e.g. click_id, invoice_id
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ==============================================================================
-- 22. PRICE PREDICTIONS (AI/ML)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS price_predictions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    vendor_id UUID REFERENCES vendors(id) ON DELETE CASCADE,
    predicted_price NUMERIC(12, 2),
    confidence NUMERIC(3, 2) CHECK (confidence >= 0 AND confidence <= 1),
    recommendation VARCHAR(10) CHECK (recommendation IN ('BUY', 'WAIT', 'HOLD', 'INSUFFICIENT_DATA')),
    expected_drop_pct NUMERIC(5, 2),
    best_buy_window TEXT,
    price_trend VARCHAR(20),
    volatility NUMERIC(5, 3),
    model_used VARCHAR(50) DEFAULT 'prophet_v1',
    predicted_at TIMESTAMPTZ DEFAULT NOW(),
    valid_until TIMESTAMPTZ DEFAULT NOW() + INTERVAL '24 hours',
    UNIQUE(product_id, vendor_id, DATE(predicted_at))
);

COMMENT ON TABLE price_predictions IS 'AI-generated price forecasts using Prophet/LSTM. Cached for 24h.';
COMMENT ON COLUMN price_predictions.confidence IS '0-1 score based on prediction interval width';

-- ==============================================================================
-- 23. SEARCH QUERIES (Analytics + AI training data)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS search_queries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query_text VARCHAR(500) NOT NULL,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    device_hash VARCHAR(64),
    intent_classified VARCHAR(50),         -- price_compare, deal_hunt, buy_ready, etc.
    intent_confidence NUMERIC(3, 2),
    results_count INT,
    clicked_product_id UUID,
    clicked_position INT,                  -- Which result position was clicked
    response_time_ms INT,                  -- Search latency
    created_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE search_queries IS 'Search analytics for improving ranking and training intent classifier.';

-- ==============================================================================
-- TRIGGERS
-- ==============================================================================

-- Products
DROP TRIGGER IF EXISTS trg_products_updated_at ON products;
CREATE TRIGGER trg_products_updated_at
    BEFORE UPDATE ON products FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Vendor Offers
DROP TRIGGER IF EXISTS trg_vendor_offers_updated_at ON vendor_offers;
CREATE TRIGGER trg_vendor_offers_updated_at
    BEFORE UPDATE ON vendor_offers FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Merchant Rules
DROP TRIGGER IF EXISTS trg_merchant_rules_updated_at ON merchant_rules;
CREATE TRIGGER trg_merchant_rules_updated_at
    BEFORE UPDATE ON merchant_rules FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Users
DROP TRIGGER IF EXISTS trg_users_updated_at ON users;
CREATE TRIGGER trg_users_updated_at
    BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Merchant Wallets
DROP TRIGGER IF EXISTS trg_merchant_wallets_updated_at ON merchant_wallets;
CREATE TRIGGER trg_merchant_wallets_updated_at
    BEFORE UPDATE ON merchant_wallets FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Sponsored Placements
DROP TRIGGER IF EXISTS trg_sponsored_placements_updated_at ON sponsored_placements;
CREATE TRIGGER trg_sponsored_placements_updated_at
    BEFORE UPDATE ON sponsored_placements FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ==============================================================================
-- INDEXES (Optimized for production workloads)
-- ==============================================================================

-- -------------------------------
-- PRODUCTS indexes
-- -------------------------------
CREATE INDEX IF NOT EXISTS idx_products_title_trgm ON products USING gin(title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_products_brand ON products(brand);
CREATE INDEX IF NOT EXISTS idx_products_model ON products(model_code);
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
CREATE INDEX IF NOT EXISTS idx_products_subcategory ON products(subcategory);
CREATE INDEX IF NOT EXISTS idx_products_popularity ON products(popularity_score DESC) WHERE popularity_score > 0.3;

-- HNSW index for vector similarity (faster than IVFFlat for high-dimensional vectors)
-- Requires: CREATE EXTENSION vector (already done in init.sql)
CREATE INDEX IF NOT EXISTS idx_products_embedding_hnsw ON products 
    USING hnsw (title_embedding vector_cosine_ops) 
    WITH (m = 16, ef_construction = 64);

-- Full-text search GIN index
CREATE INDEX IF NOT EXISTS idx_products_tsv ON products USING gin(text_search_tsv);

-- Composite: category + popularity (for category browsing)
CREATE INDEX IF NOT EXISTS idx_products_category_popular ON products(category, popularity_score DESC) 
    WHERE popularity_score > 0.3;

-- -------------------------------
-- VENDORS indexes
-- -------------------------------
CREATE INDEX IF NOT EXISTS idx_vendors_domain ON vendors(domain);
CREATE INDEX IF NOT EXISTS idx_vendors_active ON vendors(is_active) WHERE is_active = TRUE;

-- -------------------------------
-- VENDOR OFFERS indexes
-- -------------------------------
CREATE INDEX IF NOT EXISTS idx_offers_product ON vendor_offers(product_id);
CREATE INDEX IF NOT EXISTS idx_offers_vendor ON vendor_offers(vendor_id);
CREATE INDEX IF NOT EXISTS idx_offers_product_price ON vendor_offers(product_id, current_price);
CREATE INDEX IF NOT EXISTS idx_offers_price ON vendor_offers(current_price);
CREATE INDEX IF NOT EXISTS idx_offers_stock ON vendor_offers(in_stock) WHERE in_stock = TRUE;
CREATE INDEX IF NOT EXISTS idx_offers_stale ON vendor_offers(last_scraped_at) WHERE in_stock = TRUE;
CREATE INDEX IF NOT EXISTS idx_offers_merchant ON vendor_offers(merchant_id);
CREATE INDEX IF NOT EXISTS idx_offers_sponsored ON vendor_offers(is_sponsored, sponsor_bid_id) WHERE is_sponsored = TRUE;
CREATE INDEX IF NOT EXISTS idx_offers_source ON vendor_offers(source);
CREATE INDEX IF NOT EXISTS idx_offers_partner ON vendor_offers(partner_key_id) WHERE partner_key_id IS NOT NULL;

-- Composite: product + in_stock + price (most common search query)
CREATE INDEX IF NOT EXISTS idx_offers_search ON vendor_offers(product_id, in_stock, current_price);

-- -------------------------------
-- PRICE HISTORY indexes (on parent + partitions)
-- -------------------------------
CREATE INDEX IF NOT EXISTS idx_price_history_offer ON price_history(offer_id);
CREATE INDEX IF NOT EXISTS idx_price_history_time ON price_history(recorded_at);
CREATE INDEX IF NOT EXISTS idx_price_history_offer_time ON price_history(offer_id, recorded_at DESC);

-- -------------------------------
-- USERS indexes
-- -------------------------------
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone) WHERE phone IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_users_fcm ON users(fcm_token) WHERE fcm_token IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_users_device_hashes ON users USING gin(device_hashes);
CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active) WHERE is_active = TRUE;

-- -------------------------------
-- AFFILIATE CLICKS indexes
-- -------------------------------
CREATE INDEX IF NOT EXISTS idx_affiliate_clicks_device ON affiliate_clicks(device_hash);
CREATE INDEX IF NOT EXISTS idx_affiliate_clicks_offer ON affiliate_clicks(offer_id);
CREATE INDEX IF NOT EXISTS idx_affiliate_clicks_product ON affiliate_clicks(product_id);
CREATE INDEX IF NOT EXISTS idx_affiliate_clicks_user ON affiliate_clicks(user_id) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_affiliate_clicks_merchant ON affiliate_clicks(merchant_id);
CREATE INDEX IF NOT EXISTS idx_affiliate_clicks_status ON affiliate_clicks(status);
CREATE INDEX IF NOT EXISTS idx_affiliate_clicks_time ON affiliate_clicks(clicked_at DESC);
CREATE INDEX IF NOT EXISTS idx_affiliate_clicks_conversion ON affiliate_clicks(converted_at) WHERE converted_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_affiliate_clicks_fraud ON affiliate_clicks(fraud_score) WHERE fraud_score > 0.3;

-- Composite: device + merchant + time (conversion matching)
CREATE INDEX IF NOT EXISTS idx_affiliate_clicks_match ON affiliate_clicks(device_hash, merchant_id, clicked_at DESC);

-- -------------------------------
-- WATCHLIST indexes
-- -------------------------------
CREATE INDEX IF NOT EXISTS idx_watchlist_user ON user_watchlist(user_id);
CREATE INDEX IF NOT EXISTS idx_watchlist_product ON user_watchlist(product_id);
CREATE INDEX IF NOT EXISTS idx_watchlist_notify ON user_watchlist(notify_push) WHERE notify_push = TRUE;

-- -------------------------------
-- PUSH NOTIFICATIONS indexes
-- -------------------------------
CREATE INDEX IF NOT EXISTS idx_push_notif_pending ON push_notifications(status, scheduled_at) 
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_push_notif_user ON push_notifications(user_id);
CREATE INDEX IF NOT EXISTS idx_push_notif_failed ON push_notifications(status, retry_count) 
    WHERE status = 'failed' AND retry_count < 3;

-- -------------------------------
-- USER ALERTS indexes
-- -------------------------------
CREATE INDEX IF NOT EXISTS idx_user_alerts_email ON user_alerts(user_email);
CREATE INDEX IF NOT EXISTS idx_user_alerts_product ON user_alerts(product_id);
CREATE INDEX IF NOT EXISTS idx_user_alerts_active ON user_alerts(is_active) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_user_alerts_user ON user_alerts(user_id) WHERE user_id IS NOT NULL;

-- -------------------------------
-- MERCHANT RULES indexes
-- -------------------------------
CREATE INDEX IF NOT EXISTS idx_merchant_rules_merchant ON merchant_rules(merchant_id);
CREATE INDEX IF NOT EXISTS idx_merchant_rules_product ON merchant_rules(product_id);
CREATE INDEX IF NOT EXISTS idx_merchant_rules_active ON merchant_rules(is_active) WHERE is_active = TRUE;

-- -------------------------------
-- PRICE AUDIT LOGS indexes
-- -------------------------------
CREATE INDEX IF NOT EXISTS idx_audit_logs_merchant_product ON price_audit_logs(merchant_id, product_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_time ON price_audit_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_logs_circuit ON price_audit_logs(circuit_breaker_tripped) 
    WHERE circuit_breaker_tripped = TRUE;

-- -------------------------------
-- OCR SUBMISSIONS indexes
-- -------------------------------
CREATE INDEX IF NOT EXISTS idx_ocr_device ON ocr_submissions(device_hash);
CREATE INDEX IF NOT EXISTS idx_ocr_status ON ocr_submissions(status);
CREATE INDEX IF NOT EXISTS idx_ocr_created ON ocr_submissions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ocr_matched ON ocr_submissions(matched_product_id) WHERE matched_product_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ocr_fraud ON ocr_submissions(fraud_score) WHERE fraud_score > 0.3;
CREATE INDEX IF NOT EXISTS idx_ocr_extracted ON ocr_submissions USING gin(ai_extracted_data);

-- -------------------------------
-- PENDING MATCHES indexes
-- -------------------------------
CREATE INDEX IF NOT EXISTS idx_pending_matches_status ON pending_matches(status) WHERE status = 'pending';

-- -------------------------------
-- PARTNER API KEYS indexes
-- -------------------------------
CREATE INDEX IF NOT EXISTS idx_partner_keys_merchant ON partner_api_keys(merchant_id);
CREATE INDEX IF NOT EXISTS idx_partner_keys_active ON partner_api_keys(api_key_hash) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_partner_keys_prefix ON partner_api_keys(api_key_prefix);

-- -------------------------------
-- PARTNER FEED LOGS indexes
-- -------------------------------
CREATE INDEX IF NOT EXISTS idx_feed_logs_partner ON partner_feed_logs(partner_key_id);
CREATE INDEX IF NOT EXISTS idx_feed_logs_time ON partner_feed_logs(processed_at DESC);

-- -------------------------------
-- MERCHANT CLAIMS indexes
-- -------------------------------
CREATE INDEX IF NOT EXISTS idx_claims_merchant ON merchant_claims(merchant_id);
CREATE INDEX IF NOT EXISTS idx_claims_product ON merchant_claims(product_id);
CREATE INDEX IF NOT EXISTS idx_claims_status ON merchant_claims(status) WHERE status = 'pending';

-- -------------------------------
-- SPONSORED PLACEMENTS indexes
-- -------------------------------
CREATE INDEX IF NOT EXISTS idx_sponsored_merchant ON sponsored_placements(merchant_id);
CREATE INDEX IF NOT EXISTS idx_sponsored_product ON sponsored_placements(product_id);
CREATE INDEX IF NOT EXISTS idx_sponsored_keyword ON sponsored_placements(keyword) WHERE keyword IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sponsored_active ON sponsored_placements(is_active, start_date, end_date) 
    WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_sponsored_bid ON sponsored_placements(bid_amount DESC) 
    WHERE is_active = TRUE AND daily_spend < daily_budget;

-- -------------------------------
-- WALLET TRANSACTIONS indexes
-- -------------------------------
CREATE INDEX IF NOT EXISTS idx_wallet_txn_merchant ON wallet_transactions(merchant_id);
CREATE INDEX IF NOT EXISTS idx_wallet_txn_time ON wallet_transactions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wallet_txn_type ON wallet_transactions(type);

-- -------------------------------
-- PRICE PREDICTIONS indexes
-- -------------------------------
CREATE INDEX IF NOT EXISTS idx_predictions_product ON price_predictions(product_id);
CREATE INDEX IF NOT EXISTS idx_predictions_valid ON price_predictions(valid_until) 
    WHERE valid_until > NOW();
CREATE INDEX IF NOT EXISTS idx_predictions_confidence ON price_predictions(confidence DESC) 
    WHERE confidence > 0.6;

-- -------------------------------
-- SEARCH QUERIES indexes
-- -------------------------------
CREATE INDEX IF NOT EXISTS idx_search_queries_text ON search_queries USING gin(query_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_search_queries_user ON search_queries(user_id) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_search_queries_time ON search_queries(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_search_queries_intent ON search_queries(intent_classified) 
    WHERE intent_classified IS NOT NULL;

-- ==============================================================================
-- VIEWS (Convenience for common queries)
-- ==============================================================================

-- Best price per product (materialized view for fast search)
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_best_prices AS
SELECT DISTINCT ON (p.id)
    p.id AS product_id,
    p.title,
    p.brand,
    p.image_url,
    p.category,
    p.popularity_score,
    vo.id AS best_offer_id,
    vo.current_price AS best_price,
    vo.currency,
    vo.vendor_id,
    v.name AS vendor_name,
    vo.mrp,
    CASE WHEN vo.mrp > 0 THEN ROUND(((vo.mrp - vo.current_price) / vo.mrp * 100), 1) ELSE 0 END AS discount_pct,
    vo.in_stock,
    vo.is_sponsored,
    vo.product_url,
    vo.last_scraped_at
FROM products p
JOIN vendor_offers vo ON vo.product_id = p.id
JOIN vendors v ON vo.vendor_id = v.id
WHERE vo.in_stock = TRUE
ORDER BY p.id, vo.current_price ASC;

CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_best_prices_product ON mv_best_prices(product_id);
CREATE INDEX IF NOT EXISTS idx_mv_best_prices_category ON mv_best_prices(category);
CREATE INDEX IF NOT EXISTS idx_mv_best_prices_price ON mv_best_prices(best_price);

-- Refresh strategy: Run REFRESH MATERIALIZED VIEW CONCURRENTLY mv_best_prices;
-- via Celery Beat every 5 minutes

-- Merchant performance summary
CREATE OR REPLACE VIEW v_merchant_performance AS
SELECT
    merchant_id,
    COUNT(*) FILTER (WHERE status = 'clicked') AS total_clicks,
    COUNT(*) FILTER (WHERE status = 'converted') AS total_conversions,
    ROUND(COUNT(*) FILTER (WHERE status = 'converted')::numeric / NULLIF(COUNT(*) FILTER (WHERE status = 'clicked'), 0) * 100, 2) AS conversion_rate_pct,
    COALESCE(SUM(conversion_value), 0) AS total_revenue,
    COALESCE(SUM(commission_earned), 0) AS total_commission,
    DATE_TRUNC('month', clicked_at) AS month
FROM affiliate_clicks
WHERE merchant_id IS NOT NULL
GROUP BY merchant_id, DATE_TRUNC('month', clicked_at);

-- ==============================================================================
-- VERIFICATION: HNSW Index Status
-- ==============================================================================
DO $$
DECLARE
    hnsw_count INT;
BEGIN
    SELECT COUNT(*) INTO hnsw_count
    FROM pg_indexes 
    WHERE indexname = 'idx_products_embedding_hnsw';

    IF hnsw_count = 0 THEN
        RAISE WARNING 'HNSW index not created. Ensure pgvector extension is installed correctly.';
    ELSE
        RAISE NOTICE 'HNSW index verified: idx_products_embedding_hnsw';
    END IF;
END $$;
