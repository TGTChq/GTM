# RESUME — exhaustive nine release (production state 2026-09-21)

## Live now

| | |
|---|---|
| production commit | `6c79aa1` (tag `release-exhaustive-nine-6c79aa1`), `feat/rebuild-core` |
| deployment of record | `9bc1ba57-31ec-42bb-ab8b-12c7f5cdf574` — SUCCESS |
| policy | `tgtc-core/3-exhaustive-nine`, `TGTC_EXHAUSTIVE_NINE_CAMPAIGNS=1` |
| schema | 11 (migrate runs first in the start command) |
| cron | `0 3 * * *`, **next `2026-09-22T03:00:00Z`**, `restartPolicyType NEVER` (Railway-reported) |
| budget | prefix `prod-core`, daily id `prod-core-YYYYMMDD`; 3,300 Fantastic / 1,000 Apollo per day |
| Instantly ceiling | 150 per campaign, 1,100 total, per UTC day |
| routing | 10 keys -> 9 Challenger ids, 0 Control ids |
| tests | `tests_core` 1,733 passed / 0 failed; legacy 3 pre-existing network-guard failures only |

## Measured today

610 new Instantly enrolments across all nine Challenger campaigns; 0 Control;
0 duplicates. Full run 320 eligible from 3,300 records under the old
allocation; drain run 319 more from stranded inventory (one-off).

## Open

1. Watch the first scheduled run, 2026-09-22 03:00 UTC: it should start, create
   `prod-core-20260922`, release yesterday's Apollo-deferred work, deliver
   the 155 deferred OPERATIONS contacts, and exit 0.
2. Confirm the Apollo balance before raising the daily grant above 1,000.
3. Decide on the OPERATIONS campaign daily_limit (550) if more than ~150 new
   leads/day should go there.

## Check the first scheduled run

```
railway logs -s "GTM Core Canary 1000" --lines 60
```

In `run_log`, a `target/end` row after 2026-09-22 03:00 UTC with
`result` and `technical_failures`.
