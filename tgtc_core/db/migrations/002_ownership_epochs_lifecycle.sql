-- Migration 2 (review a090a2b, findings R06 / R07 / R11 and the reopen-epoch rule).
-- Idempotent: safe on a fresh v2 schema and on a database created from schema v1.

-- R07: exclusive acquisition ownership of a partition; fenced cursor commits.
ALTER TABLE source_partitions ADD COLUMN IF NOT EXISTS lease_token uuid;
ALTER TABLE source_partitions ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz;
ALTER TABLE source_partitions ADD COLUMN IF NOT EXISTS stall_reason text;
ALTER TABLE page_receipts ADD COLUMN IF NOT EXISTS fenced boolean NOT NULL DEFAULT false;

-- R06: an outbox claim is a state transition, not only a token.
ALTER TABLE delivery_outbox DROP CONSTRAINT IF EXISTS delivery_outbox_state_check;
ALTER TABLE delivery_outbox ADD CONSTRAINT delivery_outbox_state_check
    CHECK (state IN ('pending', 'claimed', 'in_flight', 'delivered', 'failed', 'blocked'));

-- Reopen-on-new-evidence: attempts are budgeted per evidence epoch, history is kept.
ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS evidence_epoch integer NOT NULL DEFAULT 1;
ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS reopened_at timestamptz;
ALTER TABLE candidate_attempts ADD COLUMN IF NOT EXISTS epoch integer NOT NULL DEFAULT 1;

-- R11: posting lifecycle is explicit.
ALTER TABLE postings ADD COLUMN IF NOT EXISTS expired_at timestamptz;

-- R08: one atomic probe reservation per interval across workers.
ALTER TABLE provider_state ADD COLUMN IF NOT EXISTS probe_reserved_at timestamptz;
