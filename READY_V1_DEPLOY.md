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
   - `VALIDATION_VERSION=tgtc-ready-v1.0`
   - `FINAL_PASS_MICROBATCH_QUERY_UNITS=6`
   - `FINAL_PASS_MAX_TOPUP_ITERATIONS=2`
   - `FINAL_PASS_MAX_RUNTIME_SECONDS=1800`
   - `REQUIRE_US_CONTACT_TERRITORY=0`
4. Preserve the existing production `VALIDATION_SIGNING_KEY`; do not replace it with the example value.
5. Run `python validate_setup.py --live` before any pipeline run.
6. Run one controlled `python -u run_daily.py` audit. Do not run `run_approved.py` until the Airtable output has been reviewed.

## Rollback

The patch does not delete legacy state labels. Revert the patch and restore the previous Railway variables to return to v0.5.1. Raw jobs and operational artifacts remain intact.
