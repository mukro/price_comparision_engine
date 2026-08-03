-- ============================================
-- PCE Data Quality Agent — Database Migration
-- Add these tables to your existing PostgreSQL schema
-- ============================================

-- Track which offers were auto-matched by the agent
ALTER TABLE vendor_offers
ADD COLUMN IF NOT EXISTS agent_matched BOOLEAN DEFAULT FALSE,
ADD COLUMN IF NOT EXISTS agent_matched_at TIMESTAMP WITH TIME ZONE,
ADD COLUMN IF NOT EXISTS agent_reasoning TEXT,
ADD COLUMN IF NOT EXISTS agent_suggestions JSONB,  -- Array of suggested product matches
ADD COLUMN IF NOT EXISTS agent_reviewed_at TIMESTAMP WITH TIME ZONE;

-- Track vendor selector health over time
CREATE TABLE IF NOT EXISTS agent_selector_health_logs (
    id SERIAL PRIMARY KEY,
    vendor_id UUID NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
    status VARCHAR(20) NOT NULL CHECK (status IN ('healthy', 'degraded', 'broken')),
    failure_reason TEXT,
    suggested_fix TEXT,
    checked_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_health_vendor ON agent_selector_health_logs(vendor_id);
CREATE INDEX IF NOT EXISTS idx_agent_health_status ON agent_selector_health_logs(status);
CREATE INDEX IF NOT EXISTS idx_agent_health_checked_at ON agent_selector_health_logs(checked_at);

-- Track agent decisions for audit and quality improvement
CREATE TABLE IF NOT EXISTS agent_decision_logs (
    id SERIAL PRIMARY KEY,
    offer_id UUID REFERENCES vendor_offers(id) ON DELETE SET NULL,
    action VARCHAR(30) NOT NULL,  -- auto_match | suggest_match | new_product | needs_human
    target_product_id UUID REFERENCES products(id) ON DELETE SET NULL,
    confidence DECIMAL(3,2),  -- 0.00 to 1.00
    reasoning TEXT,
    llm_model VARCHAR(50),  -- gpt-4o-mini | claude-3-haiku
    was_correct BOOLEAN,  -- Filled later by human review feedback
    human_overridden BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_decisions_offer ON agent_decision_logs(offer_id);
CREATE INDEX IF NOT EXISTS idx_agent_decisions_action ON agent_decision_logs(action);
CREATE INDEX IF NOT EXISTS idx_agent_decisions_created ON agent_decision_logs(created_at);

-- Track agent performance metrics (for Grafana dashboard)
CREATE TABLE IF NOT EXISTS agent_performance_metrics (
    id SERIAL PRIMARY KEY,
    run_timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    pipeline_mode VARCHAR(20) NOT NULL,  -- entity_resolution | selector_health | both
    offers_processed INTEGER DEFAULT 0,
    offers_escalated INTEGER DEFAULT 0,
    offers_auto_matched INTEGER DEFAULT 0,
    new_products_created INTEGER DEFAULT 0,
    vendors_checked INTEGER DEFAULT 0,
    vendors_healthy INTEGER DEFAULT 0,
    vendors_degraded INTEGER DEFAULT 0,
    vendors_broken INTEGER DEFAULT 0,
    selectors_auto_fixed INTEGER DEFAULT 0,
    errors_count INTEGER DEFAULT 0,
    elapsed_seconds DECIMAL(8,2),
    llm_tokens_used INTEGER DEFAULT 0,
    llm_cost_usd DECIMAL(8,4) DEFAULT 0.0000
);

CREATE INDEX IF NOT EXISTS idx_agent_perf_run ON agent_performance_metrics(run_timestamp);
CREATE INDEX IF NOT EXISTS idx_agent_perf_mode ON agent_performance_metrics(pipeline_mode);

-- Update vendors table to track agent selector updates
ALTER TABLE vendors
ADD COLUMN IF NOT EXISTS selector_last_updated_by_agent TIMESTAMP WITH TIME ZONE,
ADD COLUMN IF NOT EXISTS selector_update_reason TEXT;

-- Add a view for the human review queue (offers agent reviewed but needs human decision)
CREATE OR REPLACE VIEW human_review_queue AS
SELECT 
    vo.id AS offer_id,
    vo.raw_title,
    vo.current_price,
    vo.product_url,
    v.name AS vendor_name,
    vo.agent_suggestions,
    vo.agent_reasoning,
    vo.agent_reviewed_at,
    vo.created_at AS offer_created_at,
    EXTRACT(EPOCH FROM (NOW() - vo.created_at))/3600 AS hours_pending
FROM vendor_offers vo
JOIN vendors v ON vo.vendor_id = v.id
WHERE vo.match_status = 'agent_reviewed'
ORDER BY vo.agent_reviewed_at DESC;

-- Add a view for agent accuracy tracking
CREATE OR REPLACE VIEW agent_accuracy_report AS
SELECT 
    DATE_TRUNC('day', created_at) AS date,
    action,
    COUNT(*) AS total_decisions,
    SUM(CASE WHEN was_correct = TRUE THEN 1 ELSE 0 END) AS correct_count,
    SUM(CASE WHEN was_correct = FALSE THEN 1 ELSE 0 END) AS incorrect_count,
    SUM(CASE WHEN human_overridden = TRUE THEN 1 ELSE 0 END) AS overridden_count,
    ROUND(AVG(confidence)::numeric, 3) AS avg_confidence,
    ROUND(
        (SUM(CASE WHEN was_correct = TRUE THEN 1 ELSE 0 END)::decimal / NULLIF(COUNT(*), 0)) * 100, 
        2
    ) AS accuracy_pct
FROM agent_decision_logs
WHERE created_at >= NOW() - INTERVAL '30 days'
GROUP BY DATE_TRUNC('day', created_at), action
ORDER BY date DESC, action;
