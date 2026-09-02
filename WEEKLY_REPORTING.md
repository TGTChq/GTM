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

## Idempotence

Output paths are derived from the ISO week, so the same window always resolves to
the same pair of files. `--max-seconds N` additionally bounds provider reads in
wall-clock terms; on expiry the Instantly count is reported as a declared floor
(`campaigns_skipped_out_of_time`) instead of running long. `--once-per-window` turns a repeat invocation into a true
no-op: the check runs *before* any provider read, so a container restart or a
second cron firing on the same day neither re-scans Instantly nor rewrites the
report. `--force` overrides it. Files are written via temp file + `os.replace`, so
a crash can never leave a half-written document for a dashboard to parse.

---

# Deployment (requires approval — nothing below has been applied)

## Verified live state (Railway, read-only, 2026-09-01)

Project `tgtc-daily-pipeline` · environment `production` · workspace "My Projects".

| | GTM | GTM Approved Sync |
|---|---|---|
| Deployed commit | `a098ec4` | `a098ec4` |
| Cron | **`0 13 * * *`** | **`0 0 * * *`** |
| Start Command | `python -u run_orchestrator.py --mode live_acquisition_and_enrichment --lanes fantastic --target 300 --airtable-write --global-budget 1500 --artifact-root /app/data/state/orchestrator_v2` | `python -u run_approved.py` |
| Volume | `gtm-volume` → `/app/data/state` (5.3 / 19.5 GB) | none |
| `PIPELINE_ARTIFACT_ROOT` | `/app/data/state/orchestrator_v2` | — |
| `INSTANTLY_API_KEY` | **already set** | already set |
| `INSTANTLY_CAMPAIGN_*` | **all 10 buckets set** (36-char ids) | all 10 set |
| Region / restart | sfo / container exits after the cron run | — |

**Three of these contradict the older runbooks in this repo, which are stale:**

* GTM cron is `0 13 * * *`, not the `0 14 * * *` in
  `DEPLOY_RAILWAY_STEP_BY_STEP.md` / `PRODUCTION_HANDOFF_CHECKLIST.md`.
* GTM's live lane set is `--lanes fantastic` with no ATS budget flags, not the
  `--lanes ats,jsearch,free_feeds …` in `PRODUCTION_DEPLOYMENT.md`.
* Approved Sync runs daily at `0 0 * * *`, not `*/5 * * * *`.

Always read the live value before quoting a start command.

**Consequence for this feature: no secret needs to be added.** GTM already holds
`INSTANTLY_API_KEY` and all ten campaign ids, so `--instantly` works on GTM today.

## The binding constraint

Run artifacts live on `gtm-volume`, and a Railway volume attaches to exactly one
service. A separate report service therefore **cannot** read run artifacts and
would be limited to the Instantly and Airtable metrics. That constraint, not the
schedule, decides the architecture.

## Recommended: report first, then `exec` the pipeline (Option A)

`0 13 * * *` UTC is **06:00 PDT / 05:00 PST** — before 08:00 Pacific year-round,
with no schedule change at either DST transition. Railway cron is UTC only.

Change **GTM → Settings → Start Command** to:

```sh
sh -c 'python -u run_weekly_report.py --artifact-root /app/data/state/orchestrator_v2 --if-due friday --once-per-window --instantly --max-seconds 480 || true; exec python -u run_orchestrator.py --mode live_acquisition_and_enrichment --lanes fantastic --target 300 --airtable-write --global-budget 1500 --artifact-root /app/data/state/orchestrator_v2'
```

Cron unchanged. Nothing else changes.

Why the report runs **before** the pipeline, not after:

* **Delivery time is guaranteed.** The report finishes within minutes of 05:00/06:00
  Pacific regardless of how long acquisition takes. Chaining it after the pipeline
  would put the report's completion at the mercy of run duration, and a long run
  could push it past Brett's 08:00 meeting.
