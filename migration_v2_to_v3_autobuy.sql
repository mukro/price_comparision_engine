-- ==============================================================================
-- PCE Schema Migration: v2 → v3 (AutoBuy-Ready + Admin RBAC + Fixes)
-- Run AFTER init.sql and schema.sql are applied
-- ==============================================================================

BEGIN;

-- ==============================================================================
-- FIXES TO EXISTING SCHEMA
-- ==============================================================================

-- 1. Fix vendor_offers.match_status default (was 'matched', should be 'pending_review')
ALTER TABLE vendor_offers 
    ALTER COLUMN match_status SET DEFAULT 'pending_review';

-- 2. Add missing FK on affiliate_clicks.offer_id
DO $$
BEGIN
    ALTER TABLE affiliate_clicks 
        ADD CONSTRAINT fk_affiliate_clicks_offer 
        FOREIGN KEY (offer_id) REFERENCES vendor_offers(id);
EXCEPTION WHEN duplicate_object THEN
    RAISE NOTICE 'FK fk_affiliate_clicks_offer already exists';
END $$;

-- ==============================================================================
-- 1. USER PROFILES (KYC, Tier, AutoBuy eligibility)
-- ==============================================================================

CREATE TABLE IF NOT EXISTS user_profiles (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    phone_verified BOOLEAN DEFAULT FALSE,
    email_verified BOOLEAN DEFAULT FALSE,
    kyc_status VARCHAR(20) DEFAULT 'none' CHECK (kyc_status IN ('none', 'pending', 'verified', 'rejected')),
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

COMMENT ON TABLE user_profiles IS 'Extended user data: KYC, verification tiers, AutoBuy limits';
COMMENT ON COLUMN user_profiles.account_tier IS '1=browse, 2=alerts, 3=autobuy';
COMMENT ON COLUMN user_profiles.government_id_hash IS 'SHA256 of Aadhaar/PAN — never store raw';

DROP TRIGGER IF EXISTS trg_user_profiles_updated_at ON user_profiles;
CREATE TRIGGER trg_user_profiles_updated_at
    BEFORE UPDATE ON user_profiles FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE INDEX IF NOT EXISTS idx_user_profiles_kyc ON user_profiles(kyc_status) WHERE kyc_status = 'pending';
CREATE INDEX IF NOT EXISTS idx_user_profiles_auto_buy ON user_profiles(auto_buy_enabled) WHERE auto_buy_enabled = TRUE;
CREATE INDEX IF NOT EXISTS idx_user_profiles_tier ON user_profiles(account_tier);

-- ==============================================================================
-- 2. USER ADDRESSES (Shipping + Billing)
-- ==============================================================================

CREATE TABLE IF NOT EXISTS user_addresses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    address_type VARCHAR(20) DEFAULT 'shipping' CHECK (address_type IN ('shipping', 'billing', 'both')),
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

DROP TRIGGER IF EXISTS trg_user_addresses_updated_at ON user_addresses;
CREATE TRIGGER trg_user_addresses_updated_at
    BEFORE UPDATE ON user_addresses FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE INDEX IF NOT EXISTS idx_user_addresses_user ON user_addresses(user_id);
CREATE INDEX IF NOT EXISTS idx_user_addresses_default ON user_addresses(user_id, is_default) WHERE is_default = TRUE;

-- ==============================================================================
-- 3. USER PAYMENT METHODS (Tokenized — never store raw card numbers)
-- ==============================================================================

CREATE TABLE IF NOT EXISTS user_payment_methods (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    method_type VARCHAR(20) NOT NULL CHECK (method_type IN ('card', 'upi', 'wallet', 'netbanking', 'emi')),
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

COMMENT ON TABLE user_payment_methods IS 'Payment gateway tokens only — NEVER store raw card data';

DROP TRIGGER IF EXISTS trg_user_payment_methods_updated_at ON user_payment_methods;
CREATE TRIGGER trg_user_payment_methods_updated_at
    BEFORE UPDATE ON user_payment_methods FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE INDEX IF NOT EXISTS idx_payment_methods_user ON user_payment_methods(user_id);
CREATE INDEX IF NOT EXISTS idx_payment_methods_active ON user_payment_methods(is_active) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_payment_methods_default ON user_payment_methods(user_id, is_default) WHERE is_default = TRUE;

-- ==============================================================================
-- 4. PURCHASE ORDERS (Confirmed order history — ground truth for credit scoring)
-- ==============================================================================

CREATE TABLE IF NOT EXISTS purchase_orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    offer_id UUID NOT NULL REFERENCES vendor_offers(id),
    product_id UUID NOT NULL REFERENCES products(id),
    vendor_id UUID NOT NULL REFERENCES vendors(id),
    order_value NUMERIC(12, 2) NOT NULL,
    currency VARCHAR(3) DEFAULT 'INR',
    quantity INT DEFAULT 1,
    status VARCHAR(30) DEFAULT 'pending' CHECK (status IN (
        'pending', 'payment_initiated', 'payment_failed', 'payment_confirmed',
        'order_placed', 'vendor_confirmed', 'shipped', 'delivered', 'cancelled', 'refunded'
    )),
    payment_method_id UUID REFERENCES user_payment_methods(id),
    payment_gateway VARCHAR(50),
    payment_gateway_txn_id VARCHAR(255),
    payment_confirmed_at TIMESTAMPTZ,
    vendor_order_id VARCHAR(255),
    vendor_order_url TEXT,
    triggered_by VARCHAR(20) DEFAULT 'manual' CHECK (triggered_by IN ('manual', 'auto_buy_agent', 'price_alert', 'merchant_rule')),
    auto_buy_rule_id UUID,
    placed_at TIMESTAMPTZ DEFAULT NOW(),
    delivered_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    refund_amount NUMERIC(12, 2),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
) PARTITION BY RANGE (created_at);

COMMENT ON TABLE purchase_orders IS 'Confirmed purchase history — the ground truth for creditworthiness scoring';
COMMENT ON COLUMN purchase_orders.triggered_by IS 'manual=user, auto_buy_agent=AI, price_alert=watchlist trigger';

DROP TRIGGER IF EXISTS trg_purchase_orders_updated_at ON purchase_orders;
CREATE TRIGGER trg_purchase_orders_updated_at
    BEFORE UPDATE ON purchase_orders FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Monthly partitions for purchase_orders
DO $$
DECLARE
    start_date DATE;
    end_date DATE;
    partition_name TEXT;
BEGIN
    FOR i IN 0..13 LOOP
        start_date := DATE_TRUNC('month', CURRENT_DATE + (i || ' months')::interval);
        end_date := start_date + INTERVAL '1 month';
        partition_name := 'purchase_orders_' || TO_CHAR(start_date, 'YYYY_MM');

        IF NOT EXISTS (
            SELECT 1 FROM pg_tables WHERE tablename = partition_name AND schemaname = 'public'
        ) THEN
            EXECUTE format(
                'CREATE TABLE %I PARTITION OF purchase_orders FOR VALUES FROM (%L) TO (%L)',
                partition_name, start_date, end_date
            );
            EXECUTE format(
                'CREATE INDEX %I ON %I (user_id, status)',
                partition_name || '_user_status', partition_name
            );
        END IF;
    END LOOP;
END $$;

CREATE INDEX IF NOT EXISTS idx_purchase_orders_user ON purchase_orders(user_id);
CREATE INDEX IF NOT EXISTS idx_purchase_orders_status ON purchase_orders(status);
CREATE INDEX IF NOT EXISTS idx_purchase_orders_user_status ON purchase_orders(user_id, status);
CREATE INDEX IF NOT EXISTS idx_purchase_orders_created ON purchase_orders(created_at DESC);

-- ==============================================================================
-- 5. PAYMENT TRANSACTIONS (Audit trail for every rupee)
-- ==============================================================================

CREATE TABLE IF NOT EXISTS payment_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id UUID NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    payment_method_id UUID REFERENCES user_payment_methods(id),
    amount NUMERIC(12, 2) NOT NULL,
    currency VARCHAR(3) DEFAULT 'INR',
    gateway VARCHAR(50) NOT NULL,
    gateway_txn_id VARCHAR(255),
    gateway_status VARCHAR(50),
    status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending', 'success', 'failed', 'refunded', 'disputed')),
    failure_reason TEXT,
    refund_amount NUMERIC(12, 2),
    refunded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
) PARTITION BY RANGE (created_at);

