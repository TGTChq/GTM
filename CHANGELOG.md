# READY v1.4.5 actionable review policy — July 24, 2026

- Changed the Airtable boundary from fully verified only to actionable review: confirmed hard rejects remain terminal, while incomplete evidence is surfaced for human review.
- Unknown organization size, industry, source verification, contact territory/current-employment proof, and non-invalid email deliverability no longer silently remove leads.
- A lead still requires an aligned hiring manager, professional company-domain email, lead key, and no confirmed hard rejection.
- The daily/top-up target now uses Airtable-reviewable leads; fully verified `FINAL_PASS` remains separately observable.
- Approved review rows remain human-gated and are revalidated against hard failures before Instantly enrollment.
- `PRIMARY_MAX_JOB_AGE_DAYS` is now authoritative so a stale compatibility environment value cannot block production.

# READY v1.4.4 counterfactual recall recovery — July 24, 2026

- Recovers recently updated, active Greenhouse listings whose official board API omits `first_published`, while keeping them in the review/revalidation lane.
- Treats structured worldwide roles as US-inclusive unless the posting explicitly restricts candidates to a foreign region; unstructured JSearch/aggregator records remain conservative.
- Moves structured provider employer-identity conflicts to review rather than hard rejection; unstructured conflicts still fail closed.
- Restores placeholder employers only from a separately verified ATS board company identity.
- Prevents generic sentence starters such as `This is a...` from being interpreted as conflicting employer names.
- Adds exact recall-recovery metrics to shadow reports and an offline counterfactual replay command with zero external calls.
- Real-corpus replay: 1,058 raw / 983 unique postings; 69 to 93 prefilter survivors; 24 recovered; 0 lost.
- Validation: 457 repository tests and 15 focused v1.4.4 regression tests passed; Python compilation and clean diff checks passed; zero live external calls.

# READY v1.4.1 source observability and minimum recovery — July 23, 2026

- Reports actual JSearch request attempts, successes, estimated units, normalized jobs, errors, and skip state in shadow output.
- Adds exact shadow rejection diagnostics by reason and acquisition source, with bounded examples for false-negative review.
- Fixes a posting-integrity overfilter that required verified ATS records to repeat the employer identity inside the description; malformed, generic, aggregator, conflicting, and unstructured syndicated records remain blocked.
- Rejects generic Workable product URLs as company ATS boards and prunes legacy invalid registry entries.
- Hardens Himalayas public company-profile retrieval with browser-compatible requests, extra identity metadata, detailed failure metrics, and a repeated-access-failure circuit breaker.
- Separates multi-source FINAL_PASS top-up limits from legacy JSearch-only settings.
- Removes the stale two-microbatch ceiling in multi-source mode while retaining request-unit, runtime, inventory, and downstream-yield bounds.
- Clarifies that the minimum of 30 applies to FINAL_PASS leads; shadow contact-eligible companies are an earlier funnel stage.
- Validation: Python compilation passed; 14 focused v1.4.1 tests and 425 total offline tests passed; zero live external calls.

# READY v1.3.3 source-truth hardening — July 23, 2026

- Treats explicit Ashby `Hybrid`/`OnSite` workplace types as authoritative over a broad `isRemote=true` eligibility flag.
- Preserves raw Ashby workplace and remote fields for auditability.
- Adds generic in-office requirement detection in both the prefilter and Job Gate fact extractor.
- Adds bounded, identity-verified Himalayas company-profile enrichment for website and employee range.
- Rejects provider employee ranges that cannot overlap the 25–1,000 employee ICP before Apollo.
- Uses verified provider-profile text for narrow healthcare-industry exclusions.
- Detects CamelCase industry brands such as `UnitedHealth`.
- Rejects PEO/co-employment service-delivery roles before enrichment.
- Preserves verified profile evidence when an official ATS posting wins cross-source deduplication.
- Adds shadow metrics for profile requests, verification, websites, employee ranges, and enriched jobs.
- Validation: Python compilation passed; 399 tests passed; zero live paid-service calls.

# READY v1.3.2 cumulative preproduction audit — July 23, 2026

- Includes all v1.3.1 shadow-quality fixes and supersedes the earlier v1.3.1 package.
- Rejects exact global-remote locations before description text can imply US eligibility.
- Runs the zero-credit Job and Role Gates inside shadow mode and labels Step-2 survivors as postings, not FINAL_PASS leads.
- Uses conservative company/ATS identity compatibility to prevent substring collisions such as `Meta` vs `metabase`.
- Prevents weak feed evidence from overwriting stronger ATS registry identity; records conflicts for diagnostics.
- Re-propagates verified company domains after direct ATS acquisition and refuses propagation when multiple domains conflict.
- Uses Greenhouse `first_published` for freshness and rejects direct Greenhouse records whose date was not checked or unavailable before paid enrichment.
- Bounds Greenhouse detail requests globally/per board and limits forced ATS refresh during shadow runs.
- Preserves Ashby remote/location evidence and uses source-aware evidence labels instead of JSearch-specific labels.
- Fixes pre-contact `NEEDS_CHECK` counting and adds explicit shadow stage semantics.
- Added ten focused audit regressions; complete suite: 388 passed.

# READY v1.3 free multi-source acquisition — July 23, 2026

- Replaced JSearch as the production acquisition default with five free global feeds: Himalayas, Jobicy, We Work Remotely, Remotive, and Remote OK.
- Added automatic public ATS discovery, persistence, and direct acquisition for Greenhouse, Lever, Ashby, Recruitee, Workable, and Personio.
- Added cross-source employer/title deduplication that prefers official ATS records while preserving all discovery provenance and apply URLs.
- Added bounded landing-page discovery and fail-closed response parsing, redirect validation, private-network blocking, and response-size limits.
- Added source-level yield metrics and an acquisition/filter shadow command with zero JSearch, Apollo, Hunter, Airtable, or Instantly calls.
- Kept JSearch as an explicit rollback mode only; JSearch top-up is disabled in free multi-source production mode.
- Corrected `topup_filter_error`: a non-empty micro-batch with zero filter survivors is now treated as zero downstream yield, not a technical failure.
- Added focused regression coverage for all global feed formats, ATS detection/fetching, automatic registry growth, deduplication, and the zero-yield top-up path.

# FINAL_PASS v0.5 production hardening — July 22, 2026

- Added strict ATS employer identity, positive job-activity proof, multi-URL evaluation, and public Greenhouse, Lever, and Ashby board adapters.
- Separated official and provider evidence provenance; provider-only contradictions no longer masquerade as official evidence.
- Added current-employment and US/global ownership validation for hiring managers, with reason-specific temporary and permanent reroute expiration.
- Added recoverable-job inventory, FINAL_PASS persistence safety, and crash/query-progress checkpoints.
- Added HMAC validation fingerprints and full job/account/contact/email revalidation immediately before Instantly enrollment.
- Split technical completion from daily SLA success; net Airtable creation controls SLA and a miss exits with code 2.
- Added 21 focused v0.5 regression tests. Full suite: 297 tests plus 135 catalog subtests.
- Offline replay keeps PTP unverified, rejects Hoplite and Benzinga, and safely recovers GradeBuzz from captured evidence.

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

## v1.4.3 — Final verified production hardening
- Supersede the unshipped v1.4.2 package after a 300,972-case synthetic stress audit.
- Preserve valid Workday site IDs while pruning only false `/job` registry entries.
- Recover short catalog aliases, punctuation-equivalent titles, and permanent employment labels.
- Reject placeholder employer identities and align physical-role filtering earlier in the funnel.
- Explicitly enforce the 0–14 day shadow window.
