# Incident 2026-09-06 — Apollo lead credits exhausted; paid acquisition paused

## What happened

Run `20260906T030230Z-2f74ac7c` (commit `83fa993737`) bought 5,444 provider rows,
kept 226 net-new postings, qualified them (194 role-eligible), and then Apollo
refused on the **first** company:

```
GET https://api.apollo.io/api/v1/organizations/enrich  ->  HTTP 422
error_details.code    BILLING.LIMIT.CREDITS_EXHAUSTED
error_details.message Your team has used all of its credits for this billing cycle.
context.credit_type       lead credits
context.credit_balance    0
context.next_billing_date 2026-09-18
```

Reproduced read-only 2026-09-06T07:26–07:29Z (request ids
`a972a3be-9a9a-44fb-808a-8616b386a1f0`, `4631e508-7193-4e61-80f4-323c61ff95bf`).
Rate limits were healthy (999/1000 per minute) and `auth/health` returned 200, so
this is neither throttling nor an auth problem. Provider-side, not ours.

The circuit opened, nothing was committed to suppression, the watermark stayed in
flight — and the 226 postings were still lost, because the window offsets had
already advanced past them (100 → 2822) and nothing ever hands un-suppressed work
back. That defect is fixed in PR #100 (`pending_work`); this file covers the pause.

## The pause — what was changed

**One variable on the GTM service.** Applied 2026-09-06 ~07:45Z.

| | before | after |
|---|---|---|
| `FANTASTIC_JOBS_ENABLED` (GTM) | `1` | **`0`** |

Nothing else was touched. Recorded here so restoration needs no archaeology:

| setting | value at pause time (unchanged) |
|---|---|
| GTM cron | `0 3 * * *` |
| GTM Approved Sync cron | `0 0 * * *` |
| GTM start command | `sh -c 'python -u run_orchestrator.py --mode live_acquisition_and_enrichment --lanes fantastic --target 300 --airtable-write --global-budget 1500 --artifact-root /app/data/state/orchestrator_v2; rc=$?; python -u run_weekly_report.py --artifact-root /app/data/state/orchestrator_v2 --if-due friday --once-per-window --anchored --require-completed-run --instantly --slack --max-seconds 480 || true; exit $rc'` |

### Why a variable and not the cron

The start command runs the pipeline **and then the weekly report**, in that order,
in the same container. Removing GTM's cron would also stop Friday's report — an
unrelated service being paused by a side effect. Disabling the paid source instead
leaves the cron firing, the report generating, and Approved Sync completely
untouched, while `fantastic_jobs_adapter` returns
`{"enabled": False, "skipped_reason": "disabled"}` before issuing any request.

### Verified after applying

- effective `FANTASTIC_JOBS_ENABLED = False` on GTM (read back from the service)
- GTM cron still `0 3 * * *`; Approved Sync still `0 0 * * *`
- the change **created deployment `6bd5b959`** and **executed nothing**: 0 log
  lines, 0 `RUN SUMMARY`. A variable change triggers a redeploy, not a run.

### Restoration

```bash
railway api 'mutation { variableUpsert(input: {projectId: "898f2e3a-1c1e-4b00-b9a6-686cf0432282", environmentId: "bae427bd-64a6-4f4e-8f56-fbd406985434", serviceId: "3a41d0d7-cd66-4f53-baa6-886266ddbbed", name: "FANTASTIC_JOBS_ENABLED", value: "1"}) }'
```

This pause is temporary and is **not** tied to 2026-09-18. Apollo's
`next_billing_date` is what Apollo reports, not a guarantee: the cycle can roll
over without restoring lead credits if the plan allotment is the binding limit, if
an additional-usage cap is set, or if a payment is failing. Resume on the readiness
check below, not on a date.

## Conditions for safely resuming acquisition

All three, in order:

1. **Apollo answers.** `python acceptance/apollo_readiness.py` returns READY. It
   calls `organizations/enrich` once — the same endpoint the run uses, which costs
   nothing while it is refusing — and reports Apollo's own `credit_balance`.
2. **Custody is deployed.** `orchestrator/pending_work.py` present on the running
   commit (PR #100, `main` ≥ `b173a3c`), so a second billing stop retains the work
   instead of dropping it.
3. **Maintenance mode is cleared.** `MAINTENANCE_ONLY` must be `0` **first**. It was
   set to `1` on 2026-09-06 so the nightly container does a maintenance pass instead
   of a no-op pipeline run. With both set the run exits 2 and acquires nothing every
   night — the guard is fail-safe, so it costs nothing, but it is silent unless the
   exit code is read:

   ```bash
   railway api 'mutation { variableUpsert(input: {projectId: "898f2e3a-1c1e-4b00-b9a6-686cf0432282", environmentId: "bae427bd-64a6-4f4e-8f56-fbd406985434", serviceId: "3a41d0d7-cd66-4f53-baa6-886266ddbbed", name: "MAINTENANCE_ONLY", value: "0"}) }'
   ```

4. **Then** restore `FANTASTIC_JOBS_ENABLED=1` with the command above.

### What the first resumed run should show

`cursor: date_created slices` in the RUN SUMMARY, with drained slices and an
`expired_inventory` line. If the window from 2026-08-30 is still open it will be
re-walked slice by slice for near-zero net-new — one transition cost, explained in
`OPERATIONAL_STATUS.md`. It self-resolves if acquisition stays paused past
2026-09-11T10:02Z.

## Still unverified — needs the Apollo web app

The API key cannot see billing (`billing/summary`, `credits`,
`usage_stats/api_usage_stats` all 404; `users/me` and `teams/current` unavailable).
What Apollo's error body *does* say is `credit_balance: 0` for `lead credits`, and
that it **refuses rather than billing an overage** — so whatever additional-usage
setting exists is not currently covering lead credits. Which control is binding is
not determinable from the API.

Needed, read-only, from **Settings → Plans / Billing** on team
`6a3400f0c0beda0010d2c22c` (key `sha256[:12] 6992a98cc7db`, identical on GTM and
GTM Approved Sync):

1. Is **additional usage / pay-as-you-go** enabled, and does it cover **lead
   credits** (as opposed to export or mobile-number credits)?
2. If enabled, is there a **spend cap**, and has it been reached?
3. Current **lead-credit balance and monthly allotment**, and the consumption for
   this cycle.
4. Any **failed payment or billing hold** on the account.
5. The **cycle renewal date** as billing shows it, to confirm or contradict the
   `2026-09-18` the API reports.
