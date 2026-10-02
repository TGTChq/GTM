-- Country compliance gates, `tgtc-compliance/1` (2026-09-20). Jurisdiction is
-- STORED, never inferred: "A job's location does NOT determine the contact's
-- jurisdiction" (COMPLIANCE_MATRIX.md, authoritative, supplied by Luis).
--
-- Before this migration the core had no country column at all outside
-- `postings.countries` (the provider's derived list for the JOB). Every
-- jurisdiction question therefore had exactly one answer available -- the job's
-- -- which is the single answer the matrix forbids using for a person. Three
-- separate country columns on the approval make "stored, never inferred"
-- checkable rather than aspirational: no later reader can reconstruct one from
-- another, and `tgtc_core.policy.compliance.evaluate` binds job_country to the
-- two job gates and contact_country to the two person gates with no fallback.
--
-- Three levels, because three different things are observed at three different
-- times and must not overwrite each other:
--   * employers -- what was observed about the EMPLOYER (its country, its
--     declared legal form, and the corporate-subscriber verdict that form
--     yields). Written at identity resolution, from the provider's own org
--     block.
--   * people    -- what was observed about the PERSON (the country Apollo
--     reports for them, and any recorded opt-out). Written at enrichment.
--   * approvals -- the per-lead SNAPSHOT of all of it plus the decision:
--     which rule version decided, on what lawful basis and evidence, when the
--     privacy notice falls due, whether the lead is outreach-eligible and, when
--     it is not, the named reason. A lead is delivered from this row, so the
--     decision has to be readable from this row.
--
-- NULL for every existing row: additive, nullable, no default, no backfill --
-- a real later observation, never invented. NULL is exactly what the gates
-- treat as unknown, and unknown fails closed for sending while staying counted
-- for capacity. Inventing a value here would instead read downstream as a
-- decision that was never made.
--
-- Additive only, in the pattern of migrations 007-010: no DROP, no UPDATE, no
-- DELETE, no NOT NULL. Nothing in this file can destroy a record, which is the
-- storage half of "never delete it and never silently count it".
--
-- schema.sql carries the identical columns, since apply_schema() runs
-- schema.sql before any migration on every call and a fresh install never
-- replays this file.

-- The EMPLOYER's own jurisdiction and legal form. `company_country` is not the
-- job's country and not the contact's; `corporate_subscriber_status` is the
-- verdict `policy.compliance.classify_corporate_subscriber` returns for
-- `employer_legal_entity_type`, stored beside it so the input to the decision
-- and the decision are both auditable. NULL means never observed, which is
-- never corporate.
ALTER TABLE employers ADD COLUMN IF NOT EXISTS company_country text;
ALTER TABLE employers ADD COLUMN IF NOT EXISTS employer_legal_entity_type text;
ALTER TABLE employers ADD COLUMN IF NOT EXISTS corporate_subscriber_status text;

-- The PERSON's own jurisdiction, from the provider's person record -- the only
-- field that may decide a person gate. NULL means Apollo returned no country
-- for this person, which is unknown, which fails closed for sending.
ALTER TABLE people ADD COLUMN IF NOT EXISTS contact_country text;
ALTER TABLE people ADD COLUMN IF NOT EXISTS opt_out_status text;

-- The per-lead snapshot and verdict.
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS job_country text;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS company_country text;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS contact_country text;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS employer_legal_entity_type text;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS corporate_subscriber_status text;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS compliance_rule_version text;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS legal_basis text;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS legal_basis_evidence text;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS privacy_notice_due_at timestamptz;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS opt_out_status text;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS outreach_eligible boolean;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS outreach_block_reason text;
