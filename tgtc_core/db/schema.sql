-- TGTC core — PostgreSQL schema.  Applied idempotently by tgtc_core.db.migrate.
-- Every conceptual identity from the blueprint has its own table; a few are
-- combined where the combination is justified in the comment above the table.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     integer PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Acquisition
-- ---------------------------------------------------------------------------

-- One bounded, independent slice of one source. `lane` protects fresh capacity.
-- `complete_coverage` is the "continuous coverage" indicator; `next_offset` is the
-- "recent ingestion position". They are deliberately separate.
CREATE TABLE IF NOT EXISTS source_partitions (
    id                bigserial PRIMARY KEY,
    source            text NOT NULL,
    lane              text NOT NULL CHECK (lane IN ('fresh', 'backfill')),
    window_start      timestamptz NOT NULL,
    window_end        timestamptz NOT NULL,
    next_offset       integer NOT NULL DEFAULT 0,
    state             text NOT NULL DEFAULT 'open'
                      CHECK (state IN ('open', 'complete', 'failed', 'stalled')),
    complete_coverage boolean NOT NULL DEFAULT false,
    pages_received    integer NOT NULL DEFAULT 0,
    rows_received     integer NOT NULL DEFAULT 0,
    rows_new          integer NOT NULL DEFAULT 0,
    duplicate_pages   integer NOT NULL DEFAULT 0,
    last_error        text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, lane, window_start, window_end)
);

-- Intent is written BEFORE any potentially chargeable request; the outcome after.
-- `uncertain` is a timeout: it proves neither a charge nor its absence.
CREATE TABLE IF NOT EXISTS request_attempts (
    id                bigserial PRIMARY KEY,
    provider          text NOT NULL,
    operation         text NOT NULL,
    partition_id      bigint REFERENCES source_partitions(id),
    opportunity_id    bigint,
    person_ref        text,
    idempotency_key   text,
    params_json       jsonb NOT NULL DEFAULT '{}'::jsonb,
    status            text NOT NULL DEFAULT 'intended'
                      CHECK (status IN ('intended', 'served', 'failed', 'uncertain', 'refused')),
    http_status       integer,
    error_class       text,
    error_code        text,
    estimated_credits numeric,
    confirmed_credits numeric,
    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz,
    response_summary  jsonb
);
CREATE INDEX IF NOT EXISTS request_attempts_provider_idx
    ON request_attempts (provider, operation, started_at);

