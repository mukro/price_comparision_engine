-- ==============================================================================
-- Price Comparison Engine — Complete Database Schema
-- ==============================================================================
-- Run this as the first init script (e.g., 01_schema.sql)
-- Requires: PostgreSQL 16 + pgvector extension
-- ==============================================================================

-- ==============================================================================
-- 0. EXTENSIONS
-- ==============================================================================
CREATE EXTENSION IF NOT EXISTS pg_trgm;       -- Fuzzy text search (similarity)
CREATE EXTENSION IF NOT EXISTS vector;         -- Vector embeddings for AI search
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";    -- UUID generation

-- ==============================================================================
-- 1. PRODUCTS (Master Catalog)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS products (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL,
    brand VARCHAR(255),
    model_code VARCHAR(255),
    image_url TEXT,
    specifications JSONB DEFAULT '{}',
    category VARCHAR(255),
    embedding VECTOR(384),                      -- sentence-transformers all-MiniLM-L6-v2
    text_search_tsv TSVECTOR,                  -- Full-text search (kept in sync via trigger)
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_products_title ON products USING gin(title gin_trgm_ops);
CREATE INDEX idx_products_brand ON products(brand);
CREATE INDEX idx_products_model ON products(model_code);
CREATE INDEX idx_products_category ON products(category);
CREATE INDEX idx_products_embedding ON products USING ivfflat (embedding vector_cosine_ops);
CREATE INDEX idx_products_text_search ON products USING gin(text_search_tsv);

-- Trigger to auto-update text_search_tsv
CREATE OR REPLACE FUNCTION products_search_trigger()
RETURNS TRIGGER AS $$
BEGIN
    NEW.text_search_tsv :=
        setweight(to_tsvector('english', COALESCE(NEW.title, '')), 'A') ||
        setweight(to_tsvector('english', COALESCE(NEW.brand, '')), 'B') ||
        setweight(to_tsvector('english', COALESCE(NEW.model_code, '')), 'B');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_products_search_update ON products;
CREATE TRIGGER trg_products_search_update
    BEFORE INSERT OR UPDATE ON products
    FOR EACH ROW EXECUTE FUNCTION products_search_trigger();

-- ==============================================================================
-- 2. VENDORS (Merchant / Domain Registry)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS vendors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    domain VARCHAR(255) NOT NULL UNIQUE,
    title_selector VARCHAR(500),              -- CSS selector for scraping
    price_selector VARCHAR(500),
    stock_selector VARCHAR(500),
    stock_text_present VARCHAR(100) DEFAULT 'in stock',
    currency VARCHAR(3) DEFAULT 'INR',
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_vendors_domain ON vendors(domain);
CREATE INDEX idx_vendors_active ON vendors(is_active);

-- ==============================================================================
-- 3. VENDOR OFFERS (Price listings per product per vendor)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS vendor_offers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    vendor_id UUID NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
    merchant_id VARCHAR(255),                 -- NULL for scraped, set for partner feeds
    raw_title TEXT NOT NULL,
    current_price DECIMAL(12,2) NOT NULL,
    mrp DECIMAL(12,2),                        -- Maximum Retail Price
    currency VARCHAR(3) DEFAULT 'INR',
    in_stock BOOLEAN DEFAULT TRUE,
    product_url TEXT NOT NULL,
    affiliate_url TEXT,
    is_priority BOOLEAN DEFAULT FALSE,        -- Legacy: promoted flag
    is_sponsored BOOLEAN DEFAULT FALSE,       -- NEW: paid placement
    sponsor_bid_id UUID,                      -- NEW: links to sponsored_placements
    sponsor_rank_boost INT DEFAULT 0,          -- NEW: ranking boost
    source VARCHAR(20) DEFAULT 'scraped',      -- 'scraped', 'partner_feed', 'merchant_api'
    partner_key_id UUID,                      -- NEW: which partner API key pushed this
    feed_updated_at TIMESTAMPTZ,              -- NEW: last partner feed update
    click_count INT DEFAULT 0,                -- NEW: total clicks
    conversion_count INT DEFAULT 0,           -- NEW: total conversions
    conversion_revenue DECIMAL(12,2) DEFAULT 0, -- NEW: total conversion value
    last_scraped_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(product_id, vendor_id)
);

