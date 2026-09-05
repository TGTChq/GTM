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

The layer performs **no external writes**. It reads run artifacts from disk and,
only when explicitly asked, performs listing reads (`POST /leads/list`,
`GET records`). It cannot enroll a lead, change a campaign, patch an Airtable row,
or change a pipeline's state. Every document records
`provenance.writes_performed: none`, which refers to *provider* writes.

It does write to its own output directory, and it is worth being precise about
what, because "no writes anywhere" was the earlier claim here and it was not true:

| file | when | purpose |
|---|---|---|
| `weekly_report_<week>.json` / `.txt` / `.slack.txt` | every generated report | the artifacts |
| `weekly_report_anchor.json` | `--anchored` only | where the next window starts |
| `weekly_report_<week>.slack_sent.json` | `--slack`, after Slack accepts | the delivery receipt |

`--no-write` suppresses all of them. `--strict` is the only flag that changes the
exit status; without it the job always exits 0, so chaining the report onto a
pipeline run can never fail that run.

## The reporting window

Windows are defined in **America/Los_Angeles wall-clock time**, not UTC, and the
offset is resolved per instant — never hardcoded. The same local midnight is
`07:00Z` in PDT and `08:00Z` in PST, and the report says which resolver produced
the boundary (`timezone_source`).

* Default: the most recently **closed** Friday→Friday week. Running at 06:00
  Pacific on a Friday reports the week that just ended and never reaches into the
  day it is written.
* **`--anchored` (what production uses)** ends the window at THIS generation
  instant and starts it at the previously persisted one, held in
  `weekly_report_anchor.json` beside the reports. The run that just finished is
  therefore inside this report, and the next window begins exactly where this one
  ends — no gap, no overlap. The first ever anchored report seeds from the fixed
  weekly boundary, so it is a sane week rather than all of history.
  * The boundary advances **after the artifact is durably on disk and before Slack
    is attempted**, so a delivery failure retries against the same closed window
    instead of merging next week into it.
  * `--anchored` requires acquisition to run **before** the report. See
    *Ordering* below.
  * `--require-completed-run` is its fail-safe: with no COMPLETED run attributed
    to the window, nothing is written and the boundary is **held at the window
    start**, so those runs land in the next report instead of being skipped over.
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

**The compact reporting ledger is the first candidate for every run-derived
metric**, and the heavy run artifacts are fallbacks. Heavy artifacts are pruned to
a handful of runs by retention; the ledger is kept for 180 days. Reporting from
the heavy artifacts alone silently lost 3 of 7 runs in 2026-W36.

| Metric | Authoritative source | Timestamp that attributes it |
|---|---|---|
| `jobs_captured` | `reporting_ledger:metrics.net_new_jobs_captured` (→ `acquisition.cumulative.net_new_jobs_captured` → `waterfall.json:unit_totals.postings`) | run completion |
| `jobs_reviewed` | `reporting_ledger:metrics.jobs_reviewed` (→ `enrichment.funnel.qualification_input`) | run completion |
| `review_rate_pct` | derived: reviewed ÷ captured | — |
| `qualified_opportunities` | `reporting_ledger:metrics.qualified_opportunities` (→ `enrichment.funnel.contact_discovery_entered`) | run completion |
| `contacts_found` | `reporting_ledger:metrics.contacts_found` (→ `waterfall.json:unit_totals.contacts`) | run completion |
| `sent_to_airtable` | `reporting_ledger:metrics.sent_to_airtable` (→ `delivery.json:created`) | run completion |
| `sent_to_instantly` | **Instantly** `lead.timestamp_created` (`--instantly`) | the lead's own creation instant |

Two of those definitions were narrowed and the change matters:

* **`jobs_captured` is NET-NEW postings**, not provider rows. A row the provider
  returns for the second time is acquisition cost, not throughput; counting it
  here made a re-bought posting look like a review-stage loss. Provider volume is
  reported separately as `provider_jobs_returned` / `provider_jobs_billed`, and the
  reasons rows did not survive dedupe are three separate counters
  (`historical_duplicates`, `canonical_duplicates_in_run`,
  `cross_source_duplicates`).
