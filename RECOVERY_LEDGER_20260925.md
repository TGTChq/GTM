# Recovery ledger -- 2026-09-25

Persistent record of every change, execution, spend and receipt in this piece of work, so
it can be resumed without repeating anything. Append-only: entries are never rewritten,
only corrected by a later entry that says so.

## STATE RIGHT NOW

| thing | value |
|---|---|
| **CORE CRON** | **PAUSED (`null`) since 2026-09-25 02:14Z -- MUST BE RESTORED TO `0 3 * * *`** |
| core deployment in force | `56df036e-4d08-4ced-b296-ae00b66757ab`, commit `1633b6ce6998`, restart NEVER |
| run lock (advisory `1952937059`) | free |
| last completed run | `20260924T055100.714673Z-b60f0376`, ended 2026-09-24 05:52:24Z |
| 2026-09-25 03:00Z scheduled run | **did not run -- deliberately paused before the tick** |
| Weekly Report cron | untouched (`0,20,40 13-20 * * *`) |
| Replies cron | untouched (`15 * * * *`) |

## Entries

### 1. 2026-09-25 02:10Z -- preflight, before touching anything
Read: time 02:10:34Z, run lock **0**, no `daily/start` after 02:55Z of the prior day, last
`daily/end` 2026-09-24 05:52:24Z. Effective core deployment `56df036e-4d08-4ced-b296-ae00b66757ab`
(SUCCESS, commit `1633b6ce6998`, `cronSchedule: 0 3 * * *`, `restartPolicyType: NEVER`).
The 03:00Z run had **not** started, so the pause path applies rather than the watch path.

Full start command and manifest saved to the session scratchpad as
`core_deploy_before_pause.json`. The start command, reproduced here so it can be restored
from this file alone:

```
sh -ec 'KIND=${TGTC_RUN_KIND:-}; if [ -z "$KIND" ]; then H=$(date -u +%H); if [ "$H" -ge 3 ] && [ "$H" -le 5 ]; then KIND=scheduled; else KIND=manual; fi; fi; BID=$(python -m tgtc_core budget-id --kind $KIND); echo "run kind=$KIND budget=$BID"; python -u -m tgtc_core migrate && python -u -m tgtc_core budget --budget-id $BID --budget-kind $KIND --expires-hours 24 --fantastic-requests 60 --fantastic-credits 4000 --apollo-requests 10000 --apollo-credits 1600 --anthropic-requests 1500 --anthropic-input-tokens 14000000 --anthropic-output-tokens 1600000 && python -u -m tgtc_core run-daily --budget-id $BID --budget-kind $KIND --target 1000 --block-pages 2 --max-rounds 300 --max-items 10000 --i-understand-spend'
```

`TGTC_RUN_KIND` is already `scheduled` on the service, so the hour-of-day branch above is
dead in production and a run started at any hour is still `scheduled`.

### 2. 2026-09-25 02:14Z -- core cron paused
`serviceInstanceUpdate(serviceId: f83cd97a…, environmentId: bae427bd…, input: {cronSchedule: null})`
returned `true`. **`true` is not proof**, so it was read back:
`serviceInstance.cronSchedule` is now `None`, `numReplicas` 1, `restartPolicyType` NEVER,
and the start command is preserved (713 chars).

The *deployment* manifest still prints `cron= 0 3 * * *`, because that manifest is a
snapshot taken when the deployment was created, not live config. The service instance is
what the scheduler reads -- `PRODUCTION_CHANGE_LEDGER.md` row 2 established that when it
read `cronSchedule: None` on all six services and nothing was in fact scheduled anywhere.
No redeploy was forced to make the two agree: with `cronSchedule` null a redeploy might
not be `buildOnly`, which could start the container and run a full production job on old
code. Not worth it 45 minutes before a tick. The pause is confirmed instead by the service
instance reading plus watching 03:00Z pass with no `daily/start`.

Only GTM Core Canary 1000 was touched. Weekly Report and Replies were not.

**Rollback for this entry:** same mutation with `cronSchedule: "0 3 * * *"`.
