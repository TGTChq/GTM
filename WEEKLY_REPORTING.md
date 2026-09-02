# Weekly pipeline reporting

A read-only reporting layer that answers, for one precise week: how many jobs the
pipeline captured, how many it reviewed, the review rate, how many qualified, how
many contacts it found, how many leads reached Instantly, where the largest
measurable loss was, and what to do next.

It emits two artifacts per window:

| Output | Path | Purpose |
|---|---|---|
| JSON document | `<out-dir>/weekly_report_<ISO week>.json` | machine-readable; the future dashboard's data contract |
| Text summary | `<out-dir>/weekly_report_<ISO week>.txt` | the page Brett reads; also printed to stdout (Railway logs) |

```
python -u run_weekly_report.py --artifact-root /app/data/state/orchestrator_v2
```

## Safety

The layer performs **no writes anywhere**. It reads run artifacts from disk and,
only when explicitly asked, performs listing reads (`POST /leads/list`,
`GET records`). It cannot enroll a lead, change a campaign, patch an Airtable row,
or send a message. Every document records `provenance.writes_performed: none`.

`--strict` is the only flag that changes the exit status. Without it the job always
exits 0, so chaining the report onto a pipeline run can never fail that run.

## The reporting window

Windows are defined in **America/Los_Angeles wall-clock time**, not UTC, and the
offset is resolved per instant — never hardcoded. The same local midnight is
`07:00Z` in PDT and `08:00Z` in PST, and the report says which resolver produced
the boundary (`timezone_source`).

* Default: the most recently **closed** Friday→Friday week. Running at 06:00
  Pacific on a Friday reports the week that just ended and never reaches into the
  day it is written.
* Windows are half-open `[start, end)`, so consecutive weeks never double-count a
  run.
* A week containing a DST transition really is 167 or 169 hours long, and
  `reporting_window_duration_hours` says so.
* `zoneinfo` + `tzdata` is authoritative. If no IANA database exists, a codified
  US federal DST rule takes over and the report declares
  `timezone_source: builtin:us_federal_dst_rule`.

Every document carries `reporting_window_start`, `reporting_window_end`,
`generated_at` and `included_run_ids`.

## How work is attributed to a week

A run is placed in a week by **when the run happened** —
`run_manifest.finished_at`, falling back to `started_at`, then to the timestamp in
the run id. The field actually used is recorded per run.

**A job's `posted_at` is never used.** A backlog of month-old postings processed on
Tuesday is Tuesday's throughput, not last month's.

Two exclusions are applied and both are declared in the document:

* **Dry runs.** `full_dry_run` writes the same artifact shape as a live run — the
  2026-08-05 corpus in `data/orchestrator` contains a dry run reporting
  `delivery.enrolled = 20` against a *synthetic* lane with `allow_network: false`.
  Counting it would report manufactured throughput as a business result. Runs are
  classified `simulated` by mode marker, `allow_network=false`, or an
  all-synthetic lane set, and land in `provenance.excluded_simulated_runs`.
  `--include-simulated` overrides this for local debugging only.
* **Undateable runs.** Listed in `provenance.unattributable_run_ids` rather than
  silently dropped.

## Where each metric comes from

| Metric | Authoritative source | Timestamp that attributes it |
|---|---|---|
| `jobs_captured` | `waterfall.json:unit_totals.postings` (→ `capacity_report.json:raw_postings`) | run completion |
| `jobs_reviewed` | `orchestrator_result.json:enrichment.funnel.qualification_input` | run completion |
| `review_rate_pct` | derived: reviewed ÷ captured | — |
| `qualified_opportunities` | `enrichment.funnel.target_role_eligible` (→ `icp_eligible_companies`) | run completion |
| `contacts_found` | `waterfall.json:unit_totals.contacts` | run completion |
| `sent_to_airtable` | `delivery.json:created` | run completion |
| `sent_to_instantly` | **Instantly** `lead.timestamp_created` (`--instantly`) | the lead's own creation instant |