* **`qualified_opportunities` is `contact_discovery_entered`**, the moment a people
  search is actually issued — i.e. job/role policy AND company/ICP AND a resolvable
  domain all passed. It used to be `target_role_eligible`, a loose upstream gate
  that 92.6% of postings passed on the 2026-09-04 control run; that counter is
  still reported, as `role_qualified_postings`, but it is not "qualified".

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

## Slack delivery (`--slack`)

One HTTP POST of the **already generated** human summary to a Slack App Incoming
Webhook. No OAuth, no Socket Mode, no event subscriptions, no bot user. Nothing is
recomputed for Slack, and the machine-readable JSON is never sent.

The webhook lives in `SLACK_WEEKLY_REPORT_WEBHOOK_URL` and is treated as a secret:
never printed, never written to an artifact or receipt, and scrubbed out of error
text before it reaches a log (a `requests` exception embeds the request URL, so
redaction is unconditional rather than applied where it "looks risky").

**It cannot break the pipeline.** Delivery runs only after both artifacts are on
disk, and every failure mode -- missing webhook, timeout, 4xx, 5xx, malformed
response, an unforeseen exception -- resolves to a returned outcome, never a
raise. The exit status is unchanged either way.

**It cannot double-send.** Success is recorded as
`weekly_report_<ISO week>.slack_sent.json`, written atomically *after* Slack
accepts. On a later run:

| report | receipt | behaviour |
|---|---|---|
| exists | exists | skip Slack entirely |
| exists | missing | re-deliver **from the summary already on disk** — no rebuild, no Instantly read |
| missing | — | generate, then deliver |

The receipt is the only record of delivery, so a 2xx we failed to record is sent
again next week's run — erring toward a duplicate rather than a silently lost
report. It carries only safe metadata: report id, window, `sent_at`, HTTP status,
attempt count. No URL, no secret.

**It is bounded separately from the reporter.** 10s per attempt, at most 3
attempts, 2s incremental backoff — roughly 35s worst case, so it cannot spend the
`--max-seconds 480` reporting budget. Only 408/425/429/5xx are retried; a 403 or
404 means the webhook is wrong or revoked and retrying it is noise.

If `--slack` is passed without the env var set, the report is still written and
the gap is reported on stdout.

---

# Deployment

## Verified live state (Railway, read-only, 2026-09-04 19:03 UTC)

Project `tgtc-daily-pipeline` · environment `production` · workspace "My Projects".
Read with `railway status --json` and `railway run --no-local`; **always read the
live value before quoting a start command** — every table like this one in this
repository has gone stale at least once.

| | GTM | GTM Approved Sync |
|---|---|---|
| Service id | `3a41d0d7-…` | `d6b2e1c1-…` |
| Deployed commit | `4b41c4b` | `4b41c4b` |
| Deployment id | `cb739b78-…` | `a89cb53b-…` |
| Cron | **`0 3 * * *`** | `0 0 * * *` |
| Restart policy | `NEVER` (container exits after the run) | `NEVER` |
| Volume | `gtm-volume` → `/app/data/state` (5.5 / 19.5 GB) | **none** |
| `PIPELINE_ARTIFACT_ROOT` | `/app/data/state/orchestrator_v2` | — |
| `TZ` | `America/Merida` | `America/Merida` |
| `INSTANTLY_API_KEY` | set | set |
| `INSTANTLY_CAMPAIGN_*` | all 10 buckets set | all 10 set |
| `SLACK_WEEKLY_REPORT_WEBHOOK_URL` | set | not set (correct) |
| `OUTBOUND_WAVE1_*` | **not set** | set, `ENABLED=1`, 50% split |

GTM Start Command as deployed:

```sh
sh -c 'python -u run_weekly_report.py --artifact-root /app/data/state/orchestrator_v2 --if-due friday --once-per-window --instantly --slack --max-seconds 480 || true; exec python -u run_orchestrator.py --mode live_acquisition_and_enrichment --lanes fantastic --target 300 --airtable-write --global-budget 1500 --artifact-root /app/data/state/orchestrator_v2'
```

Two things about it are wrong for the anchored implementation, and both are
config-only:

