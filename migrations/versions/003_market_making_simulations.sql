CREATE TABLE IF NOT EXISTS market_making_simulations (
    id BIGSERIAL PRIMARY KEY,
    simulation_key TEXT NOT NULL UNIQUE,
    signal_at TIMESTAMPTZ NOT NULL,
    market TEXT NOT NULL REFERENCES markets(market),
    base_asset TEXT NOT NULL,
    quote_asset TEXT NOT NULL,
    capital NUMERIC(38, 18) NOT NULL,
    spread_pct NUMERIC(18, 10) NOT NULL,
    score NUMERIC(18, 10) NOT NULL,
    liquidity_level TEXT NOT NULL,
    volatility_bucket TEXT NOT NULL,
    volatility_pct NUMERIC(18, 10) NOT NULL,
    imbalance_bucket TEXT NOT NULL,
    imbalance_pct NUMERIC(18, 10) NOT NULL,
    hour_of_day INTEGER NOT NULL,
    execution TEXT NOT NULL,
    executable BOOLEAN NOT NULL DEFAULT FALSE,
    discarded BOOLEAN NOT NULL DEFAULT FALSE,
    realized_pnl NUMERIC(38, 18) NOT NULL,
    exposure_time_ms INTEGER NOT NULL DEFAULT 0,
    max_capital_exposed NUMERIC(38, 18) NOT NULL DEFAULT 0,
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_mm_simulations_signal_at ON market_making_simulations (signal_at DESC);
CREATE INDEX IF NOT EXISTS idx_mm_simulations_market_time ON market_making_simulations (market, signal_at DESC);
CREATE INDEX IF NOT EXISTS idx_mm_simulations_capital ON market_making_simulations (capital, signal_at DESC);
