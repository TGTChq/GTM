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
