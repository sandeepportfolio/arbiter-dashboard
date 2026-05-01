-- 004_trade_analysis.sql
-- Persist a per-arb human-readable analysis (markdown) explaining why each
-- trade attempt succeeded, failed, partially filled, or unwound. Generated
-- deterministically from the orders, fills, opportunity, and incidents
-- (see arbiter/analysis/trade_analyzer.py); refreshable via backfill.

ALTER TABLE execution_arbs
    ADD COLUMN IF NOT EXISTS analysis_md       TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS analysis_version  INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS analysis_updated_at TIMESTAMPTZ;
