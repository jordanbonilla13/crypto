CREATE TABLE IF NOT EXISTS experiment_baselines (
    id BIGSERIAL PRIMARY KEY,
    experiment_type TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    frozen_at TIMESTAMPTZ NOT NULL,
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    configuration JSONB NOT NULL DEFAULT '{}'::jsonb,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS microstructure_signals (
    id BIGSERIAL PRIMARY KEY,
    signal_key TEXT NOT NULL UNIQUE,
    detected_at TIMESTAMPTZ NOT NULL,
    market TEXT NOT NULL REFERENCES markets(market),
    base_asset TEXT NOT NULL,
    quote_asset TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    partition TEXT NOT NULL,
    predicted_direction TEXT NOT NULL,
    score NUMERIC(18, 10) NOT NULL,
    spread_pct NUMERIC(18, 10) NOT NULL,
    depth_quote NUMERIC(38, 18) NOT NULL,
    liquidity_level TEXT NOT NULL,
    volatility_pct NUMERIC(18, 10) NOT NULL,
    volume_quote NUMERIC(38, 18) NOT NULL,
    imbalance_ratio NUMERIC(18, 10) NOT NULL,
    imbalance_pct NUMERIC(18, 10) NOT NULL,
    depth_change_pct NUMERIC(18, 10) NOT NULL,
    price_jump_pct NUMERIC(18, 10) NOT NULL,
    large_order_share_pct NUMERIC(18, 10) NOT NULL,
    hour_of_day INTEGER NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_experiment_baselines_type ON experiment_baselines (experiment_type);
CREATE INDEX IF NOT EXISTS idx_microstructure_detected_at ON microstructure_signals (detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_microstructure_type_time ON microstructure_signals (signal_type, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_microstructure_market_time ON microstructure_signals (market, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_microstructure_partition_type ON microstructure_signals (partition, signal_type, detected_at DESC);