CREATE INDEX idx_vendor_offers_product ON vendor_offers(product_id);
CREATE INDEX idx_vendor_offers_vendor ON vendor_offers(vendor_id);
CREATE INDEX idx_vendor_offers_price ON vendor_offers(current_price);
CREATE INDEX idx_vendor_offers_stock ON vendor_offers(in_stock);
CREATE INDEX idx_vendor_offers_merchant ON vendor_offers(merchant_id);
CREATE INDEX idx_vendor_offers_sponsored ON vendor_offers(is_sponsored) WHERE is_sponsored = TRUE;
CREATE INDEX idx_vendor_offers_source ON vendor_offers(source);

-- ==============================================================================
-- 4. PRICE HISTORY (Time-series price data)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS price_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    offer_id UUID NOT NULL REFERENCES vendor_offers(id) ON DELETE CASCADE,
    price DECIMAL(12,2) NOT NULL,
    in_stock BOOLEAN DEFAULT TRUE,
    recorded_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_price_history_offer ON price_history(offer_id);
CREATE INDEX idx_price_history_time ON price_history(recorded_at);
CREATE INDEX idx_price_history_offer_time ON price_history(offer_id, recorded_at);

-- ==============================================================================
-- 5. MERCHANT RULES (B2B Dynamic Pricing Engine)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS merchant_rules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id VARCHAR(255) NOT NULL,
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    merchant_cost DECIMAL(12,2) NOT NULL,
    min_margin_pct DECIMAL(5,2) DEFAULT 15.0,
    map_price DECIMAL(12,2),                  -- Minimum Advertised Price
    strategy VARCHAR(50) DEFAULT 'undercut_by_fixed',
    strategy_value DECIMAL(12,2) DEFAULT 1.00,
    webhook_url TEXT,
    auto_apply_enabled BOOLEAN DEFAULT FALSE,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(merchant_id, product_id)
);

CREATE INDEX idx_merchant_rules_merchant ON merchant_rules(merchant_id);
CREATE INDEX idx_merchant_rules_product ON merchant_rules(product_id);
CREATE INDEX idx_merchant_rules_active ON merchant_rules(is_active);

-- ==============================================================================
-- 6. PRICE AUDIT LOGS (Circuit breaker tracking)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS price_audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id VARCHAR(255) NOT NULL,
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    old_price DECIMAL(12,2) NOT NULL,
    new_price DECIMAL(12,2),
    reason TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_audit_logs_merchant_product ON price_audit_logs(merchant_id, product_id);
CREATE INDEX idx_audit_logs_time ON price_audit_logs(created_at);

-- ==============================================================================
-- 7. USER ALERTS (Legacy email-based price drop alerts)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS user_alerts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_email VARCHAR(255) NOT NULL,
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    target_price DECIMAL(12,2) NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    last_notified_at TIMESTAMPTZ,
    user_id UUID,                             -- NEW: links to users table
    notify_push BOOLEAN DEFAULT FALSE,        -- NEW: also send push?
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_email, product_id)
);

CREATE INDEX idx_user_alerts_email ON user_alerts(user_email);
CREATE INDEX idx_user_alerts_product ON user_alerts(product_id);
CREATE INDEX idx_user_alerts_active ON user_alerts(is_active);

-- ==============================================================================
-- 8. OCR SUBMISSIONS (Community-contributed price data)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS ocr_submissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_hash VARCHAR(64) NOT NULL,
    product_name VARCHAR(500),
    brand VARCHAR(255),
    price DECIMAL(12,2),
    currency VARCHAR(3) DEFAULT 'INR',
    vendor_domain VARCHAR(255),
    in_stock BOOLEAN,
    ocr_confidence DECIMAL(3,2),
    ocr_engine VARCHAR(50),
    device_os VARCHAR(50),
    app_version VARCHAR(20),
    status VARCHAR(20) DEFAULT 'pending',     -- pending, approved, rejected
    reviewed_by UUID,
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_ocr_device ON ocr_submissions(device_hash);
CREATE INDEX idx_ocr_status ON ocr_submissions(status);
CREATE INDEX idx_ocr_created ON ocr_submissions(created_at);

