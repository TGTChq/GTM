# TGTC AI-Powered Job Intent & Outbound Pipeline

An automated outbound pipeline that identifies companies actively hiring for roles TGTC can support, finds the appropriate hiring manager, enriches contact information, routes qualified leads through Airtable for human review, and enrolls approved leads into the correct Instantly campaign.

## Pipeline

```text
JSearch
→ Role relevance
→ Business and ICP filters
→ Company enrichment
→ Hiring manager identification
→ Apollo / Hunter contact enrichment
→ Airtable human review
→ Instantly campaign enrollment
```

## Core Capabilities

* Collects recent US job postings across the centralized Intent 2.0 role catalog
* Normalizes 100+ search titles into canonical roles, functions, and buyer hierarchies
* Matches duplicate search results to the most specific relevant role and campaign
* Excludes staffing firms, out-of-scope industries, explicit in-person/non-paying roles, non-US roles, duplicates, CRM companies, and companies outside the target employee range
* Routes technical and GTM automation roles according to job-description signals
* Separates campaign function from hiring-manager routing so Data, IT, Finance, HR, Product, E-commerce, and other roles reach the right buyer
* Identifies the most relevant hiring manager across all related openings in the same company/function
* Uses Apollo and Hunter for contact enrichment
* Generates concise, role-specific personalization from job-description signals
* Groups related openings by company and functional bucket
* Sends qualified leads to Airtable for human review
* Enrolls approved leads into the correct Instantly campaign
* Prevents duplicate Airtable records and duplicate campaign enrollment

## Workflow

### Daily Pipeline

```bash
python -u run_daily.py
```

The daily pipeline performs scraping, qualification, enrichment, and Airtable synchronization.

### Approved Lead Sync

```bash
python -u run_approved.py
```

The approval worker checks Airtable for approved leads and enrolls them into the appropriate Instantly campaign.

## Human Review

Qualified leads are organized in Airtable using four primary views:

* Ready to Approve
* Review Queue
* Enrolled
* All Leads

The reviewer validates the company, open role, role focus, hiring manager, and email before approving a lead.

## Production Deployment

The production deployment uses Railway with two scheduled services:

```text
Daily Pipeline
→ runs once per day

Approved Lead Sync
→ runs every five minutes
```

Application secrets and API credentials are configured through Railway environment variables and are not stored in the repository.

## Local Setup

Create and activate a virtual environment:

