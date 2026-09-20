# Rollback — exhaustive nine-campaign release

Service: `GTM Core Canary 1000` · Project `tgtc-daily-pipeline`
(`898f2e3a-1c1e-4b00-b9a6-686cf0432282`) · Environment `production`
(`bae427bd-64a6-4f4e-8f56-fbd406985434`).

Previous deployed revision: **`be3af32`** (branch `feat/rebuild-core`).

## Level 1 — disable the flag (seconds, no deploy)

Every behaviour this release adds is gated by one variable. Removing it returns
the classifier, the acquisition profile and the policy version to `tgtc-core/2`
byte-identically; `tests_core` asserts that on the flag-off path.

```
railway variables -s "GTM Core Canary 1000" --set TGTC_EXHAUSTIVE_NINE_CAMPAIGNS=0
```

This does NOT undo: migrations (additive and inert), the restored
`INSTANTLY_CAMPAIGN_*` routes, or the cron. Those are separate levels below.

## Level 2 — stop the schedule

```
railway api 'mutation { serviceInstanceUpdate(serviceId: "<id>", environmentId: "bae427bd-64a6-4f4e-8f56-fbd406985434", input: { cronSchedule: null }) }'
```

## Level 3 — redeploy the previous revision

Roll the service back to the `be3af32` deployment in the Railway UI, or push
`feat/rebuild-core` back to `be3af32`. No data is rewritten by doing so.

## Migrations

Migrations in this release are additive, nullable, no backfill and idempotent;
`schema.sql` mirrors them. Rolling the code back leaves the added columns in
place, unread. There is no destructive down-migration and none is required.

## What a rollback does NOT recover

Contacts already written to Instantly are not withdrawn by any of the levels
above. The canary is bounded to at most ten per campaign (90 total) precisely
so that this exposure stays small and reviewable.
