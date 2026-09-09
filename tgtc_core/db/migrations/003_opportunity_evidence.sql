-- Persist the evidence used to open an opportunity epoch. Reprocessing an unchanged
-- classification must not grant another epoch or consume the lifetime epoch limit.
ALTER TABLE opportunity_postings ADD COLUMN IF NOT EXISTS evidence_hash text;

-- Old classifications have no verifiable input hash. Revalidate existing active
-- inventory locally; do not acquire it again and do not count it as new inventory.
WITH stale AS (
    UPDATE postings p SET state = 'identity_resolved', updated_at = now()
    WHERE p.state = 'classified' AND EXISTS (
        SELECT 1 FROM opportunity_postings op JOIN classifications c ON c.id = op.classification_id
        WHERE op.posting_id = p.id AND c.result_json->>'input_content_hash' IS NULL)
    RETURNING p.id, p.lane
)
INSERT INTO work_items (kind, subject_kind, subject_id, lane, available_at)
SELECT 'classify', 'posting', id, lane, now() FROM stale
ON CONFLICT (kind, subject_kind, subject_id) DO UPDATE SET
    state = 'ready', available_at = now(), version = work_items.version + 1,
    lease_token = NULL, lease_expires_at = NULL, close_reason = NULL;
