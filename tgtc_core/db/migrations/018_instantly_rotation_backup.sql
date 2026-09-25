-- Rotation of Instantly contacts is only safe if what it removes can be reconstructed.
--
-- The first rotation (2026-09-24, 5,450 contacts) backed up to a file on one laptop.
-- That is not a durable backup and it is not auditable by anyone else, so the record
-- lives here instead: the verbatim Instantly record, its checksum, why it was judged
-- removable, and what the deletion actually did.
--
-- A row is written BEFORE the delete is attempted. An interrupted rotation therefore
-- leaves evidence of everything it touched, and `deleted_at IS NULL` is the honest
-- state "we were about to remove this and do not know whether we did".

CREATE TABLE IF NOT EXISTS instantly_rotation_backup (
    lead_id          text PRIMARY KEY,
    campaign_id      text NOT NULL,
    campaign_name    text NOT NULL DEFAULT '',
    email            text NOT NULL DEFAULT '',
    lead_json        jsonb NOT NULL,
    payload_sha256   text NOT NULL,
    -- why this row was allowed to go, recorded at the moment of the decision
    reason           text NOT NULL,
    batch_id         text NOT NULL,
    backed_up_at     timestamptz NOT NULL DEFAULT now(),
    deleted_at       timestamptz,
    delete_status    integer,
    delete_error     text,
    restored_at      timestamptz
);

CREATE INDEX IF NOT EXISTS instantly_rotation_backup_batch_idx
    ON instantly_rotation_backup (batch_id, backed_up_at);
CREATE INDEX IF NOT EXISTS instantly_rotation_backup_email_idx
    ON instantly_rotation_backup (lower(email));
CREATE INDEX IF NOT EXISTS instantly_rotation_backup_pending_idx
    ON instantly_rotation_backup (backed_up_at) WHERE deleted_at IS NULL;
