# TGTC core rebuild — integration map

Everything here was read from the repository at `main = 5d87851` or from the local
machine on 2026-09-08. **No secret value was read or printed.** Where a fact could
not be verified from here it is marked *unverified* with what would verify it.

## 1. Repository and branch

| Item | Value |
|---|---|
| Remote | `https://github.com/TGTChq/GTM.git` |
| Base | `main = 5d87851` (PR #126). The blueprint's inspected checkout `GTM-rebuild` is this commit. |
| Rebuild worktree | `C:/TGTC/tgtc_rebuild`, branch `feat/rebuild-core`, created from `main` |
| Session's original worktree | `C:/TGTC/tgtc_pipeline_diagnostic_integration` on `feat/row2-diagnostic-instrumentation` — 333 commits **behind** main; untouched |
| Other worktrees with uncommitted work | `tgtc_retrieval_measurement_m1` (modified sources + untracked `retrieval_measurement/`), `tgtc_pipeline_audit` (detached HEAD, modified + untracked), `tgtc_pipeline` (two untracked JSON artifacts). None were touched. |
| Legacy test suite | 3,753 tests collect on `main` (`pytest tests --collect-only`) |

## 2. Local environment

| Dependency | Status |
|---|---|
| Python | 3.12.10 (production image is `python:3.12-slim`); 3.14 also present |
| PostgreSQL | **No server installed** (`psql`, `pg_ctl`, `initdb`, Docker all absent). Installed `pgserver 0.1.4` (pip, bundles PostgreSQL 16.2 binaries) and `psycopg 3.3.5`. Verified: server starts in ~2.3 s, accepts connections, `ON CONFLICT` works. Used **only** for local tests; not a production service. |
| Inference | No `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` in the environment or in the main checkout's `.env` names. `ANTHROPIC_BASE_URL` is set (harness). The semantic adapter is implemented against the documented Messages API and exercised with a replay adapter; **a live inference call is unverified**. |
| Provider credentials present locally (names only) | `.env` in `C:/TGTC/tgtc_pipeline`: `APOLLO_API_KEY`, `HUNTER_API_KEY`, `AIRTABLE_TOKEN`, `AIRTABLE_BASE_ID`, `AIRTABLE_TABLE_NAME`, `INSTANTLY_API_KEY`, `INSTANTLY_CAMPAIGN_*` (10 + `INSTANTLY_CAMPAIGN_ID`), `FANTASTIC_JOBS_API_KEY`, `VALIDATION_SIGNING_KEY`, `RAPIDAPI_KEY`, Adzuna keys. Process env: `FANTASTIC_JOBS_API_KEY`. **None used in this task.** |

## 3. Production topology (from `PRODUCTION_RUNBOOK.md`, read 2026-09-04; re-read before acting)

| | GTM | GTM Approved Sync |
|---|---|---|
| Purpose | acquisition → qualification → enrichment → Airtable | Airtable `Approved` → Instantly |
| Cron (UTC) | `0 3 * * *` | `0 0 * * *` |
| Volume | `gtm-volume` → `/app/data/state` | none |
| Start command | `run_orchestrator.py --mode live_acquisition_and_enrichment --lanes fantastic …; run_weekly_report.py …` | `python -u run_approved.py` |
| Effective state 2026-09-07 | `MAINTENANCE_ONLY=1`, `FANTASTIC_JOBS_ENABLED=0`, Apollo grant `0` (spent id), Apollo verified serving 19:23Z | `OUTBOUND_WAVE1_ENABLED=1`, 50/50 split |

There is **no PostgreSQL** in the Railway project (`railway.json`, runbook, and
memory all describe a volume-backed JSON state store). A managed PostgreSQL is a
new service with a cost; it is **not** created by this task (see deployment plan in
`HANDOFF.md`).

## 4. Provider contracts as evidenced in the repository

### 4.1 Fantastic Direct API (`fantastic_jobs_adapter.py`)

* Base `https://data.fantastic.jobs`, `Authorization: Bearer <key>`.
* Endpoints: `GET /v1/active-jb` (job boards; `source=linkedin|wellfound|ycombinator`), `GET /v1/active-ats`, `GET /v1/active-jb-count` (0 job credits; headers are the payload).
* Params proven live: `time_frame` (`1h|24h|7d|6m`), `limit`, `offset` (default order `date_posted` DESC), `cursor` (id ASC, **only with `6m`**, never mixed with offset), `date_created_gte` / `date_created_lt` (honoured; intersected with `time_frame`), `description_format=text`, `include_basic_organization_details=true`, `exclude_ats_duplicate=true`, `location`, `organization_headcount_gte/lt` (null-excluding), `ai_employment_type`, `organization_agency=exclude`, `exclude_organization_industry` (comma form).
* **Not sent by the new package:** `title_advanced`, `description_advanced`. Headcount/industry server filters are also not sent because they drop rows with null firmographics (proven 2026-09-04: Wellfound/YC rows have none); the new package filters downstream on Apollo/Fantastic facts and reports the cost.
* Quota headers: `x-api-jobs-limit`, `x-api-jobs-remaining`, `x-api-requests-limit`, `x-api-requests-remaining`, `x-api-next-billing-date`.
* Billing model as documented by the provider: one job credit per row returned, one request credit per request. Recorded per page in `page_receipts`; confirmed usage comes only from the headers.
* Record fields consumed: `id`, `title`, `organization`, `organization_url`, `org_linkedin_name|slug|website|headcount|size|industry|recruitment_agency_derived`, `domain_derived`, `countries_derived`, `locations_derived`, `location_type`, `date_created`, `date_posted`, `date_valid_through`, `description_text`, `source`, `source_type`, `employment_type`, `ai_employment_type`, `ai_taxonomies_a`, `ai_key_skills`, `ats_duplicate`, `url`. **Dropped as PII:** `recruiter_*`, `ai_hiring_manager_*`.
* Status handling: 401/403 auth (stop source), 429 quota/rate (stop source, keep pages), 408/5xx retry bounded, other non-200 fail the page only.
* Plan, balance and channel capabilities: *unverified from here* (a 0-credit `/v1/active-jb-count` call would read the headers).

### 4.2 Apollo (`apollo_client.py`, `apollo_errors.py`)

* Base `https://api.apollo.io/api/v1`, header `X-Api-Key`.
* `GET /organizations/enrich?domain=&name=` — **paid**. Returns `organization{id,name,primary_domain,estimated_num_employees,industry,linkedin_url,founded_year,…}`.
* `POST /mixed_people/api_search` with query params `q_organization_domains_list[]`, `organization_ids[]`, `person_titles[]`, `include_similar_titles=false`, `page`, `per_page` — **documented 0 credits**; returns `people[]` without emails plus `pagination.total_pages`.
* `POST /people/match?id=&reveal_personal_emails=false&reveal_phone_number=false` — **paid**; returns `person{email,email_status,organization,employment_history,linkedin_url,…}`. `email_status ∈ {verified, extrapolated, unavailable, likely_to_engage, unverified}`.
* Error taxonomy (production-proven): credit exhaustion is **only** an explicit body marker (`BILLING.LIMIT.CREDITS_EXHAUSTED`, "insufficient credits", …, with `error_details.context.{credit_type,credit_balance,next_billing_date}`); 429 or a long `Retry-After` is rate limiting; 401/403 authorization; 404/422 without the marker is record-level validation; 5xx/network is transient. A refusal is durable state (`provider_state`) retried on an interval, never per company.
* Pricing per operation and the team's plan: *unverified*; the invoice/usage page prevails over the estimate column.

### 4.3 Airtable (`airtable_client.py`, `validation_integrity.py`)

* `https://api.airtable.com/v0/{base}/{table}`, bearer token, `typecast: true`, batches of ≤ 10, 5 requests/s per base, 429 → wait 30 s.
* Field names in use (subset written by the new package): `Lead Key`, `Company`, `Outbound Company`, `Outbound Company Confidence`, `Outbound Company Identity`, `Outbound Hold`, `Website`, `Open Role`, `Open Roles`, `Outbound Role`, `Outbound Roles`, `Outbound Role Confidence`, `Role Focus`, `Focus Quality`, `Focus Evidence`, `Matched Role`, `Role Bucket`, `Job URL`, `Job Source`, `Posted At`, `Job Age Days`, `Job URL Status`, `Job URL Source`, `Job Signal Notes`, `Location`, `Employment Type`, `Relevance`, `Hiring Manager`, `HM Title`, `LinkedIn`, `Apollo Person ID`, `Email`, `Email Source`, `Apollo Email Status`, `Employees`, `Size Band`, `Industry`, `Campaign ID`, `Job ID`, `Final Decision`, `Decision Reason`, `Evidence Status`, `Firmographics Status`, `Contact Alignment`, `Email Validation`, `Validation Version`, `Validated At`, `Validation Fingerprint`, `Evidence Bundle`, `Status`.
* `Status` values: `Pending`, `Approved`, `Rejected`, `Enrolled`, `Error`. The new package writes only `Approved`.
* Legacy `Lead Key = <domain>|<email>|<bucket>` (`hiring_manager._lead_key`). Kept as the stable idempotency key so existing rows suppress correctly.
* Legacy Approved Sync eligibility (`send_safe_facts`) requires `Validation Version == config.VALIDATION_VERSION` (`tgtc-ready-v1.4.7-role-display-2`) and a matching HMAC fingerprint. New rows carry `Validation Version = tgtc-core/1` ⇒ classified **legacy → skipped, no write**. Verified by `tests_core/test_legacy_consumer_exclusion.py`, which imports the legacy function read-only.
* Existing-row suppression keys (`_company_identity_keys_from_fields`): `domain:<host>` (never an ATS/intermediary host), `name:<normalized>`, `linkedin:<slug>` (high/medium confidence, not held), each qualified `|bucket:<function>`; active statuses = everything except `Error`/`Rejected`. The new `suppressions` importer derives the same keys.

### 4.4 Instantly v2 (`instantly_client.py`, `validate_wave1_variables.py`)

* Base `https://api.instantly.ai/api/v2`, bearer token.
* `POST /leads` — returns **200 for an existing workspace email too**; `timestamp_created` vs request start (120 s skew) tells created from pre-existing; `GET /campaigns/search-by-contact?search=<email>` is the only truthful membership lookup (`/leads/list` ignores `campaign_ids`). Documented body fields used: `campaign, email, first_name, last_name, company_name, job_title, website, skip_if_in_workspace, skip_if_in_campaign, verify_leads_on_import, custom_variables`.
* Control-A `custom_variables` (exact names): `open_role, open_roles, role_focus, matched_role, role_bucket, company_size, company_size_band, job_posted_at, job_source, job_url, job_freshness, job_age_days, job_url_status, job_url_source, relevance`. Values must be string/number/bool/null.
* `skip_if_in_campaign=true` always; `skip_if_in_workspace` follows the person–employer uniqueness policy (production Approved Sync: `1`).
* Live Control campaign ids (from `validate_wave1_variables.CONTROL_CAMPAIGN_IDS`, not secrets): PRODUCT `45ac1e03-…`, OPERATIONS `4effab2f-…`, FINANCE `1db88bbe-…`, PEOPLE & HR `cf01e56b-…`, ECOMMERCE `0f0f57d5-…`, CUSTOMER EXPERIENCE `1747c87e-…`, MARKETING & CREATIVE `165c9e87-…`, GTM SYSTEMS `917973f3-…`, AI & TECHNICAL `04670c6a-…`. The package resolves ids from env at runtime and only uses this list to **refuse** a payload aimed at a campaign id that is neither a configured Control nor a configured Challenger.
* Wave 1 Challenger campaigns (nine, Draft, 24 variables) exist; the overlay runs in Approved Sync only. The new consumer exposes an `EnrollmentOverlay` hook, default no-op. Sending capacity/mailbox limits: *unverified*.

### 4.5 Hunter

Integrated in legacy as optional corroboration that fails open and can never
promote. Not called by the new package (policy §5). Client retained for a future
second-authority decision.

## 5. Outcome sources

`outbound_wave1/outcomes.py` reads Instantly `POST /leads/list` (`campaign` singular
filter) and derives `bounced` (`status == -1`), `replied` (`email_reply_count > 0`),
`lt_interest_status` classes. The new `outcome_events` table and importer accept
those shapes; a live read is not performed here.

## 6. Missing access / unverified dependencies (exact)

1. **PostgreSQL in production** — none exists. Needs a decision: Railway Postgres plugin (paid) or external. Local tests use an embedded server.
2. **Inference credentials** — no API key available. `AnthropicMessagesAdapter` is implemented and schema-tested with recorded responses; the live path is unverified.
3. **Apollo balance / plan pricing** — unknown from here. A served call on 2026-09-07 proves credits existed then, not how many.
4. **Fantastic plan and quota headers for this account** — unknown; a 0-credit count call reads them.
5. **Instantly sending capacity and the current status of each Control campaign** — unknown; ECOMMERCE Control was `completed` on 2026-09-03.
6. **Airtable field types** — the field *names* are proven by production writes; a first write from the new package should go to an isolated acceptance table (deployment plan).

## 7. Reused code (documented, by module)

| Reused | How | Why acceptable |
|---|---|---|
| `domain_utils.normalize_company_domain` | imported | pure, PSL-backed, no config import, production-proven |
| `source_domains.ATS_DOMAINS` | imported | pure set |
| `company_identity` name normalisation and compatibility heuristics | **ported** into `tgtc_core/domain/identity.py` (no alias env lookup; aliases come from `employer_aliases`) | the module imports legacy `config` lazily; porting avoids that side effect |
| `job_fact_extractor` / `job_quality` / `contact_gate` / `email_gate` regex families | **ported** into `tgtc_core/domain/facts.py` and `gates.py` with attribution | proven on production corpora; the surrounding control flow (NEEDS_CHECK, catalogue) is rejected policy |
| `apollo_errors` credit markers and category ordering | **ported** into `tgtc_core/providers/apollo.py` | the legacy classifier is bound to `requests.Response` exceptions |
| `instantly_client.classify_membership` semantics | **ported** into `tgtc_core/providers/instantly.py` | integration truth (200-for-existing) |
| `outbound_wave1/campaigns.py` registry | **ported** to `tgtc_core/policy/campaigns.py`; drift test compares them | same nine campaigns, ten functions |
| `role_mapping.BUCKET_TITLES/BUCKET_DIRECT_TITLES` | **ported** as buyer hierarchies | policy data, not control flow |

Nothing else from the legacy tree is imported by `tgtc_core/`; `tests_core/test_no_legacy_imports.py` enforces it.
