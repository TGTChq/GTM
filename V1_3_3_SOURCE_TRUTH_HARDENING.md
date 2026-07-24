# TGTC READY v1.3.3 — Source-Truth Hardening

## Purpose

READY v1.3.3 closes the remaining preproduction defects revealed by the v1.3.2 shadow without changing the downstream Apollo, hiring-manager, email, Airtable, or Instantly contracts.

## Confirmed defects addressed

### Contradictory Ashby remote fields

Ashby can expose `isRemote=true` while the more specific `workplaceType` is `Hybrid` or `OnSite`. The adapter previously converted any true remote flag into `Remote`, allowing mandatory-office postings to pass.

The normalized record now:

- preserves `_provider_is_remote`;
- preserves `_provider_workplace_type`;
- stores the structured `work_arrangement`;
- treats explicit `Hybrid` and `OnSite` values as in-person requirements;
- retains fully remote records when `workplaceType=Remote`.

Generic `in-office requirement` language is also detected independently by the local filter and Job Gate fact extractor.

### Missing free company evidence

Himalayas job records expose a stable company slug but often omit the employer website and company size. v1.3.3 performs a bounded profile lookup only for role-relevant candidates that are already viable or whose sole blocker can be resolved by verified company evidence.

The profile must match the posting employer through the conservative organization-name matcher before any data is accepted. Verified fields include:

- employer website;
- employee range;
- profile URL;
- bounded profile text for narrow industry classification.

The default budget is 30 unique company profiles per run and is configurable with:

```text
HIMALAYAS_COMPANY_PROFILE_MAX_REQUESTS=30
```

### Free pre-Apollo rejection

Verified employee ranges are rejected only when the whole range is outside the configured ICP:

- maximum below `MIN_EMPLOYEES`; or
- minimum above `MAX_EMPLOYEES`.

Overlapping ranges such as 11–50 remain eligible because they can contain companies with 25 or more employees.

Verified profile text is used only with narrow employer-self-description patterns for excluded healthcare verticals. General mentions of healthcare clients are not sufficient.

### Additional systemic rules

- CamelCase employer names are segmented for industry matching, allowing `UnitedHealth` to match the existing `health` exclusion without adding a company blacklist.
- PEO, professional-employer-organization, co-employment, and worksite-employee service-delivery roles are rejected as outsourced/intermediary models.
- Verified profile evidence survives cross-source deduplication when a stronger official ATS posting becomes the primary record.

## Shadow observability

The shadow output now includes:

```text
himalayas_company_profiles:
  candidates_considered
  attempted
  succeeded
  verified
  websites
  employee_ranges
  jobs_enriched
```

The filter funnel also reports `excluded_firmographics` separately.

## Validation

- Python compilation: passed.
- Focused v1.3.3 tests: 11 passed.
- Full repository suite: 399 passed, 0 failed.
- Exact contradictory Ashby fixture: rejected as `excluded_in_person`.
- PEO fixture: rejected as `excluded_outsourcing`.
- CamelCase healthcare fixture: rejected as `excluded_industry`.
- No live calls to Apollo, Hunter, Airtable, Instantly, or JSearch were made during implementation or tests.

## Remaining proof required

This release must still complete one new Railway shadow before production. That shadow should confirm with current public data that:

- Replit's hybrid posting no longer reaches `contact_eligible`;
- SWBC, Sharecare, and UnitedHealth no longer reach `contact_eligible`;
- verified Himalayas profiles populate domains and employee ranges;
- all paid-call counters remain zero.

Only after reviewing that output should one controlled production execution be considered.
