-- ==============================================================================
-- PCE — Bulletproof Unified Schema v3.2
-- Idempotent. Safe to re-run. No broken verification block.
-- Requires: PostgreSQL 16+, pgvector, pg_trgm, uuid-ossp
-- ==============================================================================

SET search_path = public;

-- ==============================================================================
-- 0. EXTENSIONS
-- ==============================================================================
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";
CREATE EXTENSION IF NOT EXISTS "vector";

-- ==============================================================================
-- 1. HELPER FUNCTIONS (Owner-agnostic — skips if already exists)
-- ==============================================================================

DO $$
BEGIN
    CREATE FUNCTION update_updated_at_column()
    RETURNS TRIGGER AS $f$ BEGIN NEW.updated_at = NOW(); RETURN NEW; END; $f$
    LANGUAGE plpgsql;
EXCEPTION WHEN duplicate_function THEN NULL;
END $$;

DO $$
BEGIN
    CREATE FUNCTION generate_click_id()
    RETURNS VARCHAR(64) AS $f$
    DECLARE
        chars TEXT := 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
        result VARCHAR(64) := '';
        i INT := 0;
    BEGIN
        FOR i IN 1..32 LOOP
            result := result || substr(chars, floor(random() * length(chars) + 1)::int, 1);
        END LOOP;
        RETURN 'clk_' || result;
    END; $f$
    LANGUAGE plpgsql STABLE;
EXCEPTION WHEN duplicate_function THEN NULL;
END $$;

DO $$
BEGIN
    CREATE FUNCTION products_tsv_trigger()
    RETURNS TRIGGER AS $f$
    BEGIN
        NEW.text_search_tsv :=
            setweight(to_tsvector('english', coalesce(NEW.title, '')), 'A') ||
            setweight(to_tsvector('english', coalesce(NEW.brand, '')), 'B') ||
            setweight(to_tsvector('english', coalesce(NEW.model_code, '')), 'C');
        NEW.updated_at := NOW();
        RETURN NEW;
    END; $f$
    LANGUAGE plpgsql;
EXCEPTION WHEN duplicate_function THEN NULL;
END $$;

-- ==============================================================================
-- 2. CORE TABLES
-- ==============================================================================

CREATE TABLE IF NOT EXISTS vendors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    domain VARCHAR(255) NOT NULL UNIQUE,
    affiliate_tag VARCHAR(100),
    title_selector VARCHAR(500) DEFAULT '.product-title',
    price_selector VARCHAR(500) DEFAULT '.price',
    stock_selector VARCHAR(500),
    stock_text_present VARCHAR(100) DEFAULT 'in stock',
    currency VARCHAR(3) DEFAULT 'INR',
    scraping_allowed BOOLEAN DEFAULT TRUE,
    scrape_rpm INTEGER DEFAULT 6,
    is_active BOOLEAN DEFAULT TRUE,
    respects_robots_txt BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS products (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title VARCHAR(500) NOT NULL,
    brand VARCHAR(255),
    model_code VARCHAR(255),
    image_url TEXT,
    specifications JSONB DEFAULT '{}',
    category VARCHAR(255),
    subcategory VARCHAR(255),
    title_embedding VECTOR(384),
    text_search_tsv TSVECTOR,
    popularity_score NUMERIC(5, 4) DEFAULT 0.5,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

DROP TRIGGER IF EXISTS trg_products_tsv ON products;
CREATE TRIGGER trg_products_tsv
    BEFORE INSERT OR UPDATE OF title, brand, model_code ON products
    FOR EACH ROW EXECUTE FUNCTION products_tsv_trigger();

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) UNIQUE NOT NULL,
    phone VARCHAR(20) UNIQUE,
    password_hash VARCHAR(255),
    auth_provider VARCHAR(50) DEFAULT 'local' CHECK (auth_provider IN ('local','google','phone_otp','apple')),
    auth_provider_id VARCHAR(255),
    full_name VARCHAR(255),
    avatar_url TEXT,
    fcm_token TEXT,
    device_hashes TEXT[] DEFAULT '{}',
    is_active BOOLEAN DEFAULT TRUE,
    email_verified BOOLEAN DEFAULT FALSE,
    phone_verified BOOLEAN DEFAULT FALSE,
    last_login_at TIMESTAMPTZ,
    signup_source VARCHAR(50),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ==============================================================================
-- 3. OFFERS & PRICES
-- ==============================================================================

CREATE TABLE IF NOT EXISTS vendor_offers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    vendor_id UUID NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
    vendor_product_id VARCHAR(255) NOT NULL,
    merchant_id VARCHAR(255),
    raw_title VARCHAR(500) NOT NULL,
    current_price NUMERIC(12, 2) NOT NULL,
    mrp NUMERIC(12, 2),
    currency VARCHAR(3) DEFAULT 'INR',
    in_stock BOOLEAN DEFAULT TRUE,
    is_active BOOLEAN DEFAULT TRUE,
    product_url TEXT NOT NULL,
    affiliate_url TEXT,
    is_priority BOOLEAN DEFAULT FALSE,
    is_sponsored BOOLEAN DEFAULT FALSE,
    sponsor_bid_id UUID,
    sponsor_rank_boost INT DEFAULT 0,
    source VARCHAR(20) DEFAULT 'scraped' CHECK (source IN ('scraped','partner_feed','merchant_api','official_api','affiliate_feed','user_ocr')),
    data_source VARCHAR(20) DEFAULT 'scraped' CHECK (data_source IN ('official_api','merchant_partner','affiliate_feed','user_ocr','scraped')),
    partner_key_id UUID,
    feed_updated_at TIMESTAMPTZ,
    click_count INT DEFAULT 0,
    conversion_count INT DEFAULT 0,
    conversion_revenue NUMERIC(12, 2) DEFAULT 0,
    verification_score INT DEFAULT 0 CHECK (verification_score BETWEEN 0 AND 100),
    data_provenance JSONB DEFAULT '{}',
    submitted_by_user_id VARCHAR(255),
    geo_hash VARCHAR(12),
    expires_at TIMESTAMPTZ,
    match_status VARCHAR(20) DEFAULT 'pending_review' CHECK (match_status IN ('matched','pending_review','rejected')),
    confidence_score NUMERIC(4, 3),
    last_scraped_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(vendor_id, vendor_product_id)
);

-- ==============================================================================
-- PRICE HISTORY (partitioned by recorded_at)
-- ==============================================================================

CREATE TABLE IF NOT EXISTS price_history (
    id UUID NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    offer_id UUID NOT NULL REFERENCES vendor_offers(id) ON DELETE CASCADE,
    price NUMERIC(12, 2) NOT NULL,
    in_stock BOOLEAN DEFAULT TRUE,
    PRIMARY KEY (id, recorded_at)
) PARTITION BY RANGE (recorded_at);