-- What one page actually returned. Written in the same transaction as the postings
-- it carried and before the partition cursor advances.
CREATE TABLE IF NOT EXISTS page_receipts (
    id                      bigserial PRIMARY KEY,
    partition_id            bigint NOT NULL REFERENCES source_partitions(id),
    attempt_id              bigint NOT NULL REFERENCES request_attempts(id),
    page_offset             integer NOT NULL,
    page_limit              integer NOT NULL,
    row_ids                 text[] NOT NULL,
    row_count               integer NOT NULL,
    fingerprint             text NOT NULL,
    duplicate_of_receipt_id bigint REFERENCES page_receipts(id),
    quota_jobs_remaining    integer,
    quota_requests_remaining integer,
    quota_next_billing_date text,
    rows_compressed         bytea,
    received_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS page_receipts_partition_idx ON page_receipts (partition_id, page_offset);

-- ---------------------------------------------------------------------------
-- Identity
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS employers (
    id              bigserial PRIMARY KEY,
    canonical_name  text NOT NULL,
    name_key        text NOT NULL,
    domain          text,
    linkedin_slug   text,
    apollo_org_id   text,
    employee_count  integer,
    industry        text,
    founded_year    integer,
    agency_flag     boolean,
    facts_json      jsonb NOT NULL DEFAULT '{}'::jsonb,
    enriched_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS employers_domain_uq ON employers (domain) WHERE domain IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS employers_slug_uq ON employers (linkedin_slug) WHERE linkedin_slug IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS employers_apollo_uq ON employers (apollo_org_id) WHERE apollo_org_id IS NOT NULL;

-- Every anchor that has ever pointed at an employer, with the evidence that
-- corroborated it. A name-only anchor never creates an alias by similarity.
CREATE TABLE IF NOT EXISTS employer_aliases (
    id           bigserial PRIMARY KEY,
    employer_id  bigint NOT NULL REFERENCES employers(id),
    alias_kind   text NOT NULL CHECK (alias_kind IN ('domain', 'linkedin_slug', 'name_key', 'apollo_org_id')),
    alias_value  text NOT NULL,
    evidence     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (alias_kind, alias_value)
);

-- The provider's record, once. Cross-source duplicates are linked, never merged.
-- `commercial_age_anchor` is assigned once and never reset by a re-observation.
CREATE TABLE IF NOT EXISTS postings (
    id                        bigserial PRIMARY KEY,
    source                    text NOT NULL,
    provider_job_id           text NOT NULL,
    content_hash              text NOT NULL,
    canonical_key             text,
    duplicate_of_posting_id   bigint REFERENCES postings(id),
    title                     text,
    employer_name             text,
    description_text          text,
    url                       text,
    employment_type           text,
    location_type             text,
    location_text             text,
    countries                 text[] NOT NULL DEFAULT '{}',
    provider_date_created     timestamptz,
    date_posted               timestamptz,
    date_modified             timestamptz,
    date_valid_through        timestamptz,
    first_seen_at             timestamptz NOT NULL DEFAULT now(),
    last_confirmed_active_at  timestamptz NOT NULL DEFAULT now(),
    commercial_age_anchor     timestamptz NOT NULL,
    org_json                  jsonb NOT NULL DEFAULT '{}'::jsonb,
    structured_json           jsonb NOT NULL DEFAULT '{}'::jsonb,
    employer_id               bigint REFERENCES employers(id),
    lane                      text NOT NULL DEFAULT 'fresh' CHECK (lane IN ('fresh', 'backfill')),
    state                     text NOT NULL DEFAULT 'new'
                              CHECK (state IN ('new', 'identity_resolved', 'classified', 'closed', 'expired')),
    close_reason              text,
    created_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, provider_job_id)
);
CREATE INDEX IF NOT EXISTS postings_canonical_idx ON postings (canonical_key);
CREATE INDEX IF NOT EXISTS postings_employer_idx ON postings (employer_id);
CREATE INDEX IF NOT EXISTS postings_state_idx ON postings (state, lane);

CREATE TABLE IF NOT EXISTS posting_versions (
    id            bigserial PRIMARY KEY,
    posting_id    bigint NOT NULL REFERENCES postings(id),
    version       integer NOT NULL,
    content_hash  text NOT NULL,
    changes       jsonb NOT NULL DEFAULT '{}'::jsonb,
    observed_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (posting_id, version)
);

CREATE TABLE IF NOT EXISTS opportunities (
    id                  bigserial PRIMARY KEY,
    employer_id         bigint NOT NULL REFERENCES employers(id),
    function_key        text NOT NULL,
    campaign_key        text NOT NULL,
    lane                text NOT NULL DEFAULT 'fresh' CHECK (lane IN ('fresh', 'backfill')),
    state               text NOT NULL DEFAULT 'open'
                        CHECK (state IN ('open', 'approved', 'closed')),
    close_reason        text,
    approved_person_id  bigint,
    first_posting_at    timestamptz,
    last_posting_at     timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (employer_id, function_key)
);

CREATE TABLE IF NOT EXISTS classifications (
    id                    bigserial PRIMARY KEY,
    posting_id            bigint NOT NULL REFERENCES postings(id),
    policy_version        text NOT NULL,
    model_version         text NOT NULL DEFAULT '',
    method                text NOT NULL CHECK (method IN ('deterministic', 'semantic', 'unavailable')),
    compatible_functions  text[] NOT NULL DEFAULT '{}',
    excluded              boolean NOT NULL DEFAULT false,
    exclusion_reason      text,
    result_json           jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (posting_id, policy_version, model_version)
);

CREATE TABLE IF NOT EXISTS opportunity_postings (
    opportunity_id     bigint NOT NULL REFERENCES opportunities(id),
    posting_id         bigint NOT NULL REFERENCES postings(id),
    classification_id  bigint REFERENCES classifications(id),
    linked_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (opportunity_id, posting_id)
);

-- Semantic answers keyed by content + policy + model so one description is never
-- sent twice and a copy of a posting costs nothing.
CREATE TABLE IF NOT EXISTS inference_cache (
    content_hash    text NOT NULL,
    policy_version  text NOT NULL,
    model_version   text NOT NULL,
    response_json   jsonb NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (content_hash, policy_version, model_version)
);

-- ---------------------------------------------------------------------------
-- People and attempts
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS people (
    id                   bigserial PRIMARY KEY,
    apollo_person_id     text,
    linkedin_url         text,
    first_name           text,
    last_name            text,
    title                text,
    employer_id          bigint REFERENCES employers(id),
    organization_name    text,
    organization_domain  text,
    email                text,
    email_status         text,
    email_authority      text,
    email_verified_at    timestamptz,
    facts_json           jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS people_apollo_uq ON people (apollo_person_id) WHERE apollo_person_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS people_linkedin_uq ON people (lower(linkedin_url)) WHERE linkedin_url IS NOT NULL;
CREATE INDEX IF NOT EXISTS people_email_idx ON people (lower(email));

-- One row per (opportunity, candidate, kind). A negative outcome is a negative
-- outcome; it is never stored as a successful search.
CREATE TABLE IF NOT EXISTS candidate_attempts (
    id                bigserial PRIMARY KEY,
    opportunity_id    bigint NOT NULL REFERENCES opportunities(id),
    person_id         bigint REFERENCES people(id),
    candidate_ref     text NOT NULL,
    attempt_kind      text NOT NULL CHECK (attempt_kind IN ('search', 'match', 'gate')),
    outcome           text NOT NULL,
    reason            text,
    attempt_id        bigint REFERENCES request_attempts(id),
    details           jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (opportunity_id, candidate_ref, attempt_kind)
);

CREATE TABLE IF NOT EXISTS evidence (
    id            bigserial PRIMARY KEY,
    subject_kind  text NOT NULL,
    subject_id    bigint NOT NULL,
    fact          text NOT NULL,
    value         jsonb,
    status        text NOT NULL,
    source        text NOT NULL,
    excerpt       text,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS evidence_subject_idx ON evidence (subject_kind, subject_id);

-- ---------------------------------------------------------------------------
-- Work, provider state, spend
-- ---------------------------------------------------------------------------

-- The queue. Claims are transactional (FOR UPDATE SKIP LOCKED), leases carry a token
-- and an expiry, and every transition is conditioned on (lease_token, version).
CREATE TABLE IF NOT EXISTS work_items (
    id                bigserial PRIMARY KEY,
    kind              text NOT NULL,
    subject_kind      text NOT NULL,
    subject_id        bigint NOT NULL,
    lane              text NOT NULL DEFAULT 'fresh' CHECK (lane IN ('fresh', 'backfill')),
    priority          integer NOT NULL DEFAULT 100,
    state             text NOT NULL DEFAULT 'ready'
                      CHECK (state IN ('ready', 'running', 'waiting', 'retry', 'closed', 'done')),
    available_at      timestamptz NOT NULL DEFAULT now(),
    lease_token       uuid,
    lease_expires_at  timestamptz,
    attempts          integer NOT NULL DEFAULT 0,
    max_attempts      integer NOT NULL DEFAULT 8,
    version           integer NOT NULL DEFAULT 0,
    waiting_on        text,
    last_error        text,
    close_reason      text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kind, subject_kind, subject_id)
);
CREATE INDEX IF NOT EXISTS work_items_claim_idx
    ON work_items (kind, lane, state, available_at, priority, id);

CREATE TABLE IF NOT EXISTS provider_state (
    provider              text PRIMARY KEY,
    state                 text NOT NULL DEFAULT 'unknown'
                          CHECK (state IN ('unknown', 'serving', 'refusing', 'unauthorized')),
    refusing_since        timestamptz,
    last_attempt_at       timestamptz,
    last_served_at        timestamptz,
    last_error_code       text,
    consecutive_refusals  integer NOT NULL DEFAULT 0,
    details               jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at            timestamptz NOT NULL DEFAULT now()
);

-- Requests, estimated credits and provider-confirmed credits are three columns.
CREATE TABLE IF NOT EXISTS credit_events (
    id                 bigserial PRIMARY KEY,
    provider           text NOT NULL,
    operation          text NOT NULL,
    attempt_id         bigint REFERENCES request_attempts(id),
    requests           integer NOT NULL DEFAULT 1,
    estimated_credits  numeric,
    confirmed_credits  numeric,
    basis              text NOT NULL CHECK (basis IN ('estimate', 'provider_header', 'provider_invoice')),
    created_at         timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Approval and delivery
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS approvals (
    id               bigserial PRIMARY KEY,
    opportunity_id   bigint NOT NULL REFERENCES opportunities(id),
    person_id        bigint NOT NULL REFERENCES people(id),
    employer_id      bigint NOT NULL REFERENCES employers(id),
    campaign_key     text NOT NULL,
    function_key     text NOT NULL,
    campaign_id      text NOT NULL,
    policy_version   text NOT NULL,
    lead_key         text NOT NULL,
    fingerprint      text NOT NULL,
    lead_json        jsonb NOT NULL,
    state            text NOT NULL DEFAULT 'approved' CHECK (state IN ('approved', 'delivered', 'revoked')),
    revoke_reason    text,
    approved_at      timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (opportunity_id),
    UNIQUE (lead_key)
);
-- One person, one active approval, across every function of the employer.
CREATE UNIQUE INDEX IF NOT EXISTS approvals_person_active_uq
    ON approvals (person_id) WHERE state <> 'revoked';

CREATE TABLE IF NOT EXISTS delivery_outbox (
    id                bigserial PRIMARY KEY,
    approval_id       bigint NOT NULL REFERENCES approvals(id),
    channel           text NOT NULL CHECK (channel IN ('airtable', 'instantly')),
    idempotency_key   text NOT NULL,
    payload_json      jsonb NOT NULL,
    state             text NOT NULL DEFAULT 'pending'
                      CHECK (state IN ('pending', 'in_flight', 'delivered', 'failed', 'blocked')),
    attempts          integer NOT NULL DEFAULT 0,
    lease_token       uuid,
    lease_expires_at  timestamptz,
    available_at      timestamptz NOT NULL DEFAULT now(),
    last_error        text,
    blocked_reason    text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (idempotency_key),
    UNIQUE (approval_id, channel)
);
CREATE INDEX IF NOT EXISTS delivery_outbox_claim_idx ON delivery_outbox (channel, state, available_at, id);

CREATE TABLE IF NOT EXISTS delivery_receipts (
    id                 bigserial PRIMARY KEY,
    outbox_id          bigint NOT NULL REFERENCES delivery_outbox(id),
    channel            text NOT NULL,
    receipt_kind       text NOT NULL CHECK (receipt_kind IN ('attempted', 'created', 'existing', 'reconciled', 'rejected')),
    external_id        text,
    external_campaign  text,
    response_summary   jsonb NOT NULL DEFAULT '{}'::jsonb,
    received_at        timestamptz NOT NULL DEFAULT now()
);
-- At most one terminal receipt per outbox item.
CREATE UNIQUE INDEX IF NOT EXISTS delivery_receipts_terminal_uq
    ON delivery_receipts (outbox_id) WHERE receipt_kind IN ('created', 'existing', 'reconciled');

CREATE TABLE IF NOT EXISTS suppressions (
    id          bigserial PRIMARY KEY,
    kind        text NOT NULL,
    key         text NOT NULL,
    source      text NOT NULL,
    reason      text NOT NULL,
    evidence    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kind, key)
);

CREATE TABLE IF NOT EXISTS outcome_events (
    id           bigserial PRIMARY KEY,
    provider     text NOT NULL,
    event_type   text NOT NULL,
    dedupe_key   text NOT NULL,
    email        text,
    campaign_id  text,
    external_id  text,
    occurred_at  timestamptz,
    payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
    applied      boolean NOT NULL DEFAULT false,
    applied_at   timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (dedupe_key)
);

CREATE TABLE IF NOT EXISTS run_log (
    id          bigserial PRIMARY KEY,
    run_id      text NOT NULL,
    stage       text NOT NULL,
    event       text NOT NULL,
    details     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS run_log_run_idx ON run_log (run_id, created_at);