COMMENT ON TABLE payment_transactions IS 'Immutable audit trail for every payment attempt';

DROP TRIGGER IF EXISTS trg_payment_transactions_updated_at ON payment_transactions;
CREATE TRIGGER trg_payment_transactions_updated_at
    BEFORE UPDATE ON payment_transactions FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Monthly partitions
DO $$
DECLARE
    start_date DATE; end_date DATE; partition_name TEXT;
BEGIN
    FOR i IN 0..13 LOOP
        start_date := DATE_TRUNC('month', CURRENT_DATE + (i || ' months')::interval);
        end_date := start_date + INTERVAL '1 month';
        partition_name := 'payment_transactions_' || TO_CHAR(start_date, 'YYYY_MM');
        IF NOT EXISTS (SELECT 1 FROM pg_tables WHERE tablename = partition_name AND schemaname = 'public') THEN
            EXECUTE format('CREATE TABLE %I PARTITION OF payment_transactions FOR VALUES FROM (%L) TO (%L)',
                partition_name, start_date, end_date);
            EXECUTE format('CREATE INDEX %I ON %I (user_id, status)',
                partition_name || '_user_status', partition_name);
        END IF;
    END LOOP;
END $$;

CREATE INDEX IF NOT EXISTS idx_payment_txn_order ON payment_transactions(order_id);
CREATE INDEX IF NOT EXISTS idx_payment_txn_user ON payment_transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_payment_txn_status ON payment_transactions(status);
CREATE INDEX IF NOT EXISTS idx_payment_txn_gateway ON payment_transactions(gateway, gateway_txn_id);

