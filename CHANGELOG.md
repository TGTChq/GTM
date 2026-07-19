# Definitive lead-quality and controlled-volume hardening — July 19, 2026

- Added a zero-credit quality layer that rejects entire failure families before Apollo/Hunter: multi-job roundup pages, malformed or expired postings, non-standard work programs, clearance/federal-delivery roles, outsourcing/BPO intermediaries, and contextual role collisions.
- Fixed semantic collisions including PR Account Executive vs. sales Account Executive, inventory/catalog work vs. Product Support, and static graphic design mislabeled as Video Editor.
- Fixed geography parsing so `REMOTE OK` and `PR Account Executive` cannot become Oklahoma or Puerto Rico, while explicit `City, ST`, `United States`, `Remote US`, and delimiter-bounded `- US` evidence remain eligible.
- Added safe employer normalization for ATS wrappers without allowing job boards or ATS domains to become the employer.
- Expanded staffing, outsourcing, event, nonprofit, and hidden freelance/project-based controls using corroborated business-model evidence rather than one-off title blocks.
- Added target-aware adaptive lookback: the daily catalog remains at `NUM_PAGES=1`, reserves up to 16 of the existing 150 request units, and uses a one-week diversified query only when the first pass produces fewer than 60 pre-enrichment candidates.
- Added 28 focused regression tests; complete suite now passes 159 tests.
- Offline replay of 1,356 saved postings used no external APIs and reduced the current-code candidate set from 55 to 48 while recovering safe US-scope false negatives such as titles ending in `- US`.

# Intent-Based Outbound 2.0 — July Week 3

- Added `role_catalog.py` as the single source of truth for 118 canonical target roles, function buckets, hiring-manager routing, aliases, and safe focus fallbacks.
- Removed broad AI concepts (`AI Training`, `AI Transformation`) and roles that skew heavily in-person.
- Kept `Graphic Designer` only under Marketing.
- Added Finance, Operations, People/HR, Product, E-commerce, Data, IT, and Partnerships buyer hierarchies.
- Separated campaign/function grouping from specialized hiring-manager routing.
- Added explicit in-person and non-paying job filters plus audit leak checks.
- Added global Senior, Event Marketing, and Field Marketing title exclusions.
- Changed successful zero-result searches from failures into a separate market-observation metric.
- Added specificity tie-breaking so a role such as `Tax Accountant` wins over the broader `Accountant` query.
- Added campaign-routing configuration for the new functional buckets and setup warnings when a bucket lacks a campaign.
- Added 11 new tests plus catalog-wide routing/fallback subtests; total suite: 73 passing tests.

# Corrections made

## Pipeline integrity

- `run_daily.py` now executes Steps 1-4, not only scrape/filter/audit.
- Instantly enrollment is separated into `run_approved.py`, preserving the human approval boundary.
- Cross-day seen-state is committed only after Airtable succeeds, preventing downstream failures from permanently suppressing valid jobs.
- State writes are atomic and corrupted state files are preserved instead of crashing the pipeline.

## Job quality

- A duplicate job returned by several search queries is evaluated against every role and assigned to the strongest role, instead of whichever query returned it first.
- Added conservative role relevance rules and explicit `accept/review/reject` metadata.
- Added clear mismatch rules for industrial/QA automation, IT support, non-graphic design, and other common query leakage.
- Fixed coordinate-only US classification; coordinates are no longer treated as proof of country.
- Added full US state-name support.
- Removed `en-US` locale as an independent proof that a global Workday job is US-based.

## Staffing filtering

- Fixed false positives from phrases such as “no staffing agencies” and “we do not accept agency submissions.”
- Description-based exclusion now relies on first-person/intermediary language or vague employer names.
- Added Apollo industry as a second safety layer for staffing/recruiting and other excluded industries.
- Replaced the circular “95% recall” audit with optional manually labeled ground truth that calculates accuracy, precision, recall, and F1.

## Apollo/Hunter

- People Search now uses Apollo's documented `q_organization_domains_list[]` and `person_titles[]` query parameters.
- Uses strict title matching and validates returned organization domains.
- Organization enrichment uses domain, name, and website together when available.
- Apollo person identity is preserved even when no email is returned, so Hunter fallback now actually has first name, last name, and domain.
- API/network failures raise and stop the pipeline after retries instead of silently becoming “not found” and being marked seen.
- Customer Success and Customer Support now have dedicated buyer mappings.
- Founder/CEO titles are promoted for smaller companies.

## Role-focus personalization and routing

- Added deterministic `role_focus.py`; it converts explicit job-description signals into controlled noun phrases that fit directly after “focused on.”
- Raw JD sentences are never inserted into outbound copy. Capitalization, punctuation, length, and phrase structure are controlled by canonical mappings.
- Airtable now receives `Role Focus`, `Focus Quality`, and `Focus Evidence` so a reviewer can verify or edit the personalization before approval.
- Instantly enrollment now requires `Email`, `Company`, `Open Role`, and `Role Focus`; missing context fails safely instead of sending a generic or broken email.
- Added `role_focus` to Instantly custom variables.
- Automation Specialist now routes dynamically: CRM/RevOps/GTM automation to `gtm_revenue`; technical/AI automation to `engineering`.

