# READY v1.4.3 — Final verified production hardening

This cumulative release supersedes the unshipped v1.4.2 package and applies directly on top of merged v1.4.1. It closes every defect found by the final synthetic stress audit without loosening ICP, geography, modality, staffing, outsourcing, industry, firmographic, contact, email, CRM, or FINAL_PASS policy.

## Corrections

1. Reject non-identity employer placeholders such as `name`, `company name`, `unknown`, and `confidential employer`.
2. Prevent placeholders from seeding, verifying, or remaining in the ATS registry.
3. Reject only the false Workday `/job/...` route as a site identifier while preserving legitimate customer-defined sites such as `External`, `Careers`, `Jobs`, `Recruiting`, `Default`, and `EXTERNAL_CAREERS`.
4. Force shadow filtering to use the declared 0–14 day primary window rather than a stale legacy environment value.
5. Preserve configured short role aliases `DBA`, `BDR`, and `SDR` instead of treating them as malformed titles.
6. Accept punctuation-equivalent catalog titles such as `AI-Engineer`, `GTM / Engineer`, and `Customer-Support` while retaining negative-context overrides.
7. Treat `Permanent` and explicit permanent/full-time variants as valid full-time employment; defer ambiguous labels such as `Regular`, `Employee`, and `Salaried` instead of prematurely rejecting them.
8. Align the prefilter with the downstream Role Gate for clearly physical titles such as `Warehouse Operations Analyst`.

## Validation boundary

- 442 repository tests pass offline.
- 300,972 deterministic and randomized synthetic cases were evaluated.
- All 118 roles passed role, modality, source-family, freshness, recovery, routing, Job Gate, and Role Gate matrices.
- A controlled end-to-end corpus retained every expected valid posting and rejected every expected invalid posting.
- JSearch, Apollo, Hunter, Airtable, and Instantly were not called during validation.

The next step is one controlled production execution with Airtable review enabled and Instantly enrollment disabled until manual approval.
