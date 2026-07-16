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