DO $$
DECLARE
    start_date DATE; end_date DATE; partition_name TEXT;
BEGIN
    FOR i IN 0..13 LOOP
        start_date := DATE_TRUNC('month', CURRENT_DATE + (i || ' months')::interval);
        end_date := start_date + INTERVAL '1 month';
        partition_name := 'price_history_' || TO_CHAR(start_date, 'YYYY_MM');
        EXECUTE format('CREATE TABLE IF NOT EXISTS %I PARTITION OF price_history FOR VALUES FROM (%L) TO (%L)', partition_name, start_date, end_date);
        EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I USING BRIN (recorded_at)', partition_name || '_brin', partition_name);
    END LOOP;
END $$;

-- ==============================================================================
-- 4. ALERTS & WATCHLIST
-- ==============================================================================

CREATE TABLE IF NOT EXISTS user_alerts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_email VARCHAR(255) NOT NULL,
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    target_price NUMERIC(12, 2) NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    last_triggered_at TIMESTAMPTZ,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    notify_push BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_email, product_id)
);

CREATE TABLE IF NOT EXISTS alert_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_id UUID NOT NULL REFERENCES user_alerts(id) ON DELETE CASCADE,
    triggered_price NUMERIC(12, 2) NOT NULL,
    sent_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_watchlist (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    target_price NUMERIC(12, 2),
    notify_push BOOLEAN DEFAULT TRUE,
    notify_email BOOLEAN DEFAULT FALSE,
    last_notified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, product_id)
);

CREATE TABLE IF NOT EXISTS push_notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    fcm_token TEXT NOT NULL,
    title VARCHAR(255) NOT NULL,
    body TEXT NOT NULL,
    data JSONB DEFAULT '{}',
    priority VARCHAR(10) DEFAULT 'normal' CHECK (priority IN ('normal','high')),
    scheduled_at TIMESTAMPTZ DEFAULT NOW(),
    sent_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    failed_at TIMESTAMPTZ,
    error_message TEXT,
    status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending','sent','delivered','failed')),
    retry_count INT DEFAULT 0
);

-- ==============================================================================
-- 5. MERCHANT & B2B
-- ==============================================================================

CREATE TABLE IF NOT EXISTS merchant_rules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id VARCHAR(255) NOT NULL,
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    merchant_cost NUMERIC(12, 2) NOT NULL,
    min_margin_pct NUMERIC(5, 2) DEFAULT 15.00,
    map_price NUMERIC(12, 2),
    strategy VARCHAR(50) DEFAULT 'undercut_by_fixed' CHECK (strategy IN ('undercut_by_fixed','undercut_by_pct','match_lowest','premium_fixed','dynamic_margin')),
    strategy_value NUMERIC(12, 2) DEFAULT 1.00,
    webhook_url TEXT,
    auto_apply_enabled BOOLEAN DEFAULT FALSE,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(merchant_id, product_id)
);

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

CREATE TABLE IF NOT EXISTS merchant_partners (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_name VARCHAR(255) NOT NULL,
    legal_name VARCHAR(255),
    domain VARCHAR(255) NOT NULL UNIQUE,
    website_url VARCHAR(500),
    primary_email VARCHAR(255) NOT NULL,
    primary_phone VARCHAR(20),
    gst_number VARCHAR(20),
    pan_number VARCHAR(20),
    kyc_verified BOOLEAN DEFAULT FALSE,
    kyc_verified_at TIMESTAMPTZ,
    api_key_hash VARCHAR(255),
    webhook_url VARCHAR(500),
    webhook_secret VARCHAR(255),
    plan_type VARCHAR(20) DEFAULT 'free' CHECK (plan_type IN ('free','basic','premium','enterprise')),
    is_active BOOLEAN DEFAULT TRUE,
    onboarding_status VARCHAR(20) DEFAULT 'pending' CHECK (onboarding_status IN ('pending','kyc','approved','suspended')),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    last_feed_received_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS merchant_feed_submissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    partner_id UUID NOT NULL REFERENCES merchant_partners(id),
    feed_type VARCHAR(20) DEFAULT 'full' CHECK (feed_type IN ('full','delta','price_update','stock_update')),
    items_count INT DEFAULT 0,
    items_accepted INT DEFAULT 0,
    items_rejected INT DEFAULT 0,
    status VARCHAR(20) DEFAULT 'received' CHECK (status IN ('received','processing','completed','failed')),
    processing_started_at TIMESTAMPTZ,
    processing_completed_at TIMESTAMPTZ,
    error_message TEXT,
    source_ip INET,
    user_agent TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

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

CREATE TABLE IF NOT EXISTS merchant_claims (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id VARCHAR(255) NOT NULL,
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    domain VARCHAR(255) NOT NULL,
    verification_method VARCHAR(20) NOT NULL CHECK (verification_method IN ('dns_txt','email','manual_review')),
    verification_token VARCHAR(255) NOT NULL,
    verified_at TIMESTAMPTZ,
    status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending','verified','rejected')),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

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

CREATE TABLE IF NOT EXISTS wallet_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id VARCHAR(255) NOT NULL REFERENCES merchant_wallets(merchant_id),
    type VARCHAR(20) NOT NULL CHECK (type IN ('credit','debit','refund')),
    amount NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
    description TEXT,
    reference_id VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ==============================================================================
-- 6. AFFILIATE & REVENUE
-- ==============================================================================

-- ==============================================================================
-- AFFILIATE CLICKS (partitioned by clicked_at)
-- ==============================================================================

CREATE TABLE IF NOT EXISTS affiliate_clicks (
    id UUID NOT NULL,
    clicked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    click_id VARCHAR(64) NOT NULL DEFAULT generate_click_id(),
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
    country_code VARCHAR(2),
    city VARCHAR(100),
    converted_at TIMESTAMPTZ,
    conversion_value NUMERIC(12, 2),
    commission_earned NUMERIC(12, 2),
    status VARCHAR(20) DEFAULT 'clicked' CHECK (status IN ('clicked','converted','expired','disputed')),
    fraud_score NUMERIC(3, 2) DEFAULT 0,
    fraud_flags TEXT[] DEFAULT '{}',
    PRIMARY KEY (id, clicked_at)
) PARTITION BY RANGE (clicked_at);

-- Unique on click_id must also include partition column, so we use a regular index instead
CREATE UNIQUE INDEX IF NOT EXISTS idx_affiliate_clicks_click_id ON affiliate_clicks(click_id, clicked_at);

DO $$
BEGIN
    ALTER TABLE affiliate_clicks ADD CONSTRAINT fk_affiliate_clicks_offer FOREIGN KEY (offer_id) REFERENCES vendor_offers(id);
EXCEPTION WHEN duplicate_object THEN
    RAISE NOTICE 'FK fk_affiliate_clicks_offer already exists';
END $$;

DO $$
DECLARE
    start_date DATE; end_date DATE; partition_name TEXT;
BEGIN
    FOR i IN 0..13 LOOP
        start_date := DATE_TRUNC('month', CURRENT_DATE + (i || ' months')::interval);
        end_date := start_date + INTERVAL '1 month';
        partition_name := 'affiliate_clicks_' || TO_CHAR(start_date, 'YYYY_MM');
        EXECUTE format('CREATE TABLE IF NOT EXISTS %I PARTITION OF affiliate_clicks FOR VALUES FROM (%L) TO (%L)', partition_name, start_date, end_date);
        EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I USING BRIN (clicked_at)', partition_name || '_brin', partition_name);
    END LOOP;
END $$;


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
-- 7. OCR & COMMUNITY
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
    ocr_confidence NUMERIC(3, 2),
    ocr_engine VARCHAR(50),
    ai_extracted_data JSONB,
    ai_confidence NUMERIC(3, 2),
    extraction_method VARCHAR(20) DEFAULT 'mlkit' CHECK (extraction_method IN ('mlkit','llm','manual')),
    matched_product_id UUID REFERENCES products(id),
    match_confidence NUMERIC(3, 2),
    match_method VARCHAR(20),
    fraud_score NUMERIC(3, 2) DEFAULT 0,
    fraud_flags TEXT[] DEFAULT '{}',
    device_os VARCHAR(50),
    app_version VARCHAR(20),
    status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','merged')),
    reviewed_by UUID,
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_submissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id VARCHAR(255) NOT NULL,
    vendor_domain VARCHAR(255) NOT NULL,
    extracted_price NUMERIC(12, 2),
    extracted_currency VARCHAR(10) DEFAULT 'INR',
    extracted_product_name VARCHAR(500),
    extracted_stock_status BOOLEAN,
    geo_hash VARCHAR(12),
    submitted_at TIMESTAMPTZ DEFAULT NOW(),
    verification_score INT DEFAULT 0 CHECK (verification_score BETWEEN 0 AND 100),
    reviewed_by_admin BOOLEAN DEFAULT FALSE,
    admin_notes TEXT,
    device_os VARCHAR(50),
    app_version VARCHAR(20),
    ocr_confidence NUMERIC(4, 3),
    ocr_engine VARCHAR(50),
    screenshot_hash VARCHAR(64),
    status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','merged')),
    merged_offer_id UUID REFERENCES vendor_offers(id),
    merged_at TIMESTAMPTZ,
    device_hash VARCHAR(64) NOT NULL
);

CREATE TABLE IF NOT EXISTS user_validation_votes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    submission_id UUID NOT NULL REFERENCES user_submissions(id) ON DELETE CASCADE,
    device_hash VARCHAR(64) NOT NULL,
    upvote BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(submission_id, device_hash)
);

