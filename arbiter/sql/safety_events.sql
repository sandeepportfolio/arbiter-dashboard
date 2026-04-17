-- SAFE-01: append-only audit trail for SafetySupervisor kill-switch events.
-- Every arm/reset writes exactly one row — no UPDATE/DELETE paths exist on
-- SafetyEventStore (threat-model T-3-01-D, plan 03-01).

CREATE TABLE IF NOT EXISTS safety_events (
    event_id VARCHAR(20) PRIMARY KEY,
    event_type VARCHAR(30) NOT NULL,
    actor VARCHAR(200) NOT NULL,
    reason TEXT NOT NULL,
    state_json JSONB NOT NULL,
    cancelled_counts_json JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_safety_events_created_at
    ON safety_events (created_at DESC);