The first four match `run_orchestrator.py`'s "Brett's daily metrics" block
line-for-line, so the weekly totals and the daily Railway logs cannot disagree.

### Aggregation rules

* **Silence is never zero.** A run that does not carry a counter is listed in
  `runs_missing_field` and the metric drops to `partial`; "the pipeline processed
  nothing" and "the artifact lacks this counter" are different facts.
* A metric no run reported is `unavailable` **with a reason**. It is never guessed
  and never coerced to 0.
* Every metric carries `source`, `evidence` (the exact field read), `attribution`
  (the timestamp that placed it in the window) and `contributing_run_ids`.

### `sent_to_instantly` needs `--instantly`

This is the one headline metric that **cannot** be reconstructed from run
artifacts in the current production topology:

* Enrollment is performed by a *different* Railway service (`run_approved.py` on
  **GTM Approved Sync**). That service writes no run artifact and has no volume —
  its `SENT_TO_INSTANTLY` log line does not survive the container.
* Airtable's `Status = Enrolled` is not a substitute: the base stores no
  enrolled-at timestamp, so a status column cannot be attributed to a week.
* Instantly does carry the fact, on the lead: `timestamp_created` is the instant
  the lead entered the workspace. Because Instantly answers `200` for an address
  already in the workspace, an accepted API call is not a delivery — a new
  `timestamp_created` is.

So: run with `--instantly` (needs `INSTANTLY_API_KEY` plus at least one
`INSTANTLY_CAMPAIGN_*` id) and the metric is measured. Without it the report says
`not measured` and names this reason. If a run was itself permitted to enroll
(`policy.allow_instantly_enrollment: true`), `delivery.json:enrolled` is used
instead and labelled as covering only that run.

`--airtable` adds an independent cross-check of `sent_to_airtable`, counting rows
by Airtable's own `createdTime`. `Status` is reported as a *current-value snapshot*
of those rows, never as "N rows were enrolled during the window".

## Bottleneck and action plan

The bottleneck is measured, not narrated: the funnel boundary that lost the most
records **among the stages the report could actually measure**, annotated with the
loss reason codes the orchestrator itself recorded. A failed acquisition lane
outranks any funnel boundary, because downstream counts understate capability when
acquisition broke. A window with no runs reports `no_pipeline_activity` — the
bottleneck is execution, not a funnel stage.

Actions are a fixed, auditable mapping from the identified stage and its top reason
codes to concrete work, each naming the evidence that produced it. Nothing is
invented.

## Reuse by the future dashboard

The JSON is versioned (`schema: tgtc-weekly-report/1`) and is not weekly-shaped:

* `--start` / `--end` (or `--weeks N`, `--boundary-day`, `--boundary-hour`) produce
  any window on the same code path;
* `runs[]` carries one row per run with every per-run counter and the field each
  came from — a ready time series;
* `daily[]` buckets the same counters by Pacific calendar day;
* `metrics{}` is typed and self-describing, so a dashboard can render a metric it
  has never seen, including its `unavailable` state and reason.

## Local use

```bash
# The last closed Pacific week, from the local artifact root; print only.
python -u run_weekly_report.py --no-write

# A pinned window over a specific root.
python -u run_weekly_report.py --artifact-root data/orchestrator_smoke \
  --start 2026-08-14 --end 2026-08-21 --out-dir reports/weekly

# Include the Instantly delivery count (listing reads only).
python -u run_weekly_report.py --instantly --airtable
```

Tests: `python -m pytest tests/test_weekly_report.py -q`.

---

# Deployment (requires approval — nothing below has been applied)

## The binding constraint

Run artifacts live on **`gtm-volume`**, mounted at `/app/data/state` on the **GTM**
service, and per `PRODUCTION_DEPLOYMENT.md` they are "only reachable from a
**running** GTM container". A Railway volume attaches to one service. **A separate
report service therefore cannot read run artifacts**, and would be limited to the
Instantly and Airtable metrics.

That constraint, not the schedule, decides the deployment shape.