## Lead model and Airtable

- Produces one lead per company + functional bucket rather than one lead per job posting, preventing duplicate outreach to the same buyer.
- Preserves all related openings while selecting a primary opening for copy.
- Airtable now receives the job URL, source, date, role relevance, firmographics, email source/status, company-size band, and campaign ID.
- Added deterministic `Lead Key` idempotency.
- Added `Enrolled` and `Error` lifecycle states.

## Instantly

- Uses documented v2 fields: `company_name`, `job_title`, `website`, `campaign`, `custom_variables`, `skip_if_in_workspace`, and `skip_if_in_campaign`.
- Preserves the actual intent signal in custom variables: open role, matched role, bucket, company size, posting date, source, and URL.
- Supports default, role-bucket, and role-bucket + size campaign routing.
- Duplicate responses are treated as idempotent success; real failures are written back to Airtable.

## Operations

- Removed the hard-coded Windows user path from batch files.
- Added static and read-only live setup validation.
- Added 16 unit tests covering the highest-risk logic, including role-focus formatting and dynamic Automation Specialist routing.
- Added Airtable schema and n8n scheduling documentation.
- Added `.gitignore` rules for secrets, logs, state, and raw/enriched data.

- Fixed limited JSearch smoke tests so CLI omission honors the env query cap and zero-yield successful queries do not fail API health validation.


## Intent-Based Outbound 2.0 quality and quota hardening

- Replaced the provider-flag-only remote decision with evidence precedence across
  title, location, precise description requirements, and the JSearch flag.
- Added traceable `_work_arrangement` and `_work_arrangement_reason` metadata.
- Rejected explicit hybrid, onsite, field-based, high-travel, and foreign-only
  eligibility cases while preserving remote-option and unknown cases for review.
- Added live-observed staffing, freelance marketplace, healthcare, nonprofit,
  media, and aggregator leakage controls.
- Added `run_filter_replay.py` for zero-credit offline reprocessing and role-level
  quality reporting.
- Changed the default JSearch page depth from 3 to 1 for the daily 118-role run.
- Added a 150 estimated-unit preflight budget and a 500-unit low-quota reserve.
- Added fail-fast handling for hard monthly/subscription 429 responses.
- Expanded the test suite from 81 to 102 passing tests.
- Offline replay of 1,356 saved postings produced 80 accepted jobs, removed 9
  prior leakage records, and recovered 67 remote jobs hidden by false provider
  flags, without making external calls.

## 2026-07-18 — Employer identity and daily volume hardening

- Reject publisher, aggregator, and ATS domains as employer identifiers before Apollo enrichment.
- Resolve domainless syndicated listings by employer name and require a compatible Apollo organization name.
- Validate the matched person's current organization and business-email domain against the resolved employer.
- Try up to three ranked hiring-manager candidates and continue after missing/invalid/mismatched emails.
- Search direct functional managers before executives; Founder, Co-Founder, and CEO are always true fallbacks.
- Add role-specific manager hierarchies for QA, Product Design, DevOps, Data, Sales Development, recruiting, GTM systems, and Shopify.
- Use remaining JSearch request-unit budget for adaptive page-2 discovery only on high-yield roles after full one-page catalog coverage.
- Prefer first-party-domain companies before the Apollo safety cap and print full filter/contactability funnels in Railway logs.
- Make Airtable Website use the validated company domain instead of a job-board domain.

## 2026-07-18 — Remote inventory and adaptive yield optimization

- Request remote jobs directly from JSearch and bias the search query toward remote US roles.
- Reuse the exact zero-credit Step 2 gates during scraping to measure true pre-enrichment yield.
- Rank page-2 calls by new unique jobs that survive aggregator, staffing, industry, remote, US, and role checks.
- Round-robin adaptive queries across functional buckets before allocating overflow.
- Cap adaptive expansion at 32 queries and stop carrying a role forward when an extra page adds no viable jobs.
- Raise the default eligible-company safety cap from 60 to 90 so a 30-reviewable-lead target is achievable at observed contactability rates.
- Add Railway logs for remote search settings, adaptive viable yield, and bucket allocation.
- Expand the test suite from 109 to 116 passing tests.


## 2026-07-18 — Paid-test precision recovery

- Stop treating a JSearch `country=US` echo plus `Location=Anywhere` as proof of US eligibility.
- Recover specific US locations from titles/state evidence and write them to Airtable.
- Reject explicit part-time, contractor, freelance, temporary, weekly-hour-limited, and non-active future-opening signals before enrichment.
- Add observed recruiting-platform, job-board, and staffing-company exclusions from the production audit.
- Expand Apollo industry exclusions for mental-health, healthcare, HR-services, and outsourcing accounts.
- Restrict Founder/CEO fallback to companies with at most 99 employees.
- Suppress Airtable companies in active lifecycle states across contacts and role buckets, while allowing Rejected and Error records to re-enter when a later job is qualified.
- Add a zero-credit Airtable CSV audit command and 14 regression tests covering the observed production failures.
