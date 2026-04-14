-- ARBITER — Database Schema

CREATE TABLE IF NOT EXISTS trades (
    id SERIAL PRIMARY KEY,
    arb_id VARCHAR(20) NOT NULL,
    canonical_id VARCHAR(50) NOT NULL,
    yes_platform VARCHAR(20) NOT NULL,
    yes_price DECIMAL(6,4) NOT NULL,
    yes_market_id VARCHAR(100),
    no_platform VARCHAR(20) NOT NULL,
    no_price DECIMAL(6,4) NOT NULL,
    no_market_id VARCHAR(100),
    quantity INT NOT NULL,
    gross_edge DECIMAL(6,4),
    total_fees DECIMAL(6,4),
    net_edge DECIMAL(6,4),
    realized_pnl DECIMAL(10,4),
    status VARCHAR(20) DEFAULT 'pending',
    is_simulation BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    id SERIAL PRIMARY KEY,
    platform VARCHAR(20) NOT NULL,
    canonical_id VARCHAR(50) NOT NULL,
    yes_price DECIMAL(6,4),
    no_price DECIMAL(6,4),
    volume DECIMAL(14,2),
    captured_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS balance_history (
    id SERIAL PRIMARY KEY,
    platform VARCHAR(20) NOT NULL,
    balance DECIMAL(12,2) NOT NULL,
    is_low BOOLEAN DEFAULT FALSE,
    recorded_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS alerts (
    id SERIAL PRIMARY KEY,
    alert_type VARCHAR(30) NOT NULL,
    platform VARCHAR(20),
    canonical_id VARCHAR(50),
    message TEXT,
    sent_via VARCHAR(20) DEFAULT 'telegram',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for fast queries
CREATE INDEX idx_trades_canonical ON trades(canonical_id);
CREATE INDEX idx_trades_created ON trades(created_at);
CREATE INDEX idx_prices_platform_market ON price_snapshots(platform, canonical_id);
CREATE INDEX idx_prices_captured ON price_snapshots(captured_at);
CREATE INDEX idx_balance_platform ON balance_history(platform);