CREATE TABLE IF NOT EXISTS pending_matches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ocr_submission_id UUID REFERENCES ocr_submissions(id),
    suggested_product_id UUID REFERENCES products(id),
    confidence_score NUMERIC(5, 4),
    status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
    reviewed_by UUID,
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ==============================================================================
-- 8. SCRAPING & ADMIN UTILITIES
-- ==============================================================================

CREATE TABLE IF NOT EXISTS scrape_dlq (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    url TEXT NOT NULL,
    error_message TEXT,
    payload TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS match_feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    offer_id UUID REFERENCES vendor_offers(id) ON DELETE CASCADE,
    admin_decision VARCHAR(20) NOT NULL CHECK (admin_decision IN ('approved','rejected','corrected')),
    original_confidence NUMERIC(4, 3),
    corrected_product_id UUID REFERENCES products(id),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS merchant_api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id VARCHAR(255) NOT NULL UNIQUE,
    api_key_hash VARCHAR(255) NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_used_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS takedown_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id VARCHAR(50) UNIQUE NOT NULL,
    domain VARCHAR(255) NOT NULL,
    requester_email VARCHAR(255) NOT NULL,
    requester_name VARCHAR(255),
    legal_basis VARCHAR(50),
    specific_urls TEXT[],
    status VARCHAR(20) DEFAULT 'received' CHECK (status IN ('received','under_review','action_taken','rejected','appealed')),
    action_taken TEXT,
    received_at TIMESTAMPTZ DEFAULT NOW(),
    acknowledged_at TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ,
    assigned_to VARCHAR(255),
    notes TEXT
);

-- ==============================================================================
-- 9. AUTOBUY TABLES
-- ==============================================================================

CREATE TABLE IF NOT EXISTS user_profiles (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    phone_verified BOOLEAN DEFAULT FALSE,
    email_verified BOOLEAN DEFAULT FALSE,
    kyc_status VARCHAR(20) DEFAULT 'none' CHECK (kyc_status IN ('none','pending','verified','rejected')),
    kyc_submitted_at TIMESTAMPTZ,
    kyc_verified_at TIMESTAMPTZ,
    government_id_hash VARCHAR(255),
    date_of_birth DATE,
    gender VARCHAR(20),
    account_tier INT DEFAULT 1 CHECK (account_tier IN (1, 2, 3)),
    auto_buy_enabled BOOLEAN DEFAULT FALSE,
    auto_buy_enabled_at TIMESTAMPTZ,
    max_auto_buy_order_value NUMERIC(12, 2) DEFAULT 5000.00,
    daily_auto_buy_limit NUMERIC(12, 2) DEFAULT 10000.00,
    monthly_auto_buy_limit NUMERIC(12, 2) DEFAULT 50000.00,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_addresses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    address_type VARCHAR(20) DEFAULT 'shipping' CHECK (address_type IN ('shipping','billing','both')),
    label VARCHAR(50) DEFAULT 'Home',
    full_name VARCHAR(255) NOT NULL,
    phone VARCHAR(20) NOT NULL,
    address_line1 TEXT NOT NULL,
    address_line2 TEXT,
    city VARCHAR(100) NOT NULL,
    state VARCHAR(100) NOT NULL,
    postal_code VARCHAR(20) NOT NULL,
    country VARCHAR(2) DEFAULT 'IN',
    is_default BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_payment_methods (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    method_type VARCHAR(20) NOT NULL CHECK (method_type IN ('card','upi','wallet','netbanking','emi')),
    gateway VARCHAR(50) NOT NULL,
    gateway_token VARCHAR(255) NOT NULL,
    gateway_customer_id VARCHAR(255),
    card_last4 VARCHAR(4),
    card_network VARCHAR(20),
    card_expiry_month INT,
    card_expiry_year INT,
    upi_id VARCHAR(255),
    wallet_provider VARCHAR(50),
    billing_address_id UUID REFERENCES user_addresses(id),
    is_default BOOLEAN DEFAULT FALSE,
    is_active BOOLEAN DEFAULT TRUE,
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, gateway_token)
);

