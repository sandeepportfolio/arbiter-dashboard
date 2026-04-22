-- arbiter/sql/migrations/002_widen_canonical_id.sql
-- Widens canonical_id from VARCHAR(60) to VARCHAR(200) across all tables.
-- Safe to run on existing databases; idempotent (ALTER TYPE on a wider type is always safe in Postgres).

ALTER TABLE market_mappings     ALTER COLUMN canonical_id TYPE VARCHAR(200);
ALTER TABLE mapping_candidates  ALTER COLUMN canonical_id TYPE VARCHAR(200);
ALTER TABLE execution_arbs      ALTER COLUMN canonical_id TYPE VARCHAR(200);
ALTER TABLE execution_orders    ALTER COLUMN canonical_id TYPE VARCHAR(200);
ALTER TABLE execution_incidents ALTER COLUMN canonical_id TYPE VARCHAR(200);
