ALTER TABLE opportunities
    ADD COLUMN IF NOT EXISTS capital NUMERIC(38, 18),
    ADD COLUMN IF NOT EXISTS gross_profit_pct NUMERIC(18, 10),
    ADD COLUMN IF NOT EXISTS net_profit_pct NUMERIC(18, 10),
    ADD COLUMN IF NOT EXISTS total_fee_amount NUMERIC(38, 18),
    ADD COLUMN IF NOT EXISTS slippage_pct NUMERIC(18, 10),
    ADD COLUMN IF NOT EXISTS duration_ms INTEGER,
    ADD COLUMN IF NOT EXISTS classification TEXT,
    ADD COLUMN IF NOT EXISTS market_path TEXT[],
    ADD COLUMN IF NOT EXISTS details JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_opportunities_experiment_detected ON opportunities (experiment_type, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_opportunities_net_pct ON opportunities (net_profit_pct DESC);