-- ==============================================================================
-- PURCHASE ORDERS (partitioned by created_at)
-- ==============================================================================

CREATE TABLE IF NOT EXISTS purchase_orders (
    id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    offer_id UUID NOT NULL REFERENCES vendor_offers(id),
    product_id UUID NOT NULL REFERENCES products(id),
    vendor_id UUID NOT NULL REFERENCES vendors(id),
    order_value NUMERIC(12, 2) NOT NULL,
    currency VARCHAR(3) DEFAULT 'INR',
    quantity INT DEFAULT 1,
    status VARCHAR(30) DEFAULT 'pending' CHECK (status IN (
        'pending','payment_initiated','payment_failed','payment_confirmed',
        'order_placed','vendor_confirmed','shipped','delivered','cancelled','refunded'
    )),
    payment_method_id UUID REFERENCES user_payment_methods(id),
    payment_gateway VARCHAR(50),
    payment_gateway_txn_id VARCHAR(255),
    payment_confirmed_at TIMESTAMPTZ,
    vendor_order_id VARCHAR(255),
    vendor_order_url TEXT,
    triggered_by VARCHAR(20) DEFAULT 'manual' CHECK (triggered_by IN ('manual','auto_buy_agent','price_alert','merchant_rule')),
    auto_buy_rule_id UUID,
    placed_at TIMESTAMPTZ DEFAULT NOW(),
    delivered_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    refund_amount NUMERIC(12, 2),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

DO $$
DECLARE
    start_date DATE; end_date DATE; partition_name TEXT;
BEGIN
    FOR i IN 0..13 LOOP
        start_date := DATE_TRUNC('month', CURRENT_DATE + (i || ' months')::interval);
        end_date := start_date + INTERVAL '1 month';
        partition_name := 'purchase_orders_' || TO_CHAR(start_date, 'YYYY_MM');
        IF NOT EXISTS (SELECT 1 FROM pg_tables WHERE tablename = partition_name AND schemaname = 'public') THEN
            EXECUTE format('CREATE TABLE %I PARTITION OF purchase_orders FOR VALUES FROM (%L) TO (%L)', partition_name, start_date, end_date);
            EXECUTE format('CREATE INDEX %I ON %I (user_id, status)', partition_name || '_user_status', partition_name);
        END IF;
    END LOOP;
END $$;


-- ==============================================================================
-- PAYMENT TRANSACTIONS (partitioned by created_at)
-- FKs to partitioned tables removed — replaced with indexes
-- ==============================================================================

CREATE TABLE IF NOT EXISTS payment_transactions (
    id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    order_id UUID,  -- logically references purchase_orders(id), no FK on partitioned table
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    payment_method_id UUID REFERENCES user_payment_methods(id),
    amount NUMERIC(12, 2) NOT NULL,
    currency VARCHAR(3) DEFAULT 'INR',
    gateway VARCHAR(50) NOT NULL,
    gateway_txn_id VARCHAR(255),
    gateway_status VARCHAR(50),
    status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending','success','failed','refunded','disputed')),
    failure_reason TEXT,
    refund_amount NUMERIC(12, 2),
    refunded_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

CREATE INDEX IF NOT EXISTS idx_payment_txn_order ON payment_transactions(order_id);

DO $$
DECLARE
    start_date DATE; end_date DATE; partition_name TEXT;
BEGIN
    FOR i IN 0..13 LOOP
        start_date := DATE_TRUNC('month', CURRENT_DATE + (i || ' months')::interval);
        end_date := start_date + INTERVAL '1 month';
        partition_name := 'payment_transactions_' || TO_CHAR(start_date, 'YYYY_MM');
        IF NOT EXISTS (SELECT 1 FROM pg_tables WHERE tablename = partition_name AND schemaname = 'public') THEN
            EXECUTE format('CREATE TABLE %I PARTITION OF payment_transactions FOR VALUES FROM (%L) TO (%L)', partition_name, start_date, end_date);
            EXECUTE format('CREATE INDEX %I ON %I (user_id, status)', partition_name || '_user_status', partition_name);
        END IF;
    END LOOP;
END $$;

CREATE TABLE IF NOT EXISTS user_credit_profiles (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    trust_score INT DEFAULT 300 CHECK (trust_score >= 0 AND trust_score <= 1000),
    score_version INT DEFAULT 1,
    last_scored_at TIMESTAMPTZ,
    payment_reliability_score INT DEFAULT 0 CHECK (payment_reliability_score >= 0 AND payment_reliability_score <= 100),
    purchase_history_score INT DEFAULT 0 CHECK (purchase_history_score >= 0 AND purchase_history_score <= 100),
    account_stability_score INT DEFAULT 0 CHECK (account_stability_score >= 0 AND account_stability_score <= 100),
    dispute_rate_score INT DEFAULT 0 CHECK (dispute_rate_score >= 0 AND dispute_rate_score <= 100),
    kyc_boost_score INT DEFAULT 0 CHECK (kyc_boost_score >= 0 AND kyc_boost_score <= 100),
    is_flagged BOOLEAN DEFAULT FALSE,
    flag_reason TEXT,
    flagged_at TIMESTAMPTZ,
    auto_buy_eligible BOOLEAN GENERATED ALWAYS AS (trust_score >= 650 AND kyc_boost_score >= 50 AND NOT is_flagged) STORED,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS auto_buy_rules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    trigger_price NUMERIC(12, 2),
    trigger_drop_pct NUMERIC(5, 2),
    trigger_vendor_id UUID REFERENCES vendors(id),
    max_order_value NUMERIC(12, 2) NOT NULL DEFAULT 5000,
    preferred_payment_method_id UUID REFERENCES user_payment_methods(id),
    preferred_address_id UUID REFERENCES user_addresses(id),
    max_quantity INT DEFAULT 1,
    allow_backorders BOOLEAN DEFAULT FALSE,
    is_active BOOLEAN DEFAULT TRUE,
    expiry_date DATE,
    times_triggered INT DEFAULT 0,
    last_triggered_at TIMESTAMPTZ,
    last_executed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, product_id)
);

-- ==============================================================================
-- AUTO BUY EXECUTIONS
-- FKs to partitioned tables removed — replaced with indexes
-- ==============================================================================

CREATE TABLE IF NOT EXISTS auto_buy_executions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id UUID NOT NULL REFERENCES auto_buy_rules(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_id UUID NOT NULL REFERENCES products(id),
    offer_id UUID REFERENCES vendor_offers(id),
    trigger_type VARCHAR(20) CHECK (trigger_type IN ('price_drop','restock','scheduled','manual')),
    trigger_price NUMERIC(12, 2),
    status VARCHAR(20) DEFAULT 'initiated' CHECK (status IN (
        'initiated','credit_check_passed','credit_check_failed',
        'payment_failed','payment_success','order_placed','vendor_error','cancelled'
    )),
    trust_score_at_execution INT,
    credit_check_passed BOOLEAN,
    credit_check_reason TEXT,
    payment_method_id UUID,
    payment_txn_id UUID,  -- logically references payment_transactions(id)
    order_id UUID,         -- logically references purchase_orders(id)
    vendor_order_id VARCHAR(255),
    error_code VARCHAR(50),
    error_message TEXT,
    retry_count INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_autobuy_exec_payment_txn ON auto_buy_executions(payment_txn_id);
CREATE INDEX IF NOT EXISTS idx_autobuy_exec_order ON auto_buy_executions(order_id);

CREATE TABLE IF NOT EXISTS admin_users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(255),
    role VARCHAR(20) DEFAULT 'moderator' CHECK (role IN ('superadmin','admin','moderator','viewer')),
    is_active BOOLEAN DEFAULT TRUE,
    last_login_at TIMESTAMPTZ,
    login_attempts INT DEFAULT 0,
    locked_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS admin_audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    admin_id UUID REFERENCES admin_users(id) ON DELETE SET NULL,
    action VARCHAR(50) NOT NULL,
    entity_type VARCHAR(50),
    entity_id UUID,
    old_value JSONB,
    new_value JSONB,
    ip_address INET,
    user_agent TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_consents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    consent_type VARCHAR(50) NOT NULL,
    consent_given BOOLEAN NOT NULL,
    consent_version VARCHAR(20) NOT NULL,
    ip_address INET,
    user_agent TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, consent_type, consent_version)
);

