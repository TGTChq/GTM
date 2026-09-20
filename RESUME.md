# RESUME — where this release stands

Branch: `release/exhaustive-nine-v1` (local). Base: `be3af32` (deployed).
Flag: `TGTC_EXHAUSTIVE_NINE_CAMPAIGNS`.

## Done
1. Minimal production release committed locally; audit artifacts stripped.
2. `tests_core` 1,579 passed / 0 failed. Legacy `tests` 3,751 passed, 3 failed
   (pre-existing network-guard, reproduced against the baseline).
3. Challenger routing proved offline: 10 keys -> 9 ids, no Control id.
4. Railway production state diagnosed (see the change ledger).

## Next
5. Push the release branch.
6. Diagnose the CRASHED `be3af32` deployment before deploying over it.
7. Restore `INSTANTLY_CAMPAIGN_*` on the core service using CHALLENGER ids.
8. Deploy to `GTM Core Canary 1000` with the cron still absent.
9. Smoke: start, DB, migrations, provider clients, worker, ledger.
10. Live Instantly campaign comparison.
11. Outbox reconciliation (integers, no PII).
12. Bounded canary: <=10 per campaign, <=90 total.
13. Promote, restore the daily schedule, confirm the next run.