-- ==============================================================================
-- 9. ADMIN PENDING MATCHES (Product deduplication queue)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS pending_matches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ocr_submission_id UUID REFERENCES ocr_submissions(id),
    suggested_product_id UUID REFERENCES products(id),
    confidence_score DECIMAL(5,4),
    status VARCHAR(20) DEFAULT 'pending',     -- pending, approved, rejected
    reviewed_by UUID,
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_pending_matches_status ON pending_matches(status);

-- ==============================================================================
-- 10. USER AUTHENTICATION (NEW — Foundation for everything)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) UNIQUE NOT NULL,
    phone VARCHAR(20) UNIQUE,
    password_hash VARCHAR(255),
    auth_provider VARCHAR(50) DEFAULT 'local',   -- 'local', 'google', 'phone_otp'
    auth_provider_id VARCHAR(255),
    full_name VARCHAR(255),
    avatar_url TEXT,
    fcm_token TEXT,                              -- Firebase Cloud Messaging token
    device_hashes TEXT[] DEFAULT '{}',
    is_active BOOLEAN DEFAULT TRUE,
    email_verified BOOLEAN DEFAULT FALSE,
    phone_verified BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_users_email ON users(email);
CREATE INDEX idx_users_fcm ON users(fcm_token) WHERE fcm_token IS NOT NULL;
CREATE INDEX idx_users_device_hashes ON users USING gin(device_hashes);

-- ==============================================================================
-- 11. AFFILIATE CLICK TRACKING (NEW — Revenue Proof)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS affiliate_clicks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    click_id VARCHAR(64) UNIQUE NOT NULL,
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
    clicked_at TIMESTAMPTZ DEFAULT NOW(),
    converted_at TIMESTAMPTZ,
    conversion_value DECIMAL(12,2),
    commission_earned DECIMAL(12,2),
    status VARCHAR(20) DEFAULT 'clicked'         -- clicked, converted, expired, disputed, unmatched
);

CREATE INDEX idx_affiliate_clicks_device ON affiliate_clicks(device_hash);
CREATE INDEX idx_affiliate_clicks_offer ON affiliate_clicks(offer_id);
CREATE INDEX idx_affiliate_clicks_user ON affiliate_clicks(user_id);
CREATE INDEX idx_affiliate_clicks_status ON affiliate_clicks(status);
CREATE INDEX idx_affiliate_clicks_time ON affiliate_clicks(clicked_at);
CREATE INDEX idx_affiliate_clicks_conversion ON affiliate_clicks(converted_at) WHERE converted_at IS NOT NULL;

-- ==============================================================================
-- 12. USER WATCHLIST (NEW — Retention Engine)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS user_watchlist (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    target_price DECIMAL(12,2),
    notify_push BOOLEAN DEFAULT TRUE,
    notify_email BOOLEAN DEFAULT FALSE,
    last_notified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, product_id)
);

CREATE INDEX idx_watchlist_user ON user_watchlist(user_id);
CREATE INDEX idx_watchlist_product ON user_watchlist(product_id);

-- ==============================================================================
-- 13. PUSH NOTIFICATION QUEUE (NEW — FCM Integration)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS push_notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    fcm_token TEXT NOT NULL,
    title VARCHAR(255) NOT NULL,
    body TEXT NOT NULL,
    data JSONB DEFAULT '{}',
    priority VARCHAR(10) DEFAULT 'normal',
    scheduled_at TIMESTAMPTZ DEFAULT NOW(),
    sent_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    failed_at TIMESTAMPTZ,
    error_message TEXT,
    status VARCHAR(20) DEFAULT 'pending'
);

CREATE INDEX idx_push_notif_pending ON push_notifications(status, scheduled_at) WHERE status = 'pending';

-- ==============================================================================
-- 14. PARTNER API KEYS (NEW — Tier 3 Moat)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS partner_api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id VARCHAR(255) NOT NULL,
    api_key_hash VARCHAR(255) NOT NULL,
    api_key_prefix VARCHAR(8) NOT NULL,
    name VARCHAR(255) NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    rate_limit_rpm INT DEFAULT 60,
    allowed_ips INET[],
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(merchant_id, name)
);

CREATE INDEX idx_partner_keys_merchant ON partner_api_keys(merchant_id);
CREATE INDEX idx_partner_keys_active ON partner_api_keys(api_key_hash) WHERE is_active = TRUE;

