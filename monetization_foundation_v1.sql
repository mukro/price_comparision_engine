-- Migration: monetization_foundation_v1.sql
-- Run this BEFORE any new code deploys

-- ============================================================
-- 1. USER AUTHENTICATION (Foundation for everything)
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) UNIQUE NOT NULL,
    phone VARCHAR(20) UNIQUE,
    password_hash VARCHAR(255),           -- nullable for social/OTP auth
    auth_provider VARCHAR(50) DEFAULT 'local', -- 'local', 'google', 'phone_otp'
    auth_provider_id VARCHAR(255),         -- e.g. Google sub, phone number
    full_name VARCHAR(255),
    avatar_url TEXT,
    fcm_token TEXT,                       -- Firebase Cloud Messaging token
    device_hashes TEXT[] DEFAULT '{}',    -- Array of known device hashes
    is_active BOOLEAN DEFAULT TRUE,
    email_verified BOOLEAN DEFAULT FALSE,
    phone_verified BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_users_email ON users(email);
CREATE INDEX idx_users_fcm ON users(fcm_token) WHERE fcm_token IS NOT NULL;
CREATE INDEX idx_users_device_hashes ON users USING GIN(device_hashes);

-- ============================================================
-- 2. AFFILIATE CLICK TRACKING (Revenue Proof)
-- ============================================================
CREATE TABLE IF NOT EXISTS affiliate_clicks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    click_id VARCHAR(64) UNIQUE NOT NULL,  -- pce.io/click?id=xxx
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    device_hash VARCHAR(64) NOT NULL,
    offer_id UUID NOT NULL,
    product_id UUID NOT NULL,
    vendor_id UUID NOT NULL,
    merchant_id VARCHAR(255),            -- NULL for scraped, set for partner
    landing_url TEXT NOT NULL,
    ip_address INET,
    user_agent TEXT,
    referrer TEXT,
    clicked_at TIMESTAMPTZ DEFAULT NOW(),
    converted_at TIMESTAMPTZ,              -- Set when conversion webhook fires
    conversion_value DECIMAL(12,2),        -- Order value from merchant
    commission_earned DECIMAL(12,2),         -- Your cut
    status VARCHAR(20) DEFAULT 'clicked'     -- clicked, converted, expired, disputed
);

CREATE INDEX idx_affiliate_clicks_device ON affiliate_clicks(device_hash);
CREATE INDEX idx_affiliate_clicks_offer ON affiliate_clicks(offer_id);
CREATE INDEX idx_affiliate_clicks_user ON affiliate_clicks(user_id);
CREATE INDEX idx_affiliate_clicks_status ON affiliate_clicks(status);
CREATE INDEX idx_affiliate_clicks_time ON affiliate_clicks(clicked_at);
CREATE INDEX idx_affiliate_clicks_conversion ON affiliate_clicks(converted_at) WHERE converted_at IS NOT NULL;

-- ============================================================
-- 3. USER WATCHLIST (Retention Engine)
-- ============================================================
CREATE TABLE IF NOT EXISTS user_watchlist (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    target_price DECIMAL(12,2),            -- NULL = notify on any drop
    notify_push BOOLEAN DEFAULT TRUE,
    notify_email BOOLEAN DEFAULT FALSE,
    last_notified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, product_id)
);

CREATE INDEX idx_watchlist_user ON user_watchlist(user_id);
CREATE INDEX idx_watchlist_product ON user_watchlist(product_id);

-- ============================================================
-- 4. PUSH NOTIFICATION QUEUE (FCM Integration)
-- ============================================================
CREATE TABLE IF NOT EXISTS push_notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    fcm_token TEXT NOT NULL,
    title VARCHAR(255) NOT NULL,
    body TEXT NOT NULL,
    data JSONB DEFAULT '{}',               -- {product_id, offer_id, type: 'price_drop'}
    priority VARCHAR(10) DEFAULT 'normal', -- normal, high
    scheduled_at TIMESTAMPTZ DEFAULT NOW(),
    sent_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    failed_at TIMESTAMPTZ,
    error_message TEXT,
    status VARCHAR(20) DEFAULT 'pending'   -- pending, sent, delivered, failed
);

CREATE INDEX idx_push_notif_pending ON push_notifications(status, scheduled_at) WHERE status = 'pending';

