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
MAX_ROLE_FAILURE_RATE=0.10
```

`JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN` is a preflight guard. With 118 roles and
`NUM_PAGES=1`, the estimate is 118 units. With `NUM_PAGES=3`, the estimate is
354 units and the run is blocked before the first request unless the budget is
explicitly raised for a supervised diagnostic.

Hard monthly/subscription quota errors fail fast and do not retry the remaining
catalog. `run_scrape_test.py` still records quota headers, runtime, raw results,
and selected results when the API provides them.

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
records may re-enter. No new Airtable columns are required.
