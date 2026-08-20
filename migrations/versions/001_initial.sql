CREATE TABLE IF NOT EXISTS markets (
    market TEXT PRIMARY KEY,
    base_asset TEXT NOT NULL,
    quote_asset TEXT NOT NULL,
    status TEXT NOT NULL,
    price_decimals INTEGER,
    quantity_decimals INTEGER,
    min_order_in_quote NUMERIC(38, 18),
    min_order_in_base NUMERIC(38, 18),
    tick_size NUMERIC(38, 18),
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id BIGSERIAL PRIMARY KEY,
    market TEXT NOT NULL REFERENCES markets(market),
    snapshot_at TIMESTAMPTZ NOT NULL,
    best_bid NUMERIC(38, 18),
    best_bid_size NUMERIC(38, 18),
    best_ask NUMERIC(38, 18),
    best_ask_size NUMERIC(38, 18),
    spread NUMERIC(38, 18),
    source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id BIGSERIAL PRIMARY KEY,
    market TEXT NOT NULL REFERENCES markets(market),
    snapshot_at TIMESTAMPTZ NOT NULL,
    nonce BIGINT,
    bids JSONB NOT NULL,
    asks JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS fees (
    id BIGSERIAL PRIMARY KEY,
    market TEXT NOT NULL,
    taker_fee NUMERIC(18, 10) NOT NULL,
    maker_fee NUMERIC(18, 10) NOT NULL,
    source TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS opportunities (
    id BIGSERIAL PRIMARY KEY,
    experiment_type TEXT NOT NULL,
    route TEXT,
    profitability_pct NUMERIC(18, 10),
    executable BOOLEAN NOT NULL DEFAULT FALSE,
    detected_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS simulated_trades (
    id BIGSERIAL PRIMARY KEY,
    experiment_type TEXT NOT NULL,
    market TEXT,
    side TEXT,
    capital_in NUMERIC(38, 18),
    capital_out NUMERIC(38, 18),
    fee_paid NUMERIC(38, 18),
    simulated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS experiments (
    id BIGSERIAL PRIMARY KEY,
    experiment_type TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS experiment_results (
    id BIGSERIAL PRIMARY KEY,
    experiment_id BIGINT NOT NULL REFERENCES experiments(id),
    metric_name TEXT NOT NULL,
    metric_value NUMERIC(38, 18),
    observed_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS system_events (
    id BIGSERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_market_snapshots_market_time ON market_snapshots (market, snapshot_at DESC);
CREATE INDEX IF NOT EXISTS idx_orderbook_snapshots_market_time ON orderbook_snapshots (market, snapshot_at DESC);
CREATE INDEX IF NOT EXISTS idx_opportunities_detected_at ON opportunities (detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_opportunities_route ON opportunities (route);
CREATE INDEX IF NOT EXISTS idx_experiment_results_experiment_time ON experiment_results (experiment_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_system_events_created_at ON system_events (created_at DESC);