-- ==============================================================================
-- 6. USER CREDIT PROFILES (Trust score — the "credit rating")
-- ==============================================================================

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
    auto_buy_eligible BOOLEAN GENERATED ALWAYS AS (
        trust_score >= 650 AND kyc_boost_score >= 50 AND NOT is_flagged
    ) STORED,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE user_credit_profiles IS 'User creditworthiness score (0-1000). Updated nightly by batch job.';
COMMENT ON COLUMN user_credit_profiles.trust_score IS 'Like CIBIL: 300=poor, 650=eligible for AutoBuy, 750=good, 900=excellent';
COMMENT ON COLUMN user_credit_profiles.auto_buy_eligible IS 'Auto-computed: needs score >= 650 + KYC verified + not flagged';

DROP TRIGGER IF EXISTS trg_user_credit_profiles_updated_at ON user_credit_profiles;
CREATE TRIGGER trg_user_credit_profiles_updated_at
    BEFORE UPDATE ON user_credit_profiles FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE INDEX IF NOT EXISTS idx_credit_profiles_score ON user_credit_profiles(trust_score DESC);
CREATE INDEX IF NOT EXISTS idx_credit_profiles_eligible ON user_credit_profiles(auto_buy_eligible) WHERE auto_buy_eligible = TRUE;
CREATE INDEX IF NOT EXISTS idx_credit_profiles_flagged ON user_credit_profiles(is_flagged) WHERE is_flagged = TRUE;

-- ==============================================================================
-- 7. AUTO BUY RULES (User-configured autonomous purchasing triggers)
-- ==============================================================================

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

COMMENT ON TABLE auto_buy_rules IS 'User rules for autonomous purchasing when price drops or restocks';

