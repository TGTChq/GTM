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
