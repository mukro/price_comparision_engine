-- scripts/migrate_v2_add_source_tracking.sql
-- ==========================================
-- Migration: Add data source tracking & user OCR support
-- Run this after migrate_v1_to_v2.sql
-- ==========================================

BEGIN;

-- ==========================================
-- 1. vendor_offers: add source tracking
-- ==========================================
ALTER TABLE vendor_offers
    ADD COLUMN IF NOT EXISTS data_source VARCHAR(20) NOT NULL DEFAULT 'scraped'
        CHECK (data_source IN ('official_api', 'affiliate_feed', 'merchant_partner', 'user_ocr', 'scraped')),
    ADD COLUMN IF NOT EXISTS verification_score INT DEFAULT 0
        CHECK (verification_score BETWEEN 0 AND 100),
    ADD COLUMN IF NOT EXISTS data_provenance JSONB DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS submitted_by_user_id VARCHAR(255),
    ADD COLUMN IF NOT EXISTS geo_hash VARCHAR(12),
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP WITH TIME ZONE;

CREATE INDEX IF NOT EXISTS idx_offers_data_source 
    ON vendor_offers(data_source, product_id) 
    WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_offers_verification 
    ON vendor_offers(verification_score DESC) 
    WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_offers_geo 
    ON vendor_offers(geo_hash, product_id) 
    WHERE data_source = 'user_ocr';

-- ==========================================
-- 2. New table: user_submissions (OCR audit trail)
-- ==========================================
CREATE TABLE IF NOT EXISTS user_submissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id VARCHAR(255) NOT NULL,
    vendor_domain VARCHAR(255) NOT NULL,
    extracted_price NUMERIC(12, 2),
    extracted_currency VARCHAR(10) DEFAULT 'INR',
    extracted_product_name VARCHAR(500),
    extracted_stock_status BOOLEAN,
    geo_hash VARCHAR(12),
    submitted_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    verification_score INT DEFAULT 0 CHECK (verification_score BETWEEN 0 AND 100),
    reviewed_by_admin BOOLEAN DEFAULT FALSE,
    admin_notes TEXT,
    device_os VARCHAR(50),
    app_version VARCHAR(20),
    ocr_confidence NUMERIC(4, 3),
    ocr_engine VARCHAR(50),
    screenshot_hash VARCHAR(64),
    status VARCHAR(20) DEFAULT 'pending' 
        CHECK (status IN ('pending', 'approved', 'rejected', 'merged')),
    merged_offer_id UUID REFERENCES vendor_offers(id),
    merged_at TIMESTAMP WITH TIME ZONE,
    device_hash VARCHAR(64) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_user_submissions_user 
    ON user_submissions(user_id, submitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_submissions_status 
    ON user_submissions(status, submitted_at) 
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_user_submissions_geo 
    ON user_submissions(geo_hash, submitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_submissions_screenshot 
    ON user_submissions(screenshot_hash, vendor_domain) 
    WHERE screenshot_hash IS NOT NULL;

-- ==========================================
-- 3. New table: user_validation_votes
-- ==========================================
CREATE TABLE IF NOT EXISTS user_validation_votes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    submission_id UUID NOT NULL REFERENCES user_submissions(id) ON DELETE CASCADE,
    device_hash VARCHAR(64) NOT NULL,
    upvote BOOLEAN NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(submission_id, device_hash)
);

CREATE INDEX IF NOT EXISTS idx_validation_votes_submission 
    ON user_validation_votes(submission_id, upvote);
CREATE INDEX IF NOT EXISTS idx_validation_votes_voter 
    ON user_validation_votes(device_hash, created_at DESC);

-- ==========================================
-- 4. New table: merchant_partners
-- ==========================================
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
    kyc_verified_at TIMESTAMP WITH TIME ZONE,
    api_key_hash VARCHAR(255),
    webhook_url VARCHAR(500),
    webhook_secret VARCHAR(255),
    plan_type VARCHAR(20) DEFAULT 'free' 
        CHECK (plan_type IN ('free', 'basic', 'premium', 'enterprise')),
    is_active BOOLEAN DEFAULT TRUE,
    onboarding_status VARCHAR(20) DEFAULT 'pending' 
        CHECK (onboarding_status IN ('pending', 'kyc', 'approved', 'suspended')),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_feed_received_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_merchant_partners_domain 
    ON merchant_partners(domain) 
    WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_merchant_partners_status 
    ON merchant_partners(onboarding_status);

-- ==========================================
-- 5. New table: merchant_feed_submissions
-- ==========================================
CREATE TABLE IF NOT EXISTS merchant_feed_submissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    partner_id UUID NOT NULL REFERENCES merchant_partners(id),
    feed_type VARCHAR(20) DEFAULT 'full' 
        CHECK (feed_type IN ('full', 'delta', 'price_update', 'stock_update')),
    items_count INT DEFAULT 0,
    items_accepted INT DEFAULT 0,
    items_rejected INT DEFAULT 0,
    status VARCHAR(20) DEFAULT 'received' 
        CHECK (status IN ('received', 'processing', 'completed', 'failed')),
    processing_started_at TIMESTAMP WITH TIME ZONE,
    processing_completed_at TIMESTAMP WITH TIME ZONE,
    error_message TEXT,
    source_ip INET,
    user_agent TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_feed_submissions_partner 
    ON merchant_feed_submissions(partner_id, created_at DESC);

-- ==========================================
-- 6. New table: takedown_log
-- ==========================================
CREATE TABLE IF NOT EXISTS takedown_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id VARCHAR(50) UNIQUE NOT NULL,
    domain VARCHAR(255) NOT NULL,
    requester_email VARCHAR(255) NOT NULL,
    requester_name VARCHAR(255),
    legal_basis VARCHAR(50),
    specific_urls TEXT[],
    status VARCHAR(20) DEFAULT 'received' 
        CHECK (status IN ('received', 'under_review', 'action_taken', 'rejected', 'appealed')),
    action_taken TEXT,
    received_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    acknowledged_at TIMESTAMP WITH TIME ZONE,
    resolved_at TIMESTAMP WITH TIME ZONE,
    assigned_to VARCHAR(255),
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_takedown_domain 
    ON takedown_log(domain, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_takedown_status 
    ON takedown_log(status, received_at) 
    WHERE status IN ('received', 'under_review');

-- ==========================================
-- 7. Update existing data
-- ==========================================
UPDATE vendor_offers 
SET data_source = 'scraped', 
    verification_score = 30 
WHERE data_source = 'scraped' AND verification_score = 0;

-- ==========================================
-- 8. Add source_priority view for queries
-- ==========================================
CREATE OR REPLACE VIEW trusted_offers AS
SELECT 
    vo.*,
    CASE vo.data_source
        WHEN 'official_api' THEN 100
        WHEN 'merchant_partner' THEN 90
        WHEN 'affiliate_feed' THEN 80
        WHEN 'user_ocr' THEN 50
        WHEN 'scraped' THEN 30
    END AS source_priority
FROM vendor_offers vo
WHERE vo.is_active = TRUE
  AND (vo.expires_at IS NULL OR vo.expires_at > NOW());

COMMIT;
