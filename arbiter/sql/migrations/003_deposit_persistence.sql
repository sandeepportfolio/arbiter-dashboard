-- 003_deposit_persistence.sql
-- Persist starting balances and deposit events across container restarts.

CREATE TABLE IF NOT EXISTS platform_balances (
    platform        TEXT PRIMARY KEY,
    starting_balance DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    total_deposits  DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS deposit_events (
    id              BIGSERIAL PRIMARY KEY,
    platform        TEXT NOT NULL,
    amount          DOUBLE PRECISION NOT NULL,
    balance_before  DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    balance_after   DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_deposit_events_platform ON deposit_events(platform);
CREATE INDEX IF NOT EXISTS idx_deposit_events_created ON deposit_events(created_at DESC);
