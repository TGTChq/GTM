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
CREATE UNIQUE INDEX IF NOT EXISTS evidence_employer_company_size_uq
    ON evidence (subject_kind, subject_id, fact)
    WHERE subject_kind = 'employer' AND fact IN ('company:firmographic_conflict', 'company:unknown_firmographics');
