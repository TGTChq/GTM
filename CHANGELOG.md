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
