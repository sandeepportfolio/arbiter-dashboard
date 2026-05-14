-- H20: enforce that client_order_id is globally unique across execution_orders.
--
-- Why this matters: client_order_id is the venue-supplied idempotency key. A
-- platform that receives the same client_order_id twice MAY reject the second
-- request OR (worse) treat them as separate orders depending on the venue.
-- The existing index (idx_execution_orders_client_id, in 001_*) is NOT unique
-- and only optimised lookup — nothing in the schema prevented two rows from
-- being inserted with the same client_order_id from a race or replay bug.
--
-- We create a partial unique index (WHERE client_order_id IS NOT NULL) so
-- pre-existing rows with NULL client_order_id remain valid. New writes that
-- collide will fail loudly at INSERT/UPDATE time with a unique-violation
-- error rather than silently producing a ghost order.
--
-- Idempotent: safe to run on a fresh DB or after a previous successful run.
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_client_order_id
    ON execution_orders(client_order_id)
    WHERE client_order_id IS NOT NULL;
