# Provider contract checks

Status legend:
- **V**: verified in this audit, with evidence cited.
- **H**: hypothesis from an earlier session. Re-verify it before relying on it.
- **?**: unknown.

Never promote an H to a V without a cited artifact.

## Fantastic.jobs

**Where it lives:**
- Client: `tgtc_core/providers/fantastic.py`.
- Filters: `tgtc_core/domain/acquisition_query.py`.
- Canary arms: `tgtc_core/services/daily_24h_canary.py` (uncommitted).
- Saved requests and responses:
  - `C:\TGTC\tgtc_canary_evidence\run_20260919T061752Z\` (`requests_redacted.jsonl`, `pages.jsonl`, `net_new_rows.jsonl.gz`);
  - `strategy_20260919\records_*`, `strategy_20260919\counts_*`;
  - `fantastic_support_bundle_20260918\`.
- Docs: developer.fantastic.jobs. Read them live. Record the date and quote only short fragments.

**Production path.** The core calls the Direct API with Bearer auth: `/v1/active-jb`, `/v1/active-ats` and the `-count` variants. The legacy repo root also has `fantastic_jobs_adapter.py` and Apify paths. Establish which of these the **deployed** image uses: `Dockerfile.core` copies only `tgtc_core/`, `domain_utils.py` and `source_domains.py`.

**Billing headers:**
- `x-api-jobs-this-request` gives the records billed.
- `x-api-jobs-limit` / `-remaining` and `x-api-requests-limit` / `-remaining` give the plan quota. The plan seen on 2026-09-19 was 100,000 jobs and 50,000 requests per month.
- Count endpoints bill 0 jobs but do consume a request. Record both.

### Check procedure (every parameter, every endpoint)

1. **Name.** Diff every parameter the code sends against the live docs parameter list. Check V1 versus obsolete names and the exact spelling (for example `location` vs `location_filter`, `organization_headcount_*` vs `organization_employees_*`). An undocumented name is a defect until proven honoured.
2. **Type and shape.** Check:
   - arrays versus comma strings (employment type);
   - CSV quoting of values that contain commas (`provider_list`);
   - URL encoding;
   - `id` as integer versus string;
   - boolean spelling (`true` vs `True`);
   - `title_advanced` boolean precedence and parentheses.
3. **Applied-semantics canaries.** These cost zero job credits because they run on `-count`. Hold everything else constant and run:
   - `base`;
   - `base + filter`;
   - `base + impossible value`, which **must return 0**. Examples: a nonexistent taxonomy, headcount_gte = 10^9, a nonexistent country;
   - `base + opposite value`;
   - `base + filter` restricted to a source whose field is null (Wellfound/YC).

   A filter counts as applied only when `impossible = 0`, `filter ≤ base`, and the null behaviour is recorded.
4. **Record check.** On already-saved pages, verify every returned row satisfies every sent filter. Count the violators per filter.
5. **Pagination and completeness.** Check:
   - `limit` bounds;
   - `offset` versus `cursor` (the cursor is for `6m` only);
   - that the default order is `date_posted` DESC;
   - that `time_frame` filters on `date_created`;
   - stop conditions (a short page, the ceiling, errors);
   - that the sum of pages equals the count;
   - that no page is skipped after a 400, 429 or 504;
   - failed-run recovery (`7d` + `date_created_gte/lt`).

   Prove that an error cannot silently end a partition marked "complete".
6. **Retries.** For each of 408/429/5xx, record: retried? charged? recorded as uncertain? Timeouts on the last attempt must be recorded as possibly billed.

### Known facts to re-verify

| Fact | Status |
|---|---|
| `ai_taxonomies_a` matches ANY assigned tag; `ai_taxonomies_a_primary` matches the first tag only | H (Remco + docs + counts, 09-19) |
| The headcount, employment and industry-exclusion filters DROP rows where the field is null (Wellfound/YC 66 → 0) | H |
| Current LinkedIn industry labels ("Non-profit Organizations", "Medical Practices", …) differ from the retired labels in `EXCLUDED_LINKEDIN_INDUSTRIES`, so those exclusions may never fire | H |
| `location="United States"` is hard-coded as the default; policy `market = us_market`. Non-US geographies are not requested at all | H (code read) |
| Provider `location_type=TELECOMMUTE` is never mapped to remote | H |
| `description` search returns 400 on `6m` | H |
| Only LinkedIn and ATS rows are re-checked for expiry; Wellfound/YC never are | H |
| `/v1/active-jb` `source` takes `linkedin`, `wellfound`, `ycombinator` | H |

## Apollo

**Where it lives:**
- Client: `tgtc_core/providers/apollo.py`.
- Search and selection: `tgtc_core/services/opportunity.py`.
- Personas: `tgtc_core/policy/campaigns.py` (`DIRECT_BUYER_TITLES`, `EXECUTIVE_BUYER_TITLES`, `buyer_titles`, `is_founder_tier`).
- Uncommitted mapper: `tgtc_core/domain/contact_mapping.py` (four-group scoped, so void in scope).
- Pilot payloads: `C:\TGTC\tgtc_canary_evidence\contact_pilot_20260919\` and `contact_offline_20260919\`. Their `private/` directories hold PII: aggregate only, never copy.

**Endpoints used:**

| Endpoint | Cost |
|---|---|
| `POST /mixed_people/api_search` | documented 0 credits |
| `POST /people/match` | paid; sends `reveal_personal_emails=false`, `reveal_phone_number=false` |
| `GET /organizations/enrich` | paid |

The rate limits, not credits, come from `usage_stats/api_usage_stats`.

### Check procedure

1. Confirm each endpoint and parameter against docs.apollo.io, as for Fantastic. Check `person_titles[]`, `include_similar_titles`, `q_organization_domains_list[]`, `organization_ids[]` and `contact_email_status[]`.
2. **Free before paid.** Prove from code that search precedes any match, and that no match runs for a candidate that fails domain, current-employer or function checks.
3. **Identity.** A candidate's current organization domain or LinkedIn slug must equal the resolved employer's. Check how a previous employer, a subsidiary, an alternate domain or a LinkedIn division page is handled.
4. **Titles.** Check exact versus similar-title behaviour and title normalization. Measure persona coverage **per campaign**, so all 9 need personas.
5. **Dedupe.** Check that a person or email is enriched once globally. Check caching and TTL, and reuse across jobs and campaigns. Find any path that skips a valid colleague because another contact at the same company already exists.
6. **Credits.** Reconcile credits consumed against calls made. A `people/match` with a fake id RETURNED a person, so it is not a free probe: count 1 credit.
7. Never reveal mobile numbers. Rank candidates before paying. Measure contacts 1, 2 and 3 separately, per campaign.

### Known facts to re-verify

| Fact | Status |
|---|---|
| `buyer_titles` is an exact list; it misses "Vice President, X" and "SVP X" | H |
| `is_founder_tier("Vice President of Sales")` is True because of the "president" token | H (code read) |
| Strict employer-domain equality loses alternate-domain employers | H |
| VC-attributed postings (Newfund, SoGal) and LinkedIn division pages pass qualification | H |
| Search limits: 200/min, 6k/hour, 50k/day | H |
