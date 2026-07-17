# Intent-Based Outbound 2.0 implementation notes

## Scope implemented

- Centralized target-role taxonomy with 118 active canonical roles.
- Campaign/function routing for Customer Support, Customer Success,
  Engineering/Data/IT, Finance, Marketing, Operations, People/HR, Sales/RevOps,
  Product, and E-commerce.
- Specialized buyer routing for Data, IT, Partnerships, and the other new
  functional families.
- Deterministic relevance handling for new titles, including specificity
  tie-breaking across duplicate search results.
- Explicit Senior, VP, Director, Intern, Event Marketing, and Field Marketing
  title exclusions.
- Explicit in-person/hybrid and non-paying posting exclusions.
- Safe role-level personalization fallbacks marked `manual_required` when the
  current JD does not contain enough role-specific evidence.
- Valid zero-result searches are tracked separately from API failures.

## Decisions reflected from Brett

- `B2B Technology` is the ABM segment; this role patch is the hiring-signal
  module and remains independent from the account-list build.
- `Graphic Designer` belongs to Marketing.
- `AI Training` and `AI Transformation` are removed.
- Roles that skew too heavily toward in-person work are removed.
- The founded-before-2010 requirement remains disabled.
- Discovery can expand broadly while enrichment remains bounded by the existing
  daily target and 60-company safety cap.

## Intentionally not activated by this patch

- No production deployment or Railway configuration changes.
- No Instantly campaigns are activated.
- No LinkedIn, RB2B, Meta, SMS, or ABM account-state automation is added.
- No campaign IDs are invented for Finance, Operations, People/HR, Product, or
  E-commerce. Setup validation warns until routing exists.

## Required checks before deployment

1. Run `pytest -q` and confirm all tests pass.
2. Check whether Railway/local `.env` has `ROLES_JSON`; it overrides the full
   catalog if present.
3. Confirm JSearch quota for the expanded daily query volume.
4. Confirm campaign routing or keep new-function leads in Airtable only.
5. Run a non-production scrape/filter sample and inspect relevance, in-person
   exclusions, role distribution, and runtime before merging to `main`.

- Limited JSearch diagnostics now separate API health from market yield and honor the env query cap when the CLI flag is omitted.


## Live quality hardening after the 2026-07-17 full-catalog validation

The 118-role live validation completed with 118/118 successful queries and no
API failures, but it exposed two production issues:

1. JSearch sometimes returned `job_is_remote=false` for titles that explicitly
   said Remote or Work From Home.
2. Three pages across the full catalog consumed too much monthly quota for a
   daily schedule.

The hardening resolves those issues by:

- using explicit title/location and precise requirement-language evidence before
  trusting the provider remote flag;
- rejecting hybrid, onsite, field-based, high-travel, and foreign-only roles when
  the requirement is explicit;
- adding observed staffing, marketplace, healthcare, nonprofit, media, and
  foreign-eligibility leakage controls;
- annotating every record with `_work_arrangement` and
  `_work_arrangement_reason` for reviewer traceability;
- adding `run_filter_replay.py` so saved results can be reprocessed offline;
- defaulting the daily catalog to one page per role;
- blocking estimated over-budget runs before network calls;
- aborting immediately on hard monthly/subscription quota exhaustion.

Offline replay of the 1,356 saved role-relevant postings changed the filter from
22 accepted / 1,334 rejected to 80 accepted / 1,276 rejected. It retained 13 of
the original accepted records, removed 9 clear leaks, and recovered 67 remote
postings whose provider flag was false. No external API or downstream write was
used for the replay.