-- ==============================================================================
-- 15. PARTNER FEED INGESTION LOG (NEW — Audit Trail)
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

CREATE INDEX idx_feed_logs_partner ON partner_feed_logs(partner_key_id);
CREATE INDEX idx_feed_logs_time ON partner_feed_logs(processed_at);

-- ==============================================================================
-- 16. MERCHANT CLAIMS (NEW — Trust Layer)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS merchant_claims (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id VARCHAR(255) NOT NULL,
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    domain VARCHAR(255) NOT NULL,
    verification_method VARCHAR(20) NOT NULL,    -- 'dns_txt', 'email', 'manual_review'
    verification_token VARCHAR(255) NOT NULL,
    verified_at TIMESTAMPTZ,
    status VARCHAR(20) DEFAULT 'pending',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_claims_merchant ON merchant_claims(merchant_id);
CREATE INDEX idx_claims_product ON merchant_claims(product_id);
CREATE INDEX idx_claims_status ON merchant_claims(status);

-- ==============================================================================
-- 17. SPONSORED PLACEMENTS (NEW — Revenue Engine)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS sponsored_placements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id VARCHAR(255) NOT NULL,
    product_id UUID REFERENCES products(id) ON DELETE CASCADE,
    keyword VARCHAR(255),
    bid_amount DECIMAL(12,4) NOT NULL,
    daily_budget DECIMAL(12,2) NOT NULL,
    daily_spend DECIMAL(12,2) DEFAULT 0,
    total_spend DECIMAL(12,2) DEFAULT 0,
    total_clicks INT DEFAULT 0,
    total_impressions INT DEFAULT 0,
    start_date DATE NOT NULL,
    end_date DATE,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_sponsored_merchant ON sponsored_placements(merchant_id);
CREATE INDEX idx_sponsored_product ON sponsored_placements(product_id);
CREATE INDEX idx_sponsored_keyword ON sponsored_placements(keyword) WHERE keyword IS NOT NULL;
CREATE INDEX idx_sponsored_active ON sponsored_placements(is_active, start_date, end_date);

-- ==============================================================================
-- 18. MERCHANT WALLETS (NEW)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS merchant_wallets (
    merchant_id VARCHAR(255) PRIMARY KEY,
    balance DECIMAL(12,2) DEFAULT 0,
    auto_recharge_enabled BOOLEAN DEFAULT FALSE,
    auto_recharge_threshold DECIMAL(12,2) DEFAULT 1000,
    auto_recharge_amount DECIMAL(12,2) DEFAULT 5000,
    currency VARCHAR(3) DEFAULT 'INR',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ==============================================================================
-- 19. WALLET TRANSACTIONS (NEW)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS wallet_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id VARCHAR(255) NOT NULL REFERENCES merchant_wallets(merchant_id),
    type VARCHAR(20) NOT NULL,                    -- credit, debit, refund
    amount DECIMAL(12,2) NOT NULL,
    description TEXT,
    reference_id VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_wallet_txn_merchant ON wallet_transactions(merchant_id);
CREATE INDEX idx_wallet_txn_time ON wallet_transactions(created_at);

-- ==============================================================================
-- 20. AUTO-UPDATE TRIGGERS
-- ==============================================================================

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_products_updated_at ON products;
CREATE TRIGGER trg_products_updated_at
    BEFORE UPDATE ON products FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS trg_vendor_offers_updated_at ON vendor_offers;
CREATE TRIGGER trg_vendor_offers_updated_at
    BEFORE UPDATE ON vendor_offers FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS trg_merchant_rules_updated_at ON merchant_rules;
CREATE TRIGGER trg_merchant_rules_updated_at
    BEFORE UPDATE ON merchant_rules FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS trg_users_updated_at ON users;
CREATE TRIGGER trg_users_updated_at
    BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS trg_merchant_wallets_updated_at ON merchant_wallets;
CREATE TRIGGER trg_merchant_wallets_updated_at
    BEFORE UPDATE ON merchant_wallets FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS trg_sponsored_placements_updated_at ON sponsored_placements;
CREATE TRIGGER trg_sponsored_placements_updated_at
    BEFORE UPDATE ON sponsored_placements FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
