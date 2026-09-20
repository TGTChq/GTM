-- Fix round 1, C1 (CRITICAL, independent review, 2026-09-20): a firmographic
-- conflict/unknown that services/opportunity.py's size gate correctly lets
-- proceed (Decision 2, 2026-09-19) was entering the approved set with no
-- marker distinguishing it from a confirmed 25-1,000 match -- ledger()'s
-- approved_distinct/approved_by_campaign counted both identically, and the
-- delivery payloads shipped a confident Employees/Size Band/company_size
-- label built from the very field in dispute.
--
-- NULL for every existing row: a real, later re-resolution, never invented.
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS company_size_state text;