CREATE TABLE IF NOT EXISTS user_devices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_hash VARCHAR(64) NOT NULL,
    device_type VARCHAR(20),
    os VARCHAR(50),
    app_version VARCHAR(20),
    last_seen_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, device_hash)
);

-- ==============================================================================
-- 10. AI / ML TABLES
-- ==============================================================================

CREATE TABLE IF NOT EXISTS price_predictions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    vendor_id UUID REFERENCES vendors(id) ON DELETE CASCADE,
    predicted_price NUMERIC(12, 2),
    confidence NUMERIC(3, 2) CHECK (confidence >= 0 AND confidence <= 1),
    recommendation VARCHAR(10) CHECK (recommendation IN ('BUY','WAIT','HOLD','INSUFFICIENT_DATA')),
    expected_drop_pct NUMERIC(5, 2),
    best_buy_window TEXT,
    price_trend VARCHAR(20),
    volatility NUMERIC(5, 3),
    model_used VARCHAR(50) DEFAULT 'prophet_v1',
    predicted_at TIMESTAMP DEFAULT NOW(),
    valid_until TIMESTAMPTZ DEFAULT NOW() + INTERVAL '24 hours'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_price_predictions_unique_daily
ON price_predictions (product_id, vendor_id, DATE(predicted_at));

CREATE TABLE IF NOT EXISTS search_queries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query_text VARCHAR(500) NOT NULL,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    device_hash VARCHAR(64),
    intent_classified VARCHAR(50),
    intent_confidence NUMERIC(3, 2),
    results_count INT,
    clicked_product_id UUID,
    clicked_position INT,
    response_time_ms INT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ==============================================================================
-- 11. TRIGGERS
-- ==============================================================================

DROP TRIGGER IF EXISTS trg_products_updated_at ON products;
CREATE TRIGGER trg_products_updated_at BEFORE UPDATE ON products FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS trg_vendor_offers_updated_at ON vendor_offers;
CREATE TRIGGER trg_vendor_offers_updated_at BEFORE UPDATE ON vendor_offers FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS trg_merchant_rules_updated_at ON merchant_rules;
CREATE TRIGGER trg_merchant_rules_updated_at BEFORE UPDATE ON merchant_rules FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS trg_users_updated_at ON users;
CREATE TRIGGER trg_users_updated_at BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS trg_merchant_wallets_updated_at ON merchant_wallets;
CREATE TRIGGER trg_merchant_wallets_updated_at BEFORE UPDATE ON merchant_wallets FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS trg_sponsored_placements_updated_at ON sponsored_placements;
CREATE TRIGGER trg_sponsored_placements_updated_at BEFORE UPDATE ON sponsored_placements FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS trg_user_profiles_updated_at ON user_profiles;
CREATE TRIGGER trg_user_profiles_updated_at BEFORE UPDATE ON user_profiles FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS trg_user_addresses_updated_at ON user_addresses;
CREATE TRIGGER trg_user_addresses_updated_at BEFORE UPDATE ON user_addresses FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS trg_user_payment_methods_updated_at ON user_payment_methods;
CREATE TRIGGER trg_user_payment_methods_updated_at BEFORE UPDATE ON user_payment_methods FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS trg_purchase_orders_updated_at ON purchase_orders;
CREATE TRIGGER trg_purchase_orders_updated_at BEFORE UPDATE ON purchase_orders FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS trg_payment_transactions_updated_at ON payment_transactions;
CREATE TRIGGER trg_payment_transactions_updated_at BEFORE UPDATE ON payment_transactions FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS trg_user_credit_profiles_updated_at ON user_credit_profiles;
CREATE TRIGGER trg_user_credit_profiles_updated_at BEFORE UPDATE ON user_credit_profiles FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS trg_auto_buy_rules_updated_at ON auto_buy_rules;
CREATE TRIGGER trg_auto_buy_rules_updated_at BEFORE UPDATE ON auto_buy_rules FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS trg_admin_users_updated_at ON admin_users;
CREATE TRIGGER trg_admin_users_updated_at BEFORE UPDATE ON admin_users FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ==============================================================================
-- 12. INDEXES
-- ==============================================================================

