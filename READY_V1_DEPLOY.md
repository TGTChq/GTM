# TGTC READY v1 deployment

## Contract

- Acquisition: rolling seven-day window, one page across 50 strategically distinct search roles.
- Classification: all 118 supported roles are evaluated locally.
- Airtable boundary: only signed `FINAL_PASS` records, exposed operationally as `READY`.
- Instantly remains behind `run_approved.py` and revalidation.
- `RETRY` stays internal; deterministic failures become `REJECTED`.

## Railway migration

Keep the service Start Command as `sh -c "sleep infinity"` until the controlled live audit is complete.

1. Deploy the patched code.
2. Remove `ROLES_JSON` if it contains the historical 118-role list.
3. Replace old runtime values with the values in `.env.example`, especially:
   - `DATE_POSTED=week`
   - `NUM_PAGES=1`
   - `MAX_JOB_AGE_DAYS=8`
   - `VALIDATION_VERSION=tgtc-ready-v1.1-source-resilience`
   - `FINAL_PASS_MICROBATCH_QUERY_UNITS=6`
   - `FINAL_PASS_MAX_TOPUP_ITERATIONS=2`
   - `FINAL_PASS_MAX_RUNTIME_SECONDS=1800`
   - `REQUIRE_US_CONTACT_TERRITORY=0`
4. Preserve the existing production `VALIDATION_SIGNING_KEY`; do not replace it with the example value.
5. Run `python validate_setup.py --live` before any pipeline run.
6. Run one controlled `python -u run_daily.py` audit. Do not run `run_approved.py` until the Airtable output has been reviewed.

## Rollback

The patch does not delete legacy state labels. Revert the patch and restore the previous Railway variables to return to v0.5.1. Raw jobs and operational artifacts remain intact.


## Source-resilient normal operation (READY v1.1)

The Job Gate resolves supplied company/ATS posting URLs before generic careers-page
discovery. Generic discovery is bounded by a per-job time budget. A 401/403, timeout,
or bot block cannot qualify a job by itself and cannot poison unrelated discovery
paths. A fresh direct company/ATS posting may use the structured fallback only when
identity, recency, substantial description, full-time, remote, and US-market facts
all agree and no authoritative inactive or contradictory evidence exists. Approved
Instantly enrollment still performs volatile source revalidation.

Recommended Railway values:

```env
JOB_SOURCE_DIRECT_FIRST_ENABLED=1
JOB_SOURCE_DISCOVERY_MAX_PAGES=4
JOB_SOURCE_DISCOVERY_MAX_BOARD_PAGES=2
JOB_SOURCE_DISCOVERY_BUDGET_SECONDS=18
JOB_SOURCE_DISCOVERY_TIMEOUT_SECONDS=5
JOB_SOURCE_ATTEMPTS_PER_URL=1
JOB_SOURCE_TIMEOUT_SECONDS=8
JOB_SOURCE_FRESH_DIRECT_FALLBACK_ENABLED=1
JOB_SOURCE_FRESH_DIRECT_MAX_AGE_DAYS=8
JOB_SOURCE_FRESH_DIRECT_MIN_DESCRIPTION_CHARS=700
PIPELINE_FAIL_PROCESS_ON_SLA_MISS=0
```

A technically successful run exits `0` even when the commercial 30-lead SLA is
missed, preventing Railway restart loops. The miss remains explicit in logs and the
run summary.


## READY v1.2: employer identity and provider review

- Publisher/apply domains are never treated as employer domains unless JSearch
  explicitly marks the URL direct. Name-only Apollo resolution must return a
  compatible organization before its domain is used.
- The complete configured role catalog may use bounded adaptive page-2 and
  lookback acquisition. `JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN=0` means no global
  cap; the adaptive 32-query and lookback 16-query caps still bound the run.
- Fresh, substantial provider records that already passed Step 2 may enter
  Account/Contact/Email qualification as `ACTIVE_PROVIDER_STRUCTURED`. Airtable
  displays `unverified_review`, and approval still requires trusted live-source
  revalidation before Instantly.
- Authoritative ATS absence, inactive postings, identity contradictions, stale
  records, thin descriptions, contracts, non-US scope and onsite/hybrid roles
  remain blocked.

Recommended Railway additions:

```env
VALIDATION_VERSION=tgtc-ready-v1.2-identity-and-recall
JOB_SOURCE_PROVIDER_STRUCTURED_REVIEW_ENABLED=1
JOB_SOURCE_PROVIDER_STRUCTURED_MAX_AGE_DAYS=8
JOB_SOURCE_PROVIDER_STRUCTURED_MIN_DESCRIPTION_CHARS=700
```