DROP TRIGGER IF EXISTS trg_auto_buy_rules_updated_at ON auto_buy_rules;
CREATE TRIGGER trg_auto_buy_rules_updated_at
    BEFORE UPDATE ON auto_buy_rules FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE INDEX IF NOT EXISTS idx_autobuy_user ON auto_buy_rules(user_id);
CREATE INDEX IF NOT EXISTS idx_autobuy_product ON auto_buy_rules(product_id);
CREATE INDEX IF NOT EXISTS idx_autobuy_active ON auto_buy_rules(is_active) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_autobuy_expiry ON auto_buy_rules(expiry_date) WHERE expiry_date IS NOT NULL;

-- ==============================================================================
-- 8. AUTO BUY EXECUTIONS (Every attempt logged — success or failure)
-- ==============================================================================

CREATE TABLE IF NOT EXISTS auto_buy_executions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id UUID NOT NULL REFERENCES auto_buy_rules(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_id UUID NOT NULL REFERENCES products(id),
    offer_id UUID REFERENCES vendor_offers(id),
    trigger_type VARCHAR(20) CHECK (trigger_type IN ('price_drop', 'restock', 'scheduled', 'manual')),
    trigger_price NUMERIC(12, 2),
    status VARCHAR(20) DEFAULT 'initiated' CHECK (status IN (
        'initiated', 'credit_check_passed', 'credit_check_failed',
        'payment_failed', 'payment_success', 'order_placed', 'vendor_error', 'cancelled'
    )),
    trust_score_at_execution INT,
    credit_check_passed BOOLEAN,
    credit_check_reason TEXT,
    payment_method_id UUID,
    payment_txn_id UUID REFERENCES payment_transactions(id),
    order_id UUID REFERENCES purchase_orders(id),
    vendor_order_id VARCHAR(255),
    error_code VARCHAR(50),
    error_message TEXT,
    retry_count INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

COMMENT ON TABLE auto_buy_executions IS 'Immutable log of every AutoBuy attempt — critical for debugging and compliance';

CREATE INDEX IF NOT EXISTS idx_autobuy_exec_rule ON auto_buy_executions(rule_id);
CREATE INDEX IF NOT EXISTS idx_autobuy_exec_user ON auto_buy_executions(user_id);
CREATE INDEX IF NOT EXISTS idx_autobuy_exec_status ON auto_buy_executions(status);
CREATE INDEX IF NOT EXISTS idx_autobuy_exec_created ON auto_buy_executions(created_at DESC);

-- ==============================================================================
-- 9. ADMIN USERS (Replace env-based single admin)
-- ==============================================================================

CREATE TABLE IF NOT EXISTS admin_users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(255),
    role VARCHAR(20) DEFAULT 'moderator' CHECK (role IN ('superadmin', 'admin', 'moderator', 'viewer')),
    is_active BOOLEAN DEFAULT TRUE,
    last_login_at TIMESTAMPTZ,
    login_attempts INT DEFAULT 0,
    locked_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE admin_users IS 'Multi-admin RBAC — replaces single env-based admin';
COMMENT ON COLUMN admin_users.role IS 'superadmin=full, admin=queue+compliance+domains, moderator=queue only, viewer=read-only';

DROP TRIGGER IF EXISTS trg_admin_users_updated_at ON admin_users;
CREATE TRIGGER trg_admin_users_updated_at
    BEFORE UPDATE ON admin_users FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE INDEX IF NOT EXISTS idx_admin_email ON admin_users(email);
CREATE INDEX IF NOT EXISTS idx_admin_role ON admin_users(role);
CREATE INDEX IF NOT EXISTS idx_admin_active ON admin_users(is_active) WHERE is_active = TRUE;

-- Seed the first admin from env (run this after setting .env values)
-- INSERT INTO admin_users (email, password_hash, full_name, role)
-- VALUES (
--     'admin@yourdomain.com',
--     '$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW',
--     'System Administrator',
--     'superadmin'
-- )
-- ON CONFLICT (email) DO NOTHING;

-- ==============================================================================
-- 10. ADMIN AUDIT LOGS (Who did what, when)
-- ==============================================================================

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

COMMENT ON TABLE admin_audit_logs IS 'Immutable audit trail of all admin actions';

CREATE INDEX IF NOT EXISTS idx_audit_admin ON admin_audit_logs(admin_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON admin_audit_logs(action);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON admin_audit_logs(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_created ON admin_audit_logs(created_at DESC);

-- ==============================================================================
-- 11. USER CONSENTS (GDPR / DPDP compliance)
-- ==============================================================================

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

COMMENT ON TABLE user_consents IS 'User consent tracking for GDPR, DPDP, and AutoBuy authorization';

CREATE INDEX IF NOT EXISTS idx_consents_user ON user_consents(user_id);
CREATE INDEX IF NOT EXISTS idx_consents_type ON user_consents(consent_type);

-- ==============================================================================
-- 12. USER DEVICES (Normalize device_hashes from users table)
-- ==============================================================================

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

CREATE INDEX IF NOT EXISTS idx_user_devices_hash ON user_devices(device_hash);
CREATE INDEX IF NOT EXISTS idx_user_devices_user ON user_devices(user_id);

-- ==============================================================================
-- 13. CREDIT SCORE BATCH FUNCTION
-- ==============================================================================

CREATE OR REPLACE FUNCTION recalculate_user_trust_score(p_user_id UUID)
RETURNS VOID AS $$
DECLARE
    v_payment_reliability INT;
    v_purchase_history INT;
    v_account_stability INT;
    v_dispute_rate INT;
    v_kyc_boost INT;
    v_total INT;
BEGIN
    -- Payment reliability (35% weight)
    SELECT COALESCE(
        ROUND(
            COUNT(*) FILTER (WHERE status = 'success')::numeric / 
            NULLIF(COUNT(*), 0) * 100
        ), 0
    ) INTO v_payment_reliability
    FROM payment_transactions
    WHERE user_id = p_user_id;

    -- Purchase history depth (25% weight)
    SELECT COALESCE(
        LEAST(LN(1 + COUNT(*)) * 20, 100)::int + 
        LEAST((AVG(order_value) / 100)::int, 20)
    , 0) INTO v_purchase_history
    FROM purchase_orders
    WHERE user_id = p_user_id AND status IN ('delivered', 'order_placed', 'payment_confirmed');

    -- Account stability (15% weight)
    SELECT LEAST(
        (EXTRACT(DAY FROM NOW() - created_at) / 90 * 100)::int, 100
    ) INTO v_account_stability
    FROM users WHERE id = p_user_id;

    -- Dispute rate (15% weight)
    SELECT COALESCE(
        GREATEST(0, 100 - (
            COUNT(*) FILTER (WHERE status IN ('cancelled', 'refunded'))::numeric /
            NULLIF(COUNT(*), 0) * 500
        ))::int
    , 100) INTO v_dispute_rate
    FROM purchase_orders
    WHERE user_id = p_user_id;

    -- KYC boost (10% weight)
    SELECT CASE 
        WHEN kyc_status = 'verified' THEN 100
        WHEN kyc_status = 'pending' THEN 50
        ELSE 0
    END INTO v_kyc_boost
    FROM user_profiles WHERE user_id = p_user_id;

    -- Weighted total
    v_total := (
        v_payment_reliability * 0.35 +
        v_purchase_history * 0.25 +
        v_account_stability * 0.15 +
        v_dispute_rate * 0.15 +
        v_kyc_boost * 0.10
    )::int;

    INSERT INTO user_credit_profiles (
        user_id, trust_score, score_version,
        payment_reliability_score, purchase_history_score,
        account_stability_score, dispute_rate_score, kyc_boost_score,
        last_scored_at, updated_at
    ) VALUES (
        p_user_id, v_total, 1,
        v_payment_reliability, v_purchase_history,
        v_account_stability, v_dispute_rate, v_kyc_boost,
        NOW(), NOW()
    )
    ON CONFLICT (user_id) DO UPDATE SET
        trust_score = EXCLUDED.trust_score,
        score_version = EXCLUDED.score_version,
        payment_reliability_score = EXCLUDED.payment_reliability_score,
        purchase_history_score = EXCLUDED.purchase_history_score,
        account_stability_score = EXCLUDED.account_stability_score,
        dispute_rate_score = EXCLUDED.dispute_rate_score,
        kyc_boost_score = EXCLUDED.kyc_boost_score,
        last_scored_at = EXCLUDED.last_scored_at,
        updated_at = NOW();
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION recalculate_user_trust_score IS 'Nightly batch function to update user creditworthiness. Call via Celery Beat.';

-- ==============================================================================
-- 14. VIEWS FOR AUTO BUY
-- ==============================================================================

-- Active AutoBuy rules with user eligibility
CREATE OR REPLACE VIEW v_auto_buy_eligible_rules AS
SELECT 
    abr.id AS rule_id,
    abr.user_id,
    abr.product_id,
    abr.trigger_price,
    abr.trigger_drop_pct,
    abr.max_order_value,
    abr.preferred_payment_method_id,
    abr.preferred_address_id,
    abr.max_quantity,
    ucp.trust_score,
    ucp.auto_buy_eligible,
    up.max_auto_buy_order_value AS user_max_order,
    up.daily_auto_buy_limit,
    up.monthly_auto_buy_limit
FROM auto_buy_rules abr
JOIN user_credit_profiles ucp ON abr.user_id = ucp.user_id
JOIN user_profiles up ON abr.user_id = up.user_id
WHERE abr.is_active = TRUE
  AND ucp.auto_buy_eligible = TRUE
  AND (abr.expiry_date IS NULL OR abr.expiry_date >= CURRENT_DATE);

-- User purchase summary (for credit scoring)
CREATE OR REPLACE VIEW v_user_purchase_summary AS
SELECT 
    user_id,
    COUNT(*) FILTER (WHERE status IN ('delivered', 'order_placed')) AS total_confirmed_orders,
    COUNT(*) FILTER (WHERE status IN ('cancelled', 'refunded')) AS total_disputed_orders,
    COALESCE(AVG(order_value) FILTER (WHERE status IN ('delivered', 'order_placed')), 0) AS avg_order_value,
    COALESCE(SUM(order_value) FILTER (WHERE status IN ('delivered', 'order_placed')), 0) AS lifetime_value,
    MAX(placed_at) AS last_order_at
FROM purchase_orders
GROUP BY user_id;

COMMIT;

-- ==============================================================================
-- POST-MIGRATION VERIFICATION
-- ==============================================================================

-- Verify all new tables exist
SELECT 'user_profiles' AS table_name, COUNT(*) AS row_count FROM user_profiles
UNION ALL
SELECT 'user_addresses', COUNT(*) FROM user_addresses
UNION ALL
SELECT 'user_payment_methods', COUNT(*) FROM user_payment_methods
UNION ALL
SELECT 'purchase_orders', COUNT(*) FROM purchase_orders
UNION ALL
SELECT 'payment_transactions', COUNT(*) FROM payment_transactions
UNION ALL
SELECT 'user_credit_profiles', COUNT(*) FROM user_credit_profiles
UNION ALL
SELECT 'auto_buy_rules', COUNT(*) FROM auto_buy_rules
UNION ALL
SELECT 'auto_buy_executions', COUNT(*) FROM auto_buy_executions
UNION ALL
SELECT 'admin_users', COUNT(*) FROM admin_users
UNION ALL
SELECT 'admin_audit_logs', COUNT(*) FROM admin_audit_logs
UNION ALL
SELECT 'user_consents', COUNT(*) FROM user_consents
UNION ALL
SELECT 'user_devices', COUNT(*) FROM user_devices;
