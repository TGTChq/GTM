# Changes to apply to your existing `.env`

Keep your current `.env`; do not replace it with `.env.example`. Add the following variables that are missing.

At minimum:

```env
INSTANTLY_CAMPAIGN_ID=<default campaign UUID>
PRODUCTION=1
DATE_POSTED=today
```

For separate sequences by functional family:

```env
INSTANTLY_CAMPAIGN_GTM=
INSTANTLY_CAMPAIGN_ENGINEERING=
INSTANTLY_CAMPAIGN_MARKETING=
INSTANTLY_CAMPAIGN_CUSTOMER_SUCCESS=
INSTANTLY_CAMPAIGN_CUSTOMER_SUPPORT=
INSTANTLY_CAMPAIGN_FINANCE=
INSTANTLY_CAMPAIGN_OPERATIONS=
INSTANTLY_CAMPAIGN_PEOPLE_HR=
INSTANTLY_CAMPAIGN_PRODUCT=
INSTANTLY_CAMPAIGN_ECOMMERCE=
```

Optional size overrides:

```env
INSTANTLY_CAMPAIGN_MARKETING_SMALL=
INSTANTLY_CAMPAIGN_MARKETING_MID=
INSTANTLY_CAMPAIGN_MARKETING_LARGE=
```

Recommended behavior during the paid test:

```env
REJECT_UNKNOWN_FIRMOGRAPHICS=0
ENFORCE_FOUNDED_BEFORE=0
ENABLE_BROADER_INDUSTRY_EXCLUSIONS=1
VERIFY_WITH_HUNTER=1
ENFORCE_HM_MATCH_RATE=0
REQUIRE_STAFFING_GROUND_TRUTH=0
DEBUG_API_RESPONSES=0
```

After you have labeled a validation sample and the system is stable, you can turn stricter gates on:

```env
REQUIRE_STAFFING_GROUND_TRUTH=1
ENFORCE_HM_MATCH_RATE=1
```

Your current `.env` contains the API credentials but no Instantly campaign UUID. The API key alone is not enough to know which sequence should receive an approved lead.

## Intent-Based Outbound 2.0 role catalog

Leave `ROLES_JSON` unset to use the complete centralized role catalog. If
Railway already has `ROLES_JSON`, it overrides the catalog and must either be
removed or replaced intentionally. Check it before deployment; do not paste API
credentials into chat or source control.

The new role functions can enter Airtable before every function-specific
campaign is ready, but they cannot be enrolled unless either a matching
`INSTANTLY_CAMPAIGN_*` variable or the default `INSTANTLY_CAMPAIGN_ID` exists.
`python validate_setup.py` now warns about any uncovered active bucket.

The founded-before-2010 filter remains disabled:

```env
ENFORCE_FOUNDED_BEFORE=0
```


## JSearch Pro-plan observability and safety controls

Use one page for the complete daily 118-role catalog. The live three-page test
showed that page depth consumes materially more RapidAPI request units.

```env
NUM_PAGES=1
JSEARCH_MAX_QUERIES_PER_RUN=0
JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN=150
JSEARCH_STOP_ON_LOW_QUOTA=1
JSEARCH_MIN_REMAINING_REQUESTS=500
JSEARCH_REMOTE_JOBS_ONLY=1
JSEARCH_REMOTE_QUERY_BIAS=1
JSEARCH_ADAPTIVE_DEEPENING=1
JSEARCH_MAX_EXTRA_PAGES_PER_ROLE=1
JSEARCH_ADAPTIVE_MAX_EXTRA_QUERIES=32
JSEARCH_ADAPTIVE_MIN_PREFILTER_VIABLE=1
JSEARCH_ADAPTIVE_BUCKET_BALANCING=1
JSEARCH_ADAPTIVE_LOOKBACK=1
JSEARCH_ADAPTIVE_LOOKBACK_DATE_POSTED=week
JSEARCH_ADAPTIVE_LOOKBACK_MAX_QUERIES=16
JSEARCH_TARGET_PREFILTER_VIABLE=60
MAX_ROLE_FAILURE_RATE=0.10
```

`JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN` is a preflight guard. With 118 roles and
`NUM_PAGES=1`, the estimate is 118 units. With `NUM_PAGES=3`, the estimate is
354 units and the run is blocked before the first request unless the budget is
explicitly raised for a supervised diagnostic.

Hard monthly/subscription quota errors fail fast and do not retry the remaining
catalog. `run_scrape_test.py` still records quota headers, runtime, raw results,
and selected results when the API provides them.


The lookback does not weaken quality rules and does not set `NUM_PAGES=2`. It
reserves at most 16 units inside the existing 150-unit run budget, uses a
one-week window only for a diversified subset of the strongest role queries,
and stops as soon as 60 jobs have survived every zero-credit pre-enrichment
gate. These variables are defaults in code, so deployment does not depend on
adding them immediately; setting them explicitly in Railway makes the operating
policy visible.

The remote-only request and remote query bias prevent onsite inventory from
consuming most of the daily budget. Adaptive page-2 calls now require at least
one new job that survives the zero-credit Step 2 gates, and they are distributed
across role buckets before any bucket receives overflow. Keep `NUM_PAGES=1`; the
extra depth is controlled by the adaptive budget rather than three pages for all
118 roles.

