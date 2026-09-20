-- Fix round 1, I3 (IMPORTANT, independent review, 2026-09-20): services/
-- opportunity.py's `_size_gate` records a firmographic_conflict/
-- unknown_firmographics as an `evidence` row, but `evidence` carries no
-- unique constraint -- one conflicting employer accumulates a new row at
-- BOTH call sites (pre- and post-enrichment) on every pass, and again on
-- every retry/reschedule, so a bucket-size query over-counts distinct
-- employers.
--
-- Scoped to exactly the two company-size fact values this task's evidence
-- rows use: every OTHER `evidence` fact (apollo_domain_disagreement,
-- domain_disagreement, ...) keeps its existing one-row-per-observed-event
-- semantics untouched.
--
-- Fix round 2, I3 (IMPORTANT, independent review): a plain, unconstrained
-- INSERT shipped in the previous commit (db79e4f) -- a database where that
-- code already ran holds duplicate (subject_kind, subject_id, fact) rows for
-- these two facts, and CREATE UNIQUE INDEX hard-fails on them, wedging every
-- future apply_schema() call until an operator deletes rows by hand.
-- Deduped first (keep the newest row per group; this codebase's own
-- precedent, migration 006, stages a write before its own constraint the
-- same way). schema.sql carries the identical statement pair, since
-- apply_schema() runs schema.sql before any migration on every call.
DELETE FROM evidence dup
USING evidence newer
WHERE dup.subject_kind = 'employer' AND dup.fact IN ('company:firmographic_conflict', 'company:unknown_firmographics')
  AND newer.subject_kind = dup.subject_kind AND newer.subject_id = dup.subject_id AND newer.fact = dup.fact
  AND newer.id > dup.id;
CREATE UNIQUE INDEX IF NOT EXISTS evidence_employer_company_size_uq
    ON evidence (subject_kind, subject_id, fact)
    WHERE subject_kind = 'employer' AND fact IN ('company:firmographic_conflict', 'company:unknown_firmographics');
