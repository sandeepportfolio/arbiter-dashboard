-- arbiter/sql/migrations/002_widen_canonical_id.sql
-- Widens canonical_id from VARCHAR(60) to VARCHAR(200) across all tables.
-- Safe to run on existing databases; idempotent (ALTER TYPE on a wider type is always safe in Postgres).
-- Uses DO blocks to skip tables that don't exist yet (market_mappings / mapping_candidates
-- are created by MarketMappingStore.init_schema which may run after this migration).

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'market_mappings') THEN
    ALTER TABLE market_mappings ALTER COLUMN canonical_id TYPE VARCHAR(200);
  END IF;
END $$;

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'mapping_candidates') THEN
    ALTER TABLE mapping_candidates ALTER COLUMN canonical_id TYPE VARCHAR(200);
  END IF;
END $$;

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'execution_arbs') THEN
    ALTER TABLE execution_arbs ALTER COLUMN canonical_id TYPE VARCHAR(200);
  END IF;
END $$;

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'execution_orders') THEN
    ALTER TABLE execution_orders ALTER COLUMN canonical_id TYPE VARCHAR(200);
  END IF;
END $$;

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'execution_incidents') THEN
    ALTER TABLE execution_incidents ALTER COLUMN canonical_id TYPE VARCHAR(200);
  END IF;
END $$;