1. **The report runs before the pipeline.** `--anchored` ends the window at the
   generation instant, so with report-first the run happening in the same
   container is never in the report it precedes. See *Ordering* below.
2. **`--anchored` and `--require-completed-run` are not passed at all.** PR #62 is
   deployed but not enabled: the report is still using the fixed Friday→Friday
   window, with no fail-safe against a Friday whose acquisition did not complete.

## The `--if-due` gate must be on the scheduler's calendar

`--if-due-timezone` now defaults to **UTC**, not to `--timezone`. This is a real
defect that was live:

| cron | UTC weekday at the firing | Pacific weekday at the firing | `--if-due friday` under a Pacific gate |
|---|---|---|---|
| `0 13 * * *` | Friday | Friday 06:00 | matched |
| `0 3 * * *` | Friday | **Thursday 20:00** | **never matched** |

The cron moved from `0 13 * * *` to `0 3 * * *` on 2026-09-04. The only symptom
was an ordinary `not due today` line, printed daily, on a job that exited 0 — the
weekly report simply stopped existing. The gate exists to pick one firing out of a
daily **UTC** cron, so it now compares against UTC, and the skip message names the
zone and the weekday it actually saw.

The reporting **window** is unaffected and stays Pacific-labelled. Pass
`--if-due-timezone America/Los_Angeles` to restore the old comparison.

## Ordering: acquisition first, then the report

The report must be the **second** half of the Start Command:

```sh
sh -c 'python -u run_orchestrator.py --mode live_acquisition_and_enrichment --lanes fantastic --target 300 --airtable-write --global-budget 1500 --artifact-root /app/data/state/orchestrator_v2; python -u run_weekly_report.py --artifact-root /app/data/state/orchestrator_v2 --if-due friday --once-per-window --anchored --require-completed-run --instantly --slack --max-seconds 480 || true'
```

Why this order, and why the previous "report first" reasoning no longer holds:

* **`--anchored` is the reason.** The window ends at the generation instant, so
  Friday's own acquisition is in Friday's report only if acquisition already
  finished. Report-first makes the anchored window structurally empty of the run
  in its own container.
* **The old argument was about a fixed window.** Under the fixed Friday→Friday
  boundary, Friday's run started *after* the boundary and belonged to next week by
  construction, so running first lost nothing. That is no longer the window in use.
* **Delivery time is no longer pinned to the cron.** The report now lands after the
  run, which took 3.3 h on 2026-09-04 (measured; 6.7 h at 2×). The cron at 03:00
  UTC therefore delivers between roughly 06:20 and 09:40 UTC. This is the real
  trade-off of anchoring, and it is why the due gate had to move to UTC: the
  Pacific weekday at report time flips between Thursday and Friday depending on
  how long the run took, while the UTC weekday stays Friday across the whole range.
* **`exec` can no longer be used** on the pipeline half, because something must run
  after it. The container's exit code becomes the *report's*, and the report exits
  0 unless `--strict`. If the pipeline's exit code must remain the container's,
  capture it (`rc=$?`) and `exit "$rc"` after the report — but note that the
  restart policy is `NEVER`, so the exit code is observational either way.
* **The report still cannot break the pipeline.** It runs after it, `|| true`
  absorbs any non-zero exit, and `--max-seconds 480` bounds provider reads
  in-process.
* **`--once-per-window` still bounds repeats.** A redeploy or second firing on the
  same Friday exits before touching Instantly.

## Acceptance: heavy artifacts vs ledger-only

The contract is that a week renders identically whether or not the heavy run
artifacts still exist — retention deletes them long before the 8-week reporting
horizon, and if the two stores disagree the ledger is a second opinion rather than
a survivor.

```bash
# A: render from the real artifact root, into a scratch directory.
python -u run_weekly_report.py --artifact-root <root> \
  --start <YYYY-MM-DD> --end <YYYY-MM-DD> --out-dir /tmp/acc_heavy --quiet

# B: copy ONLY the ledger store to an isolated root.
mkdir -p /tmp/acc_root && cp -r <root>/run_ledger /tmp/acc_root/

# C: render again from the ledger alone.
python -u run_weekly_report.py --artifact-root /tmp/acc_root \
  --start <YYYY-MM-DD> --end <YYYY-MM-DD> --out-dir /tmp/acc_ledger --quiet

# D: the literal Slack text must be byte-identical.
diff /tmp/acc_heavy/*.slack.txt /tmp/acc_ledger/*.slack.txt
```