-- ============================================================
-- 5. MERCHANT PARTNER FEED API KEYS (Tier 3 Moat)
-- ============================================================
CREATE TABLE IF NOT EXISTS partner_api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id VARCHAR(255) NOT NULL,
    api_key_hash VARCHAR(255) NOT NULL,    -- bcrypt hash of the key
    api_key_prefix VARCHAR(8) NOT NULL,   -- First 8 chars for display
    name VARCHAR(255) NOT NULL,           -- e.g. "Amazon India Production"
    is_active BOOLEAN DEFAULT TRUE,
    rate_limit_rpm INT DEFAULT 60,         -- Requests per minute
    allowed_ips INET[],                    -- IP allowlist (NULL = any)
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(merchant_id, name)
);

CREATE INDEX idx_partner_keys_merchant ON partner_api_keys(merchant_id);
CREATE INDEX idx_partner_keys_active ON partner_api_keys(api_key_hash) WHERE is_active = TRUE;

-- ============================================================
-- 6. PARTNER FEED INGESTION LOG (Audit Trail)
-- ============================================================
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

-- ============================================================
-- 7. MERCHANT CLAIMS (Trust Layer)
-- ============================================================
CREATE TABLE IF NOT EXISTS merchant_claims (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id VARCHAR(255) NOT NULL,
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    domain VARCHAR(255) NOT NULL,          -- e.g. amazon.in
    verification_method VARCHAR(20) NOT NULL, -- 'dns_txt', 'email', 'manual_review'
    verification_token VARCHAR(255) NOT NULL, -- e.g. pce-verify-abc123
    verified_at TIMESTAMPTZ,
    status VARCHAR(20) DEFAULT 'pending', -- pending, verified, rejected
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_claims_merchant ON merchant_claims(merchant_id);
CREATE INDEX idx_claims_product ON merchant_claims(product_id);
CREATE INDEX idx_claims_status ON merchant_claims(status);

-- ============================================================
-- 8. SPONSORED PLACEMENTS (Revenue Engine)
-- ============================================================
CREATE TABLE IF NOT EXISTS sponsored_placements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id VARCHAR(255) NOT NULL,
    product_id UUID REFERENCES products(id) ON DELETE CASCADE,
    keyword VARCHAR(255),                   -- Target search keyword (NULL = product-level)
    bid_amount DECIMAL(12,4) NOT NULL,     -- CPC bid in INR/USD
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

-- ============================================================
-- 9. MERCHANT WALLETS
-- ============================================================
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

-- ============================================================
-- 10. WALLET TRANSACTIONS
-- ============================================================
CREATE TABLE IF NOT EXISTS wallet_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id VARCHAR(255) NOT NULL REFERENCES merchant_wallets(merchant_id),
    type VARCHAR(20) NOT NULL,              -- credit, debit, refund
    amount DECIMAL(12,2) NOT NULL,
    description TEXT,
    reference_id VARCHAR(255),             -- e.g. invoice_id, click_id
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_wallet_txn_merchant ON wallet_transactions(merchant_id);
CREATE INDEX idx_wallet_txn_time ON wallet_transactions(created_at);

-- ============================================================
-- 11. ALTER EXISTING TABLES
-- ============================================================

-- Add sponsor fields to vendor_offers
ALTER TABLE vendor_offers 
    ADD COLUMN IF NOT EXISTS is_sponsored BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS sponsor_bid_id UUID REFERENCES sponsored_placements(id),
    ADD COLUMN IF NOT EXISTS sponsor_rank_boost INT DEFAULT 0;

-- Add partner source tracking
ALTER TABLE vendor_offers
    ADD COLUMN IF NOT EXISTS source VARCHAR(20) DEFAULT 'scraped',  -- 'scraped', 'partner_feed', 'merchant_api'
    ADD COLUMN IF NOT EXISTS partner_key_id UUID REFERENCES partner_api_keys(id),
    ADD COLUMN IF NOT EXISTS feed_updated_at TIMESTAMPTZ;

-- Update user_alerts to support user_id (backward compatible)
ALTER TABLE user_alerts 
    ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS notify_push BOOLEAN DEFAULT FALSE;

-- Add conversion tracking to vendor_offers
ALTER TABLE vendor_offers
    ADD COLUMN IF NOT EXISTS conversion_count INT DEFAULT 0,
    ADD COLUMN IF NOT EXISTS conversion_revenue DECIMAL(12,2) DEFAULT 0;

-- ============================================================
-- 12. TRIGGERS
-- ============================================================

-- Auto-update updated_at on users
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_users_updated_at ON users;
CREATE TRIGGER trg_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS trg_merchant_wallets_updated_at ON merchant_wallets;
CREATE TRIGGER trg_merchant_wallets_updated_at
    BEFORE UPDATE ON merchant_wallets
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS trg_sponsored_placements_updated_at ON sponsored_placements;
CREATE TRIGGER trg_sponsored_placements_updated_at
    BEFORE UPDATE ON sponsored_placements
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