## Option A — chain onto the GTM daily cron (recommended)

GTM already runs daily, on the volume that holds the artifacts, at a time that is
before 08:00 Pacific all year. `--if-due friday` makes the report a no-op on the
other six days.

1. In the Railway UI, open **GTM → Settings → Start Command** and record the
   current value (the acquisition command in `PRODUCTION_DEPLOYMENT.md`).
2. Replace it with the same command wrapped so the report runs after it and cannot
   change the pipeline's exit status:

   ```sh
   sh -c 'python -u run_orchestrator.py --mode live_acquisition_and_enrichment --lanes ats,jsearch,free_feeds --target 300 --airtable-write --global-budget 1500 --ats-lane-budget 1200 --reserved-non-ats 200 --board-budget 120 --provider-budget 300 --artifact-root /app/data/state/orchestrator_v2; rc=$?; python -u run_weekly_report.py --artifact-root /app/data/state/orchestrator_v2 --if-due friday --instantly || true; exit $rc'
   ```

   *Paste the recorded acquisition command verbatim; the one above is the
   documented value and must be re-checked against the live field.*
3. Leave **GTM → Cron** unchanged. This repo documents `0 14 * * *`;
   **verify the live value in the Railway UI**, because both the cron and the
   start command are service-managed, not config-as-code.
4. `INSTANTLY_API_KEY` and the `INSTANTLY_CAMPAIGN_*` ids must be present on
   **GTM** for `--instantly` to work. They are currently set on **GTM Approved
   Sync** — per `railway-per-service-config-drift`, variables are per service, so
   confirm each one on GTM before relying on the metric. Drop `--instantly` if you
   would rather not add the key there; the report then declares
   `sent_to_instantly` as unavailable rather than guessing.

**Timing.** Railway cron is UTC (`DEPLOY_RAILWAY_STEP_BY_STEP.md:13`) and has no
timezone setting, so the schedule must be safe under both offsets. `0 14 * * *` is
07:00 PDT and 06:00 PST — before the 08:00 Pacific meeting year-round, with no
schedule change at either DST transition. The report's own Pacific window is
computed from the IANA database, so the week boundary stays correct regardless.

**Output.** The summary prints to the GTM deploy logs, and both files are written
to `/app/data/state/orchestrator_v2/weekly_reports/`, where each week accumulates
for the dashboard.

## Option B — a separate "GTM Weekly Report" service

Only if the report must run independently of acquisition. Accept the loss of
funnel metrics, or pair it with Option C.

| Setting | Value |
|---|---|
| Start Command | `python -u run_weekly_report.py --instantly --airtable --out-dir /app/data/weekly_reports` |
| Cron | `0 13 * * 5` — 06:00 PDT / 05:00 PST Friday, before 08:00 Pacific year-round |
| Restart policy | NEVER |
| Volume | none available (`gtm-volume` belongs to GTM) |
| Variables | `INSTANTLY_API_KEY`, `INSTANTLY_CAMPAIGN_*`, `AIRTABLE_TOKEN`, `AIRTABLE_BASE_ID`, `AIRTABLE_TABLE_NAME` |

Without the volume, `jobs_captured`, `jobs_reviewed`, `review_rate_pct`,
`qualified_opportunities` and `contacts_found` are all reported as **unavailable**.
Only `sent_to_instantly` and the Airtable cross-check are measurable. The report
will say so plainly — it will not fill the gap with zeros.

## Option C — publish a metrics ledger (future work, for the dashboard)

To make the funnel readable from any service, GTM would append one compact metrics
row per run to a store other services can read. The per-run rows this report
already emits (`runs[]`) are the natural payload. Not implemented; it is a code
change to the pipeline, not a deployment step.

## Gates that need explicit authorization

None of these has been performed:

* changing the GTM Start Command (Option A step 2);
* creating a service or setting any cron (Option B);
* adding `INSTANTLY_API_KEY` / campaign ids to the GTM service;
* deploying or redeploying any Railway service;
* any Airtable or Instantly write — the layer has no write path at all.