CREATE INDEX IF NOT EXISTS idx_products_title_trgm ON products USING gin(title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_products_brand ON products(brand);
CREATE INDEX IF NOT EXISTS idx_products_model ON products(model_code);
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
CREATE INDEX IF NOT EXISTS idx_products_subcategory ON products(subcategory);
CREATE INDEX IF NOT EXISTS idx_products_popularity ON products(popularity_score DESC) WHERE popularity_score > 0.3;
CREATE INDEX IF NOT EXISTS idx_products_embedding_hnsw ON products USING hnsw (title_embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_products_tsv ON products USING gin(text_search_tsv);
CREATE INDEX IF NOT EXISTS idx_products_category_popular ON products(category, popularity_score DESC) WHERE popularity_score > 0.3;

CREATE INDEX IF NOT EXISTS idx_vendors_domain ON vendors(domain);
CREATE INDEX IF NOT EXISTS idx_vendors_active ON vendors(is_active) WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_offers_product ON vendor_offers(product_id);
CREATE INDEX IF NOT EXISTS idx_offers_vendor ON vendor_offers(vendor_id);
CREATE INDEX IF NOT EXISTS idx_offers_product_price ON vendor_offers(product_id, current_price);
CREATE INDEX IF NOT EXISTS idx_offers_price ON vendor_offers(current_price);
CREATE INDEX IF NOT EXISTS idx_offers_stock ON vendor_offers(in_stock) WHERE in_stock = TRUE;
CREATE INDEX IF NOT EXISTS idx_offers_stale ON vendor_offers(last_scraped_at) WHERE in_stock = TRUE;
CREATE INDEX IF NOT EXISTS idx_offers_stale_priority ON vendor_offers(last_scraped_at ASC NULLS FIRST) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_offers_merchant ON vendor_offers(merchant_id);
CREATE INDEX IF NOT EXISTS idx_offers_sponsored ON vendor_offers(is_sponsored, sponsor_bid_id) WHERE is_sponsored = TRUE;
CREATE INDEX IF NOT EXISTS idx_offers_source ON vendor_offers(source);
CREATE INDEX IF NOT EXISTS idx_offers_partner ON vendor_offers(partner_key_id) WHERE partner_key_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_offers_search ON vendor_offers(product_id, in_stock, current_price);
CREATE INDEX IF NOT EXISTS idx_offers_data_source ON vendor_offers(data_source, product_id) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_offers_verification ON vendor_offers(verification_score DESC) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_offers_geo ON vendor_offers(geo_hash, product_id) WHERE data_source = 'user_ocr';

CREATE INDEX IF NOT EXISTS idx_price_history_offer ON price_history(offer_id);
CREATE INDEX IF NOT EXISTS idx_price_history_time ON price_history(recorded_at);
CREATE INDEX IF NOT EXISTS idx_price_history_offer_time ON price_history(offer_id, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone) WHERE phone IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_users_fcm ON users(fcm_token) WHERE fcm_token IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_users_device_hashes ON users USING gin(device_hashes);
CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active) WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_affiliate_clicks_device ON affiliate_clicks(device_hash);
CREATE INDEX IF NOT EXISTS idx_affiliate_clicks_offer ON affiliate_clicks(offer_id);
CREATE INDEX IF NOT EXISTS idx_affiliate_clicks_product ON affiliate_clicks(product_id);
CREATE INDEX IF NOT EXISTS idx_affiliate_clicks_user ON affiliate_clicks(user_id) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_affiliate_clicks_merchant ON affiliate_clicks(merchant_id);
CREATE INDEX IF NOT EXISTS idx_affiliate_clicks_status ON affiliate_clicks(status);
CREATE INDEX IF NOT EXISTS idx_affiliate_clicks_time ON affiliate_clicks(clicked_at DESC);
CREATE INDEX IF NOT EXISTS idx_affiliate_clicks_conversion ON affiliate_clicks(converted_at) WHERE converted_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_affiliate_clicks_fraud ON affiliate_clicks(fraud_score) WHERE fraud_score > 0.3;
CREATE INDEX IF NOT EXISTS idx_affiliate_clicks_match ON affiliate_clicks(device_hash, merchant_id, clicked_at DESC);

CREATE INDEX IF NOT EXISTS idx_watchlist_user ON user_watchlist(user_id);
CREATE INDEX IF NOT EXISTS idx_watchlist_product ON user_watchlist(product_id);
CREATE INDEX IF NOT EXISTS idx_watchlist_notify ON user_watchlist(notify_push) WHERE notify_push = TRUE;
CREATE INDEX IF NOT EXISTS idx_user_alerts_email ON user_alerts(user_email);
CREATE INDEX IF NOT EXISTS idx_user_alerts_product ON user_alerts(product_id);
CREATE INDEX IF NOT EXISTS idx_user_alerts_active ON user_alerts(is_active) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_user_alerts_user ON user_alerts(user_id) WHERE user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_push_notif_pending ON push_notifications(status, scheduled_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_push_notif_user ON push_notifications(user_id);
CREATE INDEX IF NOT EXISTS idx_push_notif_failed ON push_notifications(status, retry_count) WHERE status = 'failed' AND retry_count < 3;

CREATE INDEX IF NOT EXISTS idx_merchant_rules_merchant ON merchant_rules(merchant_id);
CREATE INDEX IF NOT EXISTS idx_merchant_rules_product ON merchant_rules(product_id);
CREATE INDEX IF NOT EXISTS idx_merchant_rules_active ON merchant_rules(is_active) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_audit_logs_merchant_product ON price_audit_logs(merchant_id, product_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_time ON price_audit_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_logs_circuit ON price_audit_logs(circuit_breaker_tripped) WHERE circuit_breaker_tripped = TRUE;

CREATE INDEX IF NOT EXISTS idx_ocr_device ON ocr_submissions(device_hash);
CREATE INDEX IF NOT EXISTS idx_ocr_status ON ocr_submissions(status);
CREATE INDEX IF NOT EXISTS idx_ocr_created ON ocr_submissions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ocr_matched ON ocr_submissions(matched_product_id) WHERE matched_product_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ocr_fraud ON ocr_submissions(fraud_score) WHERE fraud_score > 0.3;
CREATE INDEX IF NOT EXISTS idx_ocr_extracted ON ocr_submissions USING gin(ai_extracted_data);
CREATE INDEX IF NOT EXISTS idx_pending_matches_status ON pending_matches(status) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_user_submissions_user ON user_submissions(user_id, submitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_submissions_status ON user_submissions(status, submitted_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_user_submissions_geo ON user_submissions(geo_hash, submitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_submissions_screenshot ON user_submissions(screenshot_hash, vendor_domain) WHERE screenshot_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_validation_votes_submission ON user_validation_votes(submission_id, upvote);
CREATE INDEX IF NOT EXISTS idx_validation_votes_voter ON user_validation_votes(device_hash, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_partner_keys_merchant ON partner_api_keys(merchant_id);
CREATE INDEX IF NOT EXISTS idx_partner_keys_active ON partner_api_keys(api_key_hash) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_partner_keys_prefix ON partner_api_keys(api_key_prefix);
CREATE INDEX IF NOT EXISTS idx_feed_logs_partner ON partner_feed_logs(partner_key_id);
CREATE INDEX IF NOT EXISTS idx_feed_logs_time ON partner_feed_logs(processed_at DESC);
CREATE INDEX IF NOT EXISTS idx_claims_merchant ON merchant_claims(merchant_id);
CREATE INDEX IF NOT EXISTS idx_claims_product ON merchant_claims(product_id);
CREATE INDEX IF NOT EXISTS idx_claims_status ON merchant_claims(status) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_merchant_partners_domain ON merchant_partners(domain) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_merchant_partners_status ON merchant_partners(onboarding_status);
CREATE INDEX IF NOT EXISTS idx_feed_submissions_partner ON merchant_feed_submissions(partner_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_sponsored_merchant ON sponsored_placements(merchant_id);
CREATE INDEX IF NOT EXISTS idx_sponsored_product ON sponsored_placements(product_id);
CREATE INDEX IF NOT EXISTS idx_sponsored_keyword ON sponsored_placements(keyword) WHERE keyword IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sponsored_active ON sponsored_placements(is_active, start_date, end_date) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_sponsored_bid ON sponsored_placements(bid_amount DESC) WHERE is_active = TRUE AND daily_spend < daily_budget;
CREATE INDEX IF NOT EXISTS idx_wallet_txn_merchant ON wallet_transactions(merchant_id);
CREATE INDEX IF NOT EXISTS idx_wallet_txn_time ON wallet_transactions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wallet_txn_type ON wallet_transactions(type);

CREATE INDEX IF NOT EXISTS idx_predictions_product ON price_predictions(product_id);
CREATE INDEX IF NOT EXISTS idx_predictions_valid ON price_predictions(valid_until);
CREATE INDEX IF NOT EXISTS idx_predictions_confidence ON price_predictions(confidence DESC) WHERE confidence > 0.6;
CREATE INDEX IF NOT EXISTS idx_search_queries_text ON search_queries USING gin(query_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_search_queries_user ON search_queries(user_id) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_search_queries_time ON search_queries(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_search_queries_intent ON search_queries(intent_classified) WHERE intent_classified IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_autobuy_user ON auto_buy_rules(user_id);
CREATE INDEX IF NOT EXISTS idx_autobuy_product ON auto_buy_rules(product_id);
CREATE INDEX IF NOT EXISTS idx_autobuy_active ON auto_buy_rules(is_active) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_autobuy_expiry ON auto_buy_rules(expiry_date) WHERE expiry_date IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_autobuy_exec_rule ON auto_buy_executions(rule_id);
CREATE INDEX IF NOT EXISTS idx_autobuy_exec_user ON auto_buy_executions(user_id);
CREATE INDEX IF NOT EXISTS idx_autobuy_exec_status ON auto_buy_executions(status);
CREATE INDEX IF NOT EXISTS idx_autobuy_exec_created ON auto_buy_executions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_profiles_kyc ON user_profiles(kyc_status) WHERE kyc_status = 'pending';
CREATE INDEX IF NOT EXISTS idx_user_profiles_auto_buy ON user_profiles(auto_buy_enabled) WHERE auto_buy_enabled = TRUE;
CREATE INDEX IF NOT EXISTS idx_user_profiles_tier ON user_profiles(account_tier);
CREATE INDEX IF NOT EXISTS idx_user_addresses_user ON user_addresses(user_id);
CREATE INDEX IF NOT EXISTS idx_user_addresses_default ON user_addresses(user_id, is_default) WHERE is_default = TRUE;
CREATE INDEX IF NOT EXISTS idx_payment_methods_user ON user_payment_methods(user_id);
CREATE INDEX IF NOT EXISTS idx_payment_methods_active ON user_payment_methods(is_active) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_payment_methods_default ON user_payment_methods(user_id, is_default) WHERE is_default = TRUE;
CREATE INDEX IF NOT EXISTS idx_credit_profiles_score ON user_credit_profiles(trust_score DESC);
CREATE INDEX IF NOT EXISTS idx_credit_profiles_eligible ON user_credit_profiles(auto_buy_eligible) WHERE auto_buy_eligible = TRUE;
CREATE INDEX IF NOT EXISTS idx_credit_profiles_flagged ON user_credit_profiles(is_flagged) WHERE is_flagged = TRUE;
CREATE INDEX IF NOT EXISTS idx_purchase_orders_user ON purchase_orders(user_id);
CREATE INDEX IF NOT EXISTS idx_purchase_orders_status ON purchase_orders(status);
CREATE INDEX IF NOT EXISTS idx_purchase_orders_user_status ON purchase_orders(user_id, status);
CREATE INDEX IF NOT EXISTS idx_purchase_orders_created ON purchase_orders(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_payment_txn_order ON payment_transactions(order_id);
CREATE INDEX IF NOT EXISTS idx_payment_txn_user ON payment_transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_payment_txn_status ON payment_transactions(status);
CREATE INDEX IF NOT EXISTS idx_payment_txn_gateway ON payment_transactions(gateway, gateway_txn_id);

CREATE INDEX IF NOT EXISTS idx_admin_email ON admin_users(email);
CREATE INDEX IF NOT EXISTS idx_admin_role ON admin_users(role);
CREATE INDEX IF NOT EXISTS idx_admin_active ON admin_users(is_active) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_audit_admin ON admin_audit_logs(admin_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON admin_audit_logs(action);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON admin_audit_logs(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_created ON admin_audit_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_consents_user ON user_consents(user_id);
CREATE INDEX IF NOT EXISTS idx_consents_type ON user_consents(consent_type);
CREATE INDEX IF NOT EXISTS idx_user_devices_hash ON user_devices(device_hash);
CREATE INDEX IF NOT EXISTS idx_user_devices_user ON user_devices(user_id);
CREATE INDEX IF NOT EXISTS idx_scrape_dlq_created ON scrape_dlq(created_at);
CREATE INDEX IF NOT EXISTS idx_match_feedback_offer ON match_feedback(offer_id);
CREATE INDEX IF NOT EXISTS idx_merchant_api_keys_merchant ON merchant_api_keys(merchant_id);
CREATE INDEX IF NOT EXISTS idx_takedown_domain ON takedown_log(domain, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_takedown_status ON takedown_log(status, received_at) WHERE status IN ('received','under_review');

-- ==============================================================================
-- 13. VIEWS
-- ==============================================================================

CREATE OR REPLACE VIEW trusted_offers AS
SELECT vo.*,
    CASE vo.data_source
        WHEN 'official_api' THEN 100
        WHEN 'merchant_partner' THEN 90
        WHEN 'affiliate_feed' THEN 80
        WHEN 'user_ocr' THEN 50
        WHEN 'scraped' THEN 30
    END AS source_priority
FROM vendor_offers vo
WHERE vo.is_active = TRUE AND (vo.expires_at IS NULL OR vo.expires_at > NOW());

CREATE OR REPLACE VIEW v_auto_buy_eligible_rules AS
SELECT
    abr.id AS rule_id, abr.user_id, abr.product_id, abr.trigger_price,
    abr.trigger_drop_pct, abr.max_order_value, abr.preferred_payment_method_id,
    abr.preferred_address_id, abr.max_quantity, ucp.trust_score,
    ucp.auto_buy_eligible, up.max_auto_buy_order_value AS user_max_order,
    up.daily_auto_buy_limit, up.monthly_auto_buy_limit
FROM auto_buy_rules abr
JOIN user_credit_profiles ucp ON abr.user_id = ucp.user_id
JOIN user_profiles up ON abr.user_id = up.user_id
WHERE abr.is_active = TRUE AND ucp.auto_buy_eligible = TRUE
    AND (abr.expiry_date IS NULL OR abr.expiry_date >= CURRENT_DATE);

CREATE OR REPLACE VIEW v_user_purchase_summary AS
SELECT
    user_id,
    COUNT(*) FILTER (WHERE status IN ('delivered','order_placed')) AS total_confirmed_orders,
    COUNT(*) FILTER (WHERE status IN ('cancelled','refunded')) AS total_disputed_orders,
    COALESCE(AVG(order_value) FILTER (WHERE status IN ('delivered','order_placed')), 0) AS avg_order_value,
    COALESCE(SUM(order_value) FILTER (WHERE status IN ('delivered','order_placed')), 0) AS lifetime_value,
    MAX(placed_at) AS last_order_at
FROM purchase_orders GROUP BY user_id;

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
-- 14. MATERIALIZED VIEW
-- ==============================================================================

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_best_prices AS
SELECT DISTINCT ON (p.id)
    p.id AS product_id, p.title, p.brand, p.image_url, p.category, p.popularity_score,
    vo.id AS best_offer_id, vo.current_price AS best_price, vo.currency, vo.vendor_id,
    v.name AS vendor_name, vo.mrp,
    CASE WHEN vo.mrp > 0 THEN ROUND(((vo.mrp - vo.current_price) / vo.mrp * 100), 1) ELSE 0 END AS discount_pct,
    vo.in_stock, vo.is_sponsored, vo.product_url, vo.last_scraped_at
FROM products p
JOIN vendor_offers vo ON vo.product_id = p.id
JOIN vendors v ON vo.vendor_id = v.id
WHERE vo.in_stock = TRUE
ORDER BY p.id, vo.current_price ASC;

CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_best_prices_product ON mv_best_prices(product_id);
CREATE INDEX IF NOT EXISTS idx_mv_best_prices_category ON mv_best_prices(category);
CREATE INDEX IF NOT EXISTS idx_mv_best_prices_price ON mv_best_prices(best_price);

-- ==============================================================================
-- 15. CREDIT SCORING FUNCTION
-- ==============================================================================

CREATE OR REPLACE FUNCTION recalculate_user_trust_score(p_user_id UUID)
RETURNS VOID AS $$
DECLARE
    v_payment_reliability INT; v_purchase_history INT; v_account_stability INT;
    v_dispute_rate INT; v_kyc_boost INT; v_total INT;
BEGIN
    SELECT COALESCE(ROUND(COUNT(*) FILTER (WHERE status = 'success')::numeric / NULLIF(COUNT(*), 0) * 100), 0)
    INTO v_payment_reliability FROM payment_transactions WHERE user_id = p_user_id;

    SELECT COALESCE(LEAST(LN(1 + COUNT(*)) * 20, 100)::int + LEAST((AVG(order_value) / 100)::int, 20), 0)
    INTO v_purchase_history FROM purchase_orders
    WHERE user_id = p_user_id AND status IN ('delivered','order_placed','payment_confirmed');

    SELECT LEAST((EXTRACT(DAY FROM NOW() - created_at) / 90 * 100)::int, 100)
    INTO v_account_stability FROM users WHERE id = p_user_id;

    SELECT COALESCE(GREATEST(0, 100 - (COUNT(*) FILTER (WHERE status IN ('cancelled','refunded'))::numeric / NULLIF(COUNT(*), 0) * 500))::int, 100)
    INTO v_dispute_rate FROM purchase_orders WHERE user_id = p_user_id;

    SELECT CASE WHEN kyc_status = 'verified' THEN 100 WHEN kyc_status = 'pending' THEN 50 ELSE 0 END
    INTO v_kyc_boost FROM user_profiles WHERE user_id = p_user_id;

    v_total := (v_payment_reliability * 0.35 + v_purchase_history * 0.25 + v_account_stability * 0.15 + v_dispute_rate * 0.15 + v_kyc_boost * 0.10)::int;

    INSERT INTO user_credit_profiles (
        user_id, trust_score, score_version, payment_reliability_score, purchase_history_score,
        account_stability_score, dispute_rate_score, kyc_boost_score, last_scored_at, updated_at
    ) VALUES (p_user_id, v_total, 1, v_payment_reliability, v_purchase_history,
        v_account_stability, v_dispute_rate, v_kyc_boost, NOW(), NOW())
    ON CONFLICT (user_id) DO UPDATE SET
        trust_score = EXCLUDED.trust_score, score_version = EXCLUDED.score_version,
        payment_reliability_score = EXCLUDED.payment_reliability_score,
        purchase_history_score = EXCLUDED.purchase_history_score,
        account_stability_score = EXCLUDED.account_stability_score,
        dispute_rate_score = EXCLUDED.dispute_rate_score,
        kyc_boost_score = EXCLUDED.kyc_boost_score,
        last_scored_at = EXCLUDED.last_scored_at, updated_at = NOW();
END;
$$ LANGUAGE plpgsql;

-- ==============================================================================
-- 16. BACKFILL & DEFAULTS
-- ==============================================================================

UPDATE vendor_offers SET match_status = 'pending_review' WHERE match_status IS NULL;
UPDATE vendors SET scraping_allowed = TRUE, scrape_rpm = 6, stock_text_present = 'in stock', updated_at = NOW() WHERE scraping_allowed IS NULL;
UPDATE vendor_offers SET is_active = TRUE WHERE is_active IS NULL;
UPDATE vendor_offers SET data_source = 'scraped', verification_score = 30 WHERE data_source = 'scraped' AND verification_score = 0;

-- ==============================================================================
-- 17. DONE
-- ==============================================================================
DO $$ BEGIN RAISE NOTICE 'PCE Unified Schema v3.2 applied successfully.'; END $$;