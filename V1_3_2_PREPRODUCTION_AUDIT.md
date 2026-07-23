# TGTC READY v1.3.2 — Cumulative Preproduction Audit

## Scope

This cumulative hotfix is built on READY v1.3 and supersedes the earlier v1.3.1 package. It addresses defects found after the first live free-source shadow run. It does not perform production writes or paid enrichment during installation.

## What the first shadow proved

- Public acquisition worked: 786 global records plus 407 direct ATS jobs produced 1,085 selected postings.
- Automatic ATS discovery worked: nine boards were present and two were learned from current feeds.
- Cross-source deduplication removed 35 overlapping postings.
- The run made zero JSearch, Apollo, Hunter, Airtable, or Instantly calls.

The 44 Step-2 survivors were postings, not final leads. The original shadow did not run the Job Gate, Role Gate, Account Gate, contact gate, or email gate.

## Root causes found

1. Exact global locations such as `Anywhere in the World` could pass when the description mentioned the United States.
2. Direct ATS records could be rejected by employer-description conflicts because the quality layer did not distinguish independently verified board identity.
3. Greenhouse `updated_at` had been treated as posting freshness; it can represent an edit rather than first publication.
4. Ashby remote evidence was not fully preserved.
5. Company-domain propagation ran before direct ATS jobs were added, so official ATS identity could not improve feed records in the same acquisition run.
6. Substring identity checks could create collisions such as `Meta` versus `metabase`.
7. Weak feed evidence could overwrite stronger ATS registry identity.
8. The shadow did not execute the zero-credit Job and Role Gates and therefore overstated proximity to FINAL_PASS.
9. Greenhouse detail requests needed per-board and per-run bounds.
10. Pre-contact `NEEDS_CHECK` reporting and evidence-source labels were inconsistent.

## Corrections

- Exact global-remote locations fail US eligibility before description-based evidence is considered.
- ATS identity trust requires conservative company/board compatibility; substring containment is not accepted.
- Registry identities carry confidence levels. Weak incompatible feed evidence is recorded as a conflict but cannot overwrite a stronger identity.
- Official direct ATS records are fed back into the registry after acquisition.
- Company domains are propagated again after ATS acquisition and only when exactly one safe domain exists for a normalized company identity.
- Greenhouse retrieves `first_published` only for role-relevant jobs, bounded by 25 detail requests per board and 100 per run by default.
- Direct Greenhouse records without verified `first_published` are rejected before paid enrichment rather than treated as fresh.
- Ashby `isRemote` and secondary location evidence are preserved.
- Shadow mode now executes Step 2 plus the Job and Role Gates using public source checks, while still making zero paid/downstream calls.
- Shadow forced ATS refresh is capped at 25 boards by default.
- Shadow reports separate posting counts, unique-company counts, contact-eligible counts, and explicit stage semantics.
- Provider evidence labels are source-aware instead of JSearch-specific.
- `NEEDS_CHECK` counts now derive from actual Job/Role Gate states.

## Filters preserved

The production funnel continues to enforce:

- posting integrity and active-source evidence;
- target-role relevance and permitted seniority;
- remote work with explicit US eligibility;
- full-time, active, non-contract intent;
- no mandatory clearance, professional license, physical facility, or disqualifying travel/onsite requirement;
- staffing, outsourcing, aggregator, restricted-industry, and non-paying exclusions;
- CRM, duplicate, and previously-seen exclusions;
- account identity, 25–1,000 employee range, industry and business-model checks through Apollo;
- hiring-manager relevance, person identity, current employment, email validity, FINAL_PASS persistence, Airtable review, and live revalidation before Instantly.

## Validation

```text
Python compilation: passed
Focused v1.3.2 audit tests: 10 passed
Complete repository suite: 388 passed, 0 failed
```

The complete suite ran with `PRODUCTION=0` and a temporary test-only validation signing key. Logged API errors are mocked failure-path assertions; installation makes no live calls.

## What is proven

- The identified code and observability defects are corrected.
- Existing regression behavior remains intact across 388 tests.
- The cumulative patch can be validated without touching live services.

## What is not yet proven

- The number of contact-eligible postings produced by a fresh live shadow after these corrections.
- The number of companies that pass Apollo firmographic/account validation.
- The number of valid hiring managers and emails.
- The 30-net-Airtable-row daily target.

Those outcomes require a new shadow run followed, only if clean, by one controlled production run.

## Required variables

```text
VALIDATION_VERSION=tgtc-ready-v1.3.2-preproduction-audit
ATS_GREENHOUSE_DETAIL_MAX_REQUESTS_PER_BOARD=25
ATS_GREENHOUSE_DETAIL_MAX_REQUESTS_PER_RUN=100
ATS_SHADOW_FORCE_REFRESH_MAX_BOARDS=25
```

All existing READY v1.3 free-source variables remain unchanged. No new API key is required.
