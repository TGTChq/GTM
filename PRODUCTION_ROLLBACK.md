# Rollback — exhaustive nine-campaign release

Service `GTM Core Canary 1000` (`f83cd97a-135d-48e3-8e12-d517a51edfff`) ·
project `tgtc-daily-pipeline` (`898f2e3a-1c1e-4b00-b9a6-686cf0432282`) ·
environment `production` (`bae427bd-64a6-4f4e-8f56-fbd406985434`).

| | commit | tag |
|---|---|---|
| current production | `6c79aa1` | `release-exhaustive-nine-6c79aa1` |
| previous production | `be3af32` | `pre-exhaustive-nine-be3af32` |

Deployment of record: `9bc1ba57-31ec-42bb-ab8b-12c7f5cdf574` (GitHub,
`feat/rebuild-core` at `6c79aa1`).

## Level 1 — disable the scope policy (no deploy)

```
railway variables -s "GTM Core Canary 1000" --set TGTC_EXHAUSTIVE_NINE_CAMPAIGNS=0
```

Restores `tgtc-core/2` classification, the previous acquisition behaviour, the
previous policy version and the flag-gated guards (physical-title exclusion,
retired-Control-campaign refusal, compliance recheck). `tests_core` asserts the
flag-off classification path is byte-identical to `tgtc-core/2`.

It deliberately does NOT undo these production fixes, which are not scope
policy and are correct in both modes:

* exit semantics (a run below target exits 0);
* `run-target` drains the Instantly channel, not only Airtable;
* the contact country resolved from the person's own stored evidence;
* budget-wait release and draining bought inventory after acquisition stops;
* the per-UTC-day delivery ceiling (governed by its own variables).

## Level 2 — stop the schedule

```
railway api 'mutation { serviceInstanceUpdate(serviceId: "f83cd97a-135d-48e3-8e12-d517a51edfff", environmentId: "bae427bd-64a6-4f4e-8f56-fbd406985434", input: { cronSchedule: null }) }'
```

## Level 3 — stop Instantly writes only

```
railway variables -s "GTM Core Canary 1000" --set TGTC_DELIVERY_MAX_TOTAL=0
```

Every Instantly write is then deferred (never blocked or failed), so nothing is
lost and the rows drain once the ceiling is raised again.

## Level 4 — previous code

Redeploy `be3af32`: Railway UI → the service → Deployments → roll back to a
`be3af32` deployment, or push the tag to the watched branch (a non-fast-forward,
so it needs an explicit force):

```
git push --force origin pre-exhaustive-nine-be3af32^{commit}:refs/heads/feat/rebuild-core
```

Then restore the previous start command, recorded in
`PRODUCTION_CHANGE_LEDGER.md` ("Service instance" table).

## Migrations

Additive, nullable, no backfill, idempotent; `schema.sql` mirrors them. Old
code ignores the added columns. No down-migration is required and none exists.

## What no rollback recovers

Contacts already enrolled in Instantly stay enrolled. The per-day ceiling
(150 per campaign, 1,100 in total) bounds that exposure.
