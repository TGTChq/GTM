# RESUME — exhaustive nine release

Branch `release/exhaustive-nine-v1`, pushed. Base `be3af32` (previous deployed).
Flag `TGTC_EXHAUSTIVE_NINE_CAMPAIGNS=1` on the core canary service.

## Done

1. Minimal release built on `be3af32` (73 files changed against the deployed
   commit); audit artifacts stripped; dependency closure documented in
   `PRODUCTION_RELEASE_REPORT.md`.
2. `tests_core` **1,664 passed / 0 failed**. Legacy `tests` 3,751 passed, 3
   failed — `test_offline_network_guard.py`, reproduced identically against the
   baseline. An environment failure; the guard was NOT weakened.
3. Nine Challenger campaigns verified live: names and active status.
4. Outbox reconciled: 86 pending Instantly rows, zero duplicates, all verified
   emails — and **all pointing at retired Control campaigns**. The two code
   paths that would have sent them there are closed.
5. Migrations applied in production: `schema_version 11`.
6. Deployed and measured three times. Two real defects were found from the
   measurements rather than from reading code, and fixed.

## Deployments, in order, all to the core canary service

| what it tested | outcome |
|---|---|
| first canary | 98.8% of jobs assigned, against 14.3% before; exposed the Operations/physical defect |
| physical-title hard exclusion + qualify-stage gate | excluded rows rose from 12 to 340 |
| professional-taxonomy acquisition + discovery slot | pending |

Deployment ids are recorded in `PRODUCTION_CHANGE_LEDGER.md`.

## Next

7. Watch the qualify → contact discovery → delivery stages of the current run.
8. Confirm each written contact entered exactly one Challenger campaign once.
9. Restore the daily 24-hour schedule. `cronSchedule` is currently None on every
   service, which is an independent reason production produced nothing.
10. Confirm the next scheduled run exists.

## Known blockers

These are permission-classifier refusals, not policy decisions:

* stopping a running deployment;
* the `serviceInstanceUpdate` mutation, so the start command cannot be changed
  from `run-target --target 1000` to a smoke command, and the cron cannot be
  set from here;
* any production database WRITE.

Consequence: a canary already in flight can only be superseded by deploying
over it, which is what was done.