```bash
python -m venv .venv
.venv\Scripts\activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Create a local `.env` using `.env.example` as the reference.

Run validation (102 tests, including live-data regression hardening):

```bash
python -m unittest discover -s tests -v
python validate_setup.py
```

Run a controlled end-to-end test:

```bash
python -u run_full_test.py --companies 3 --push-airtable
```

## Security

The following files and directories are excluded from version control:

* `.env`
* local virtual environments
* API debug responses
* generated logs
* raw, filtered, and enriched run data

Production credentials must be stored only in Railway environment variables.

### JSearch smoke-test semantics

Limited `run_scrape_test.py` runs validate API/auth/quota health even when a valid query produces zero selected jobs. Full-catalog runs still enforce production yield and role-distribution gates. Omitting `--max-queries` honors `JSEARCH_MAX_QUERIES_PER_RUN`; pass `--max-queries 0` to force the complete catalog.


## Offline filter replay and quota-safe JSearch operation

The daily 118-role catalog is designed to run with one JSearch page per role:

```env
NUM_PAGES=1
JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN=150
JSEARCH_STOP_ON_LOW_QUOTA=1
JSEARCH_MIN_REMAINING_REQUESTS=500
JSEARCH_REMOTE_JOBS_ONLY=1
JSEARCH_REMOTE_QUERY_BIAS=1
JSEARCH_ADAPTIVE_DEEPENING=1
JSEARCH_ADAPTIVE_MAX_EXTRA_QUERIES=32
JSEARCH_ADAPTIVE_MIN_PREFILTER_VIABLE=1
JSEARCH_ADAPTIVE_BUCKET_BALANCING=1
```

The scraper estimates request units before its first network call. A 118-role,
three-page run estimates 354 units and is blocked by the default 150-unit guard.
The base request asks JSearch for remote inventory directly. Remaining units are
allocated only to roles whose first page survives the same local aggregator,
staffing, industry, work-arrangement, geography, and role-quality gates used by
Step 2. Allocation round-robins across functional buckets before giving a bucket
more page-2 calls. Hard monthly/subscription quota responses abort immediately.

Saved JSearch data can be re-filtered without any external calls:

```bash
python run_filter_replay.py --input data/raw/jobs_YYYY-MM-DD.json
```

The replay writes accepted/rejected JSON, a role-level CSV, and a quality report
under `data/replay/`. It does not call JSearch, Apollo, Hunter, Airtable, or
Instantly.


## Definitive quality and controlled-volume layer

The pipeline now applies a shared zero-credit pre-enrichment decision layer in
both scraping and final filtering. It addresses failure families rather than
individual company names:

- posting integrity: roundup pages, generic employers, malformed titles, and
  expired embedded application deadlines;
- employment quality: SkillBridge, internships, externships, apprenticeships,
  fellowships, freelance/project work, and hidden non-full-time arrangements;
- restricted delivery: security-clearance roles and work explicitly supporting
  federal agencies or government programs;
- employer integrity: staffing, BPO/outsourcing, ATS wrappers, excluded
  industries, and mission-driven nonprofit `.org` organizations when the
  description corroborates that business model;
- role semantics: sales vs. public relations Account Executives, customer
  Product Support vs. inventory/catalog operations, and video vs. static design;
- geography: explicit US evidence is accepted without treating ordinary tokens
  such as `OK` or `PR` as state abbreviations.

Volume is recovered without lowering those standards. Keep `NUM_PAGES=1`. When
the normal catalog and selective page-2 queries remain below 60 viable jobs, a
reserved portion of the same 150-unit budget runs up to 16 diversified one-week
lookback queries. Duplicate job IDs are removed before role selection.

The offline regression benchmark is reproducible:

```powershell
$env:PRODUCTION="0"
python run_filter_replay.py --accepted data/replay/jobs_filtered_2026-07-17.json --rejected data/replay/jobs_rejected_2026-07-17.json --output-dir data/replay/definitive
```

It processes 1,356 saved postings with no JSearch, Apollo, Hunter, Airtable, or
Instantly calls. The 30-lead setting remains a production target, not a promise:
actual reviewable output still depends on live job inventory, firmographics,
hiring-manager availability, and verified email coverage.

## Paid-test quality recovery

The production filter is quality-first even with the full 118-role catalog:

- `Anywhere` is not treated as US evidence by itself. A remote listing needs an
  explicit US scope, a US state/location signal, or another specific US marker.
- Explicit part-time, contractor, freelance, temporary, hourly-limited, future
  opening, evergreen, and talent-pool posts are rejected before Apollo.
- Known job boards, recruiting platforms, staffing firms, and observed
  intermediary employers are rejected before enrichment.
- Apollo industry checks remove excluded healthcare, recruiting, outsourcing,
  government, nonprofit, events, media, and chemical accounts.
- Founder/CEO fallback is available only for companies with 99 or fewer
  employees; larger accounts require a functional leader.
- Airtable suppresses a company already present in an active lifecycle state,
  even when the contact or role bucket would create a new Lead Key. Rejected
  and Error records may re-enter when a later job is genuinely qualified.
- The Airtable `Location` field uses recovered evidence such as `Campbell, CA` or
  `Remote, United States` instead of the provider's generic `Anywhere` label.

Audit an Airtable export without consuming API credits:

```powershell
python audit_airtable_export.py "Leads-All Leads.csv" --last 16 --output lead_quality_audit.csv
```

The audit writes PASS/REVIEW/REJECT decisions and deterministic reasons using the
same local gates as production.


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
