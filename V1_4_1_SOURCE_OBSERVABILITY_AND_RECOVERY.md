# READY v1.4.1 — source observability and minimum recovery

## Purpose

READY v1.4.1 is a narrow hardening release based on the first live Railway shadow of v1.4. It does not remove the job, account, contact, or email gates. It corrects source accounting, removes a false Workable board, makes Himalayas company-profile failures observable and bounded, prevents legacy top-up limits from stopping multi-source recovery after two micro-batches, and fixes a specific posting-integrity overfilter affecting verified ATS records.

## Minimum-30 behavior

The production target remains 30 `FINAL_PASS` leads, not 30 raw postings or 30 shadow `contact_eligible` companies.

When the primary 0–14 day lane finishes below 30 `FINAL_PASS`, the pipeline proceeds in this order:

1. Reprocesses active inventory from 15–30 days using the same downstream gates.
2. If a deficit remains and JSearch is available, launches targeted JSearch micro-batches.
3. Continues beyond the former two-iteration legacy limit in multi-source mode.
4. Stops only when the minimum is reached or a real bound is hit: the JSearch request-unit budget, runtime, valid inventory exhaustion, the eligible-company safety cap when configured, or the downstream-yield circuit breaker.

`MULTI_SOURCE_FINAL_PASS_MAX_TOPUP_ITERATIONS=0` means there is no separate iteration cap. It does not mean unlimited API usage; the existing request and runtime budgets remain authoritative.

## Shadow findings explained

The first v1.4 shadow acquired 2,219 postings, kept 114 after the deterministic filter, and produced 66 pre-contact-eligible jobs across 58 companies. The modality relaxation was not the bottleneck: zero postings were excluded because they were onsite or hybrid. Most losses were posting-integrity failures, stale postings, and role mismatch.

v1.4.1 adds exact rejection diagnostics with per-source counts and bounded samples. It also stops requiring verified public ATS records to repeat the employer name inside the job description. Generic employers, malformed records, aggregators, identity conflicts, and unstructured JSearch syndication remain guarded.

## Corrections

- Reports actual JSearch requests, successful requests, estimated units, normalized jobs, errors, and skip reason in shadow output.
- Retains the compatibility `external_paid_calls` field but no longer hard-codes JSearch to zero.
- Rejects generic Workable product URLs such as `workable.com/jobs` as company boards.
- Prunes legacy invalid Workable registry entries at load time.
- Uses browser-compatible headers, canonical profile URLs, and additional title metadata when reading public Himalayas company pages.
- Adds Himalayas HTTP-status and failure-reason metrics.
- Stops Himalayas profile enrichment after repeated access failures instead of wasting the full request budget.
- Separates multi-source FINAL_PASS recovery limits from legacy JSearch-only settings.
- Adds shadow funnel diagnostics that distinguish raw, filtered, pre-contact, and FINAL_PASS stages.
- Allows verified direct ATS identities and structured first-party public-feed identities to pass the duplicate description-proof check while preserving all later source, account, contact, and email gates.
- Keeps unstructured JSearch and unknown syndicated employer identities behind the existing posting-integrity guard.

## Validation

- Python compilation: passed.
- Focused v1.4.1 tests: 14 passed.
- Complete offline suite: 425 passed.
- Patch clean-application replay: passed.
- No live JSearch, Apollo, Hunter, Airtable, Instantly, Railway, or job-source calls during validation.