* **The data is identical either way.** The reported window ends Friday 00:00
  Pacific. Friday's own run starts at 05:00/06:00 Pacific, *after* that boundary, so
  it belongs to next week's report by construction and can never be part of this
  one. Running first loses nothing.
* **`exec` preserves exit semantics exactly.** The orchestrator replaces the shell
  process, so the container's exit code *is* the pipeline's exit code — byte-for-byte
  today's behaviour. No `rc=$?` bookkeeping, no wrapper swallowing a failure.
* **The report cannot break the pipeline.** `--max-seconds 480` bounds provider
  reads *in process* (no dependency on a `timeout` binary in the image), `|| true`
  absorbs any non-zero exit, and neither can reach the `exec`. Verified locally:
  with report exit codes 0/1/137 and pipeline exit codes 0/7/42, the wrapper's exit
  code is always the pipeline's.
* **`--once-per-window` bounds repeats.** A redeploy or second firing on the same
  Friday exits immediately without touching Instantly.

Trade-off worth stating: because the report runs first, a Friday report reflects
runs through **Thursday**. That is exactly the closed week, so it is correct — but
it does mean the report never contains the run happening in the same container.

## Alternative: separate service (Option B)

Only if the report must run independently of acquisition.

| Setting | Value |
|---|---|
| Start Command | `python -u run_weekly_report.py --instantly --airtable --out-dir /app/data/weekly_reports` |
| Cron | `0 13 * * 5` (06:00 PDT / 05:00 PST Friday) |
| Volume | none available — `gtm-volume` belongs to GTM |

Without the volume, `jobs_captured`, `jobs_reviewed`, `review_rate_pct`,
`qualified_opportunities` and `contacts_found` are **unavailable**. Only
`sent_to_instantly` and the Airtable cross-check are measurable. The report says so
plainly rather than filling the gap with zeros. Not recommended.

## Instantly campaigns included

`configured_campaign_ids()` reads every `INSTANTLY_CAMPAIGN_*` name in
`config.CAMPAIGN_ENV_BY_BUCKET`, plus `_SMALL`/`_MID`/`_LARGE` band variants where
set, plus the default `INSTANTLY_CAMPAIGN_ID`. Against the live GTM environment
that resolves to exactly the ten role-bucket campaigns:

```
INSTANTLY_CAMPAIGN_CUSTOMER_SUCCESS   INSTANTLY_CAMPAIGN_MARKETING
INSTANTLY_CAMPAIGN_CUSTOMER_SUPPORT   INSTANTLY_CAMPAIGN_OPERATIONS
INSTANTLY_CAMPAIGN_ECOMMERCE          INSTANTLY_CAMPAIGN_PEOPLE_HR
INSTANTLY_CAMPAIGN_ENGINEERING        INSTANTLY_CAMPAIGN_PRODUCT
INSTANTLY_CAMPAIGN_FINANCE            INSTANTLY_CAMPAIGN_GTM
```

No band variants are set, and `INSTANTLY_CAMPAIGN_ID` is present but **empty**, so
it contributes nothing — matching `config.resolve_campaign_id`, which falls back to
that empty default only when a bucket has no campaign. Every campaign the pipeline
can route to is therefore covered.

If Outbound Wave 1 is ever enabled, its challenger campaigns are *not* in
`CAMPAIGN_ENV_BY_BUCKET` and would need `--campaign-id` (or an env addition) to be
counted. No Wave 1 variables exist on GTM today.

Reads are `POST /leads/list` with the **singular** `campaign` filter — the plural
`campaign_ids` filter is ignored by the API and must never be relied on. Leads are
counted by `timestamp_created`; a campaign that fails or hits the 200-page ceiling
is named in `campaigns_failed` / `campaigns_truncated`, contributes nothing
silently, and drops the metric to `partial`.

## Gates that still need explicit authorization

* changing the GTM Start Command (the only production change required);
* deploying / redeploying GTM.

Not required and not proposed: cron changes, secret changes, new services, Airtable
or Instantly writes, external sends.
