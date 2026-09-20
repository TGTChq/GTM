# Task: country compliance gates (`tgtc-compliance/1`)

Branch `audit/phase2-offline-fixes`, base `d47c16e`. Role: IMPLEMENTER.

Specification: `C:\TGTC\tgtc_canary_evidence\funnel_audit_20260919\COMPLIANCE_MATRIX.md`
(authoritative, researched, supplied by Luis 2026-09-20).

## What is being built

**A.** Four INDEPENDENT country gates — `job_acquisition_allowed`,
`aggregate_capacity_modelling_allowed`, `person_enrichment_allowed`,
`cold_email_allowed` — as data plus a small pure API in `tgtc_core/policy/`.
A single generic geography flag is forbidden.

**B.** Jurisdiction STORED, never inferred. A job's location does not determine
the contact's. Twelve distinct persisted fields, additive migration, no backfill.

**C.** The UK deterministic gate: every condition must hold; unknown entity type
is NEVER corporate.

**D.** Fail closed for sending, open for capacity. Never delete, never silently
count.

**E.** Counting: every category reported separately, never summed. DE/AE/SA can
never reach a "ready to send" total.

## Hard constraints

TDD (a failing test precedes every production change); a deterministic test per
country gate; no deploy/push/outreach/Airtable/Instantly writes/Apollo spend/any
provider call; frozen labels and holdout untouched; `python
rebuild/run_offline_tests.py tests_core -q`.

Two reviewer rules this branch enforces: **share a predicate, never copy it**,
and **state plainly whether a change is reachable by the live pipeline or only
by tests**.

## Deliverables

- code + migration, committed, one commit per coherent piece;
- an isolated dry-run report over `phase3_paid/records/` (in-process, never
  production);
- `git diff d47c16e..HEAD` written to this directory.