`--start/--end` is the acceptance path specifically because an explicit window
**cannot move the anchor** the next real report will read, and `--out-dir` keeps
the output out of the production report directory. No `--slack`, so no receipt is
written and nothing is delivered.

`tests/test_weekly_report_production_acceptance.py` runs exactly this, including
the loss-reason census: without it the ledger-only render can state the size of a
drop but not its cause, which is a *different* message — so byte equality is also
a check that the ledger carries everything the stakeholder page is built from.

## Instantly campaigns included

`configured_campaign_ids()` reads every `INSTANTLY_CAMPAIGN_*` name in
`config.CAMPAIGN_ENV_BY_BUCKET`, plus `_SMALL`/`_MID`/`_LARGE` band variants where
set, plus the default `INSTANTLY_CAMPAIGN_ID`, **plus the Outbound Wave 1
challenger campaigns** in `OUTBOUND_WAVE1_CHALLENGER_CAMPAIGNS_JSON`.

The challenger arm is not optional detail. Wave 1 routes a share of accounts into a
separate campaign per role bucket, and those ids are not in
`CAMPAIGN_ENV_BY_BUCKET`. With Wave 1 live at a 50% split, omitting them reports
roughly half of the week's deliveries as not having happened — as a clean number,
with no gap declared. Ids are deduplicated, so a bucket whose challenger and
control are the same campaign is counted once (as `customer_success` and
`customer_support` already are).

Against the live GTM environment the control set resolves to the ten role-bucket
campaigns; `INSTANTLY_CAMPAIGN_ID` is present but **empty**, so it contributes
nothing, matching `config.resolve_campaign_id`.

**`OUTBOUND_WAVE1_CHALLENGER_CAMPAIGNS_JSON` is currently set on GTM Approved Sync
only, not on GTM**, so the code path is in place but the report cannot see the
challenger ids until that variable is added to GTM. `--campaign-id` (repeatable)
remains available as an explicit override.

Reads are `POST /leads/list` with the **singular** `campaign` filter — the plural
`campaign_ids` filter is ignored by the API and must never be relied on. Leads are
counted by `timestamp_created`; a campaign that fails or hits the 200-page ceiling
is named in `campaigns_failed` / `campaigns_truncated`, contributes nothing
silently, and drops the metric to `partial`.

## The binding constraint on architecture

Run artifacts live on `gtm-volume`, and a Railway volume attaches to exactly one
service. A separate report service therefore **cannot** read run artifacts and
would be limited to the Instantly and Airtable metrics — `jobs_captured`,
`jobs_reviewed`, `review_rate_pct`, `qualified_opportunities` and `contacts_found`
would all be unavailable. That constraint, not the schedule, is why the report is
chained onto GTM's cron.

## Slack delivery is live

`SLACK_WEEKLY_REPORT_WEBHOOK_URL` is set on **GTM only** (not on Approved Sync, and
it should stay that way), and `--slack` is in the reporter half of the Start
Command. The first real delivery landed at 2026-09-04T13:01:09Z (`HTTP 200 after 1
attempt`), reporting 2026-W36.

To disable Slack again, drop `--slack` from the Start Command (the variable can
stay); to disable it without a deploy, clear the variable — the report still
writes both artifacts and simply declares the delivery gap.

## Gates that still need explicit authorization

* **Changing the GTM Start Command** — to run acquisition first and to pass
  `--anchored --require-completed-run`. This is the only production change the
  reporting layer still needs, and it is config-only.
* **Adding `OUTBOUND_WAVE1_CHALLENGER_CAMPAIGNS_JSON` to GTM** — read-only for the
  report's purposes (it only widens which campaigns are counted), but it is a
  service-setting change like any other.
* Deploying / redeploying GTM.

Not required and not proposed: cron changes beyond what is already live, secret
rotation, new services, Airtable or Instantly writes, external sends.