To make the 30-lead target feasible at realistic contactability rates, use:

```env
TARGET_REVIEWABLE_LEADS_PER_RUN=30
MAX_ELIGIBLE_COMPANIES_PER_RUN=90
```


## Paid-test quality recovery gates

The wider 118-role catalog must preserve the original paid-test standard. Add
these variables to the **GTM** Railway service only:

```env
REQUIRE_FULL_TIME_ROLES=1
REJECT_NON_ACTIVE_HIRING_SIGNALS=1
REQUIRE_EXPLICIT_US_REMOTE_SCOPE=1
FOUNDER_FALLBACK_MAX_EMPLOYEES=99
AIRTABLE_SUPPRESS_EXISTING_COMPANY=1
```

These gates reject explicit part-time/contract/freelance roles, future-opening
or talent-pool posts, and generic `Anywhere` records that have no independent US
hiring evidence. Founder/CEO fallback is limited to companies with at most 99
employees, and Airtable suppresses an account already present under another
contact or role bucket when its lifecycle is still active. Rejected and Error
records may re-enter. FINAL_PASS v0.5 adds the integrity columns documented above.


## FINAL_PASS v0.5 operational hardening

Add these variables to the Railway service before restoring the production start command:

```env
VALIDATION_SIGNING_KEY=<long random secret, at least 32 characters>
JOB_SOURCE_MAX_ACTIVE_AGE_DAYS=45
REROUTE_TEMPORARY_TTL_HOURS=12
REROUTE_PERMANENT_TTL_DAYS=30
REQUIRE_CURRENT_EMPLOYMENT_EVIDENCE=1
REQUIRE_CONTACT_LINKEDIN=1
REQUIRE_US_CONTACT_TERRITORY=1
APPROVED_REVALIDATION_MAX_AGE_HOURS=24
APPROVED_REVALIDATE_JOB_SOURCE=1
SLA_REQUIRE_NET_NEW_AIRTABLE=1
RECOVERABLE_JOB_TTL_DAYS=7
RECOVERABLE_JOB_MAX_ATTEMPTS=5
FINAL_PASS_INVENTORY_TTL_DAYS=7
```

Generate `VALIDATION_SIGNING_KEY` locally and store it only in Railway. Do not commit it or paste it into chat. A suitable PowerShell command is:

```powershell
[Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLower()
```

The Airtable table now requires three additional integrity fields: `Apollo Person ID`, `Validated At`, and `Validation Fingerprint`. It also validates all fields listed in `AIRTABLE_SETUP.md` during `python validate_setup.py --live`.

For exhaustive top-up, `0` means no arbitrary local cap; the search still terminates when the finite strategy space is exhausted and persists checkpoints between interrupted runs:

```env
JSEARCH_MAX_QUERIES_PER_RUN=0
JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN=0
FINAL_PASS_MAX_TOPUP_ITERATIONS=0
FINAL_PASS_MAX_RUNTIME_SECONDS=0
MAX_ELIGIBLE_COMPANIES_PER_RUN=0
```

This does not guarantee that the market contains 30 valid new companies every day. It guarantees that an SLA miss is reported separately from technical success, recoverable candidates are retained, and a single empty microbatch cannot masquerade as full inventory exhaustion.
## FINAL_PASS v0.5 production hardening

Add these variables before deploying v0.5. Generate a unique signing secret; do not use the placeholder from `.env.example`.

```env
VALIDATION_VERSION=tgtc-final-pass-v0.5
VALIDATION_SIGNING_KEY=<unique random secret of at least 32 characters>
TARGET_FINAL_PASS_LEADS_PER_RUN=30
JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN=0
MAX_ELIGIBLE_COMPANIES_PER_RUN=0
FINAL_PASS_MICROBATCH_QUERY_UNITS=60
FINAL_PASS_MAX_TOPUP_ITERATIONS=500
FINAL_PASS_MAX_RUNTIME_SECONDS=10800
FINAL_PASS_MAX_EMPTY_QUERY_CYCLES=2
JOB_SOURCE_MAX_ACTIVE_AGE_DAYS=45
REQUIRE_CURRENT_EMPLOYMENT_EVIDENCE=1
REQUIRE_CONTACT_LINKEDIN=1
REQUIRE_US_CONTACT_TERRITORY=1
REROUTE_TEMPORARY_TTL_HOURS=12
REROUTE_PERMANENT_TTL_DAYS=30
APPROVED_REVALIDATION_MAX_AGE_HOURS=24
APPROVED_REVALIDATE_JOB_SOURCE=1
SLA_REQUIRE_NET_NEW_AIRTABLE=1
RECOVERABLE_JOB_TTL_DAYS=7
RECOVERABLE_JOB_MAX_ATTEMPTS=5
FINAL_PASS_INVENTORY_TTL_DAYS=7
```

`0` for the JSearch and eligible-company caps means the pipeline is controlled by quota visibility, the finite search-plan state, the 500-iteration guard, and the three-hour runtime guard rather than the old arbitrary 370-unit/90-company ceiling. A missed target exits with code `2` and retains the checkpoint instead of reporting business success.

Create these Airtable fields before the first v0.5 run: `Apollo Person ID` (single line text), `Validated At` (date with time), and `Validation Fingerprint` (single line text).
