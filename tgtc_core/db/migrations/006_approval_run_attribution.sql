-- Attribute every new approval to the production run that created it.  Existing
-- rows remain explicitly legacy/unattributed and can never inflate a new target.
ALTER TABLE approvals
    ADD COLUMN IF NOT EXISTS run_id text NOT NULL DEFAULT 'legacy-unattributed';

CREATE INDEX IF NOT EXISTS approvals_run_idx ON approvals (run_id, approved_at);

-- One opportunity may yield up to the configured contact quota.  Person-level
-- uniqueness and lead_key uniqueness continue to prevent duplicate leads.
ALTER TABLE approvals DROP CONSTRAINT IF EXISTS approvals_opportunity_id_key;
CREATE UNIQUE INDEX IF NOT EXISTS approvals_opportunity_person_uq
    ON approvals (opportunity_id, person_id);

CREATE TABLE IF NOT EXISTS company_function_contacts (
    company_function_key text NOT NULL,
    contact_key          text NOT NULL,
    source               text NOT NULL,
    evidence             jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (company_function_key, contact_key)
);
CREATE INDEX IF NOT EXISTS company_function_contacts_contact_idx
    ON company_function_contacts (contact_key);

-- Preserve pre-migration Airtable history.  The same Lead Key/record id is used
-- across domain/name/linkedin aliases, so union counts remain contact counts.
INSERT INTO company_function_contacts (company_function_key, contact_key, source, evidence)
SELECT key,
       COALESCE('lead:' || NULLIF(lower(evidence->>'lead_key'), ''),
                'record:' || NULLIF(lower(evidence->>'record_id'), ''),
                'legacy-suppression:' || id::text),
       source,
       evidence
FROM suppressions
WHERE kind = 'company_function'
ON CONFLICT (company_function_key, contact_key) DO NOTHING;

-- The new search page size and verified-email filter materially change the
-- evidence available to buyer-search waits.  Requeue those waits exactly once,
-- when migration 006 is first applied; later schema replays must preserve backoff.
DO $migration$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE version = 6) THEN
        UPDATE work_items
        SET state = 'ready', available_at = now(), waiting_on = NULL,
            lease_token = NULL, lease_expires_at = NULL, updated_at = now()
        WHERE kind = 'qualify_opportunity'
          AND state = 'waiting'
          AND waiting_on LIKE 'buyer_search_pending:%';
    END IF;
END
$migration$;
