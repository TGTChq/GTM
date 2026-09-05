# TGTC production runbook

**This is the authoritative document for the live system.** Where any other file
in this repository disagrees with it, this one is right and the other is stale.
Several of them are: `DEPLOY_RAILWAY_STEP_BY_STEP.md`,
`PRODUCTION_HANDOFF_CHECKLIST.md` and `PRODUCTION_DEPLOYMENT.md` each quote a cron
or a lane set that production has not run for weeks. They are kept for their
history, not their instructions.

Every value below was read from the live services, read-only, on **2026-09-04**.
Read the live value again before acting on any of it — the table below has been
wrong within a day of being written, more than once.

```bash
railway status --json                                  # cron, start command, volume, deployment
railway run --service GTM --no-local -- python -c '...' # a variable's value, without printing it
railway logs -d <deployment-id>                        # a past run, well beyond 7 days
railway ssh --service GTM "<cmd>"                      # the volume, ONLY while a container runs
```

---

## 1. What runs, where

Project `tgtc-daily-pipeline` · environment `production` · region `sfo`.

| | **GTM** | **GTM Approved Sync** |
|---|---|---|
| Purpose | acquisition → qualification → enrichment → Airtable review staging | Airtable `Approved` → Instantly enrollment |
| Service id | `3a41d0d7-cd66-4f53-baa6-886266ddbbed` | `d6b2e1c1-c931-4803-8555-91a8216124bc` |
| Cron (UTC) | `0 3 * * *` | `0 0 * * *` |
| Restart policy | `NEVER` — the container exits when the run ends | `NEVER` |
| Volume | `gtm-volume` → `/app/data/state` (5.5 / 19.5 GB) | **none** |
| Artifact root | `/app/data/state/orchestrator_v2` | — |
| Writes to | Airtable (`Status=Pending`) | Instantly, Airtable (`Status=Enrolled`/`Error`) |

A Railway volume attaches to exactly one service. That single fact decides the
architecture: nothing except GTM can read run artifacts, which is why the weekly
report is chained onto GTM's cron rather than run as its own service.

### Start Commands are SERVICE-managed

They are **not** in `railway.json`, and they are not in this repository. The image
`CMD` is a deliberately harmless zero-network preflight
(`run_orchestrator.py --preflight-only`), so a service with no Start Command set
performs a readiness check instead of silently running acquisition.

`railway redeploy` re-deploys the OLD snapshot and will not pick up a settings
change. Use the dashboard, or a `serviceInstanceDeployV2` call.

**GTM** (as applied 2026-09-05 — acquisition first, then the anchored report):

```sh
sh -c 'python -u run_orchestrator.py --mode live_acquisition_and_enrichment --lanes fantastic --target 300 --airtable-write --global-budget 1500 --artifact-root /app/data/state/orchestrator_v2; rc=$?; python -u run_weekly_report.py --artifact-root /app/data/state/orchestrator_v2 --if-due friday --once-per-window --anchored --require-completed-run --instantly --slack --max-seconds 480 || true; exit $rc'
```

The order is not cosmetic. `--anchored` ends the reporting window at the
generation instant, so a report that runs FIRST can never contain the run in its
own container. `exec` had to go for the same reason — something now runs after
acquisition — so `rc=$?` … `exit $rc` preserves the pipeline's exit code as the
container's, verified across all nine exit-code combinations.

`--if-due friday` is evaluated in **UTC**, which is the zone Railway cron
schedules in. It defaulted to the reporting timezone (Pacific), and when the cron
moved to `0 3 * * *` the gate stopped matching entirely: 03:00 UTC Friday is
Thursday 20:00 Pacific. The job printed "not due today" and exited 0, every day.

The whole string is pinned in `tests/test_gtm_start_command_contract.py` and
driven at the real firing instants, including the 3.3 h and 6.7 h acquisition
cases.

**GTM Approved Sync**: `python -u run_approved.py`

---

## 2. Repository defaults vs required Railway values

`config.py` defaults are chosen so that **a deploy with no environment changes
nothing**. Most of the interesting behaviour is therefore off in the repository
and on in Railway, and reading `config.py` alone will mislead you about what
production does.

Flags that are OFF by default and ON in production (GTM):

```
PRE_APOLLO_EXISTING_DEDUPE=1          AIRTABLE_WRITE_SEND_SAFE_ONLY=1
ALTERNATE_CONTACT_CASCADE_ENABLED=1   HM_SECOND_PASS_TITLE_BROADENING=1
HM_DOMAIN_CORROBORATION_RECOVERY=1    SEGMENT_ALLOCATOR_ENABLED=1
YIELD_LEDGER_ENABLED=1                FANTASTIC_DATE_CREATED_WATERMARK_ENABLED=1
FANTASTIC_FUNCTIONAL_ROLE_EXPANSION_ENABLED=1
FANTASTIC_FUNCTION_AWARE_UPSTREAM_DEDUPE_ENABLED=1
FANTASTIC_TITLE_ALIASES_ENABLED=1
```

Acquisition shape (GTM): four Fantastic sources enabled
(`ATS`, `LINKEDIN`, `WELLFOUND`, `YCOMBINATOR`), `FANTASTIC_SOURCE_ALLOCATION=fair_share`,
per-source limits `3000` each, `FANTASTIC_JOBS_MAX_JOBS_PER_RUN=12000`,
`FANTASTIC_JOBS_TIME_FRAME=7d`, `NET_NEW_SEND_SAFE_TARGET=1000`.

The per-source limits SUM against the per-run cap, which is why four sources at
3000 need a 12000 cap rather than a 3000 one.

Flags that remain OFF in production (unset, repository default `False`):

```
COMPANY_OPPORTUNITY_COLLAPSE_ENABLED
```

Values left UNSET so the repository default applies: `FANTASTIC_TOPUP_SLICE_JOBS`
(500), `TOPUP_MAX_ITERATIONS` (40), `TOPUP_RUNTIME_BUDGET_SECONDS` (0 = uncapped),
`ALTERNATE_CONTACT_MAX_ENRICHMENTS_PER_RUN` (100),
`FANTASTIC_AUTO_APPROVE_SEND_SAFE` (default `True`).

### Per-service drift is real

The two services do **not** share an environment, and reading one and calling it
"production" has been wrong before. Differences that matter today:

| variable | GTM | GTM Approved Sync |
|---|---|---|
| `OUTBOUND_WAVE1_ENABLED` | **not set** (delivery cannot fire here) | `1` |
| `OUTBOUND_WAVE1_B_SPLIT_PCT` / `_SALT` / `_MIN_RECORD_CREATED_AT` | not set | set |
| `OUTBOUND_WAVE1_CHALLENGER_CAMPAIGNS_JSON` | set 2026-09-05, **read only by the weekly report** | set, read by the enrollment overlay |
| `SLACK_WEEKLY_REPORT_WEBHOOK_URL` | set | not set (correct) |
| `PIPELINE_ARTIFACT_ROOT` | set | not set (no volume) |
| `VALIDATION_VERSION` | set | not set |

`TZ=America/Merida` on both. It affects log timestamps, not the reporting window
(Pacific) or the `--if-due` gate (UTC).

The Wave 1 campaign map is on GTM **only** so `configured_campaign_ids()` counts
Challenger-arm deliveries in `sent to Instantly` — without it that number
under-reports by the challenger share (measured 2026-09-05: 361 challenger vs 408
control of 770 delivered). It cannot enable delivery there:
`wave1_enrollment_overlay` returns `("", {})` unless `OUTBOUND_WAVE1_ENABLED` is
true, and GTM never calls `airtable_record_to_lead` at all — `run_approved.py` is
the only production caller, and it runs on the other service.

---

## 2b. What GTM actually does to an Airtable row

Two policies decide this, both ON in production, and both have been documented
wrongly before. Neither is "everything uncertain lands in Pending for a human".

### `AIRTABLE_WRITE_SEND_SAFE_ONLY=1` — a non-send-safe row is NOT WRITTEN

`airtable_client.push_leads` filters the create list through `send_safe_facts`
before writing. A candidate that fails is **withheld entirely** — not written as
`Pending`, not written in any status. It stays in the run's enrichment artifacts
and is reported as `skip.send_safe_withheld`.

This was the largest single category on 2026-09-04: **1,681 submitted, 781
created**. The other 900 were withheld here.

### `FANTASTIC_AUTO_APPROVE_SEND_SAFE` (default `True`, unset in Railway) — a send-safe Fantastic row is created `Approved`

`airtable_client._job_to_fields` sets `Status = Pending`, then promotes to
`Approved` when all three hold:

1. `FANTASTIC_AUTO_APPROVE_SEND_SAFE` is on;
2. `_is_fantastic_job(job)` — a genuine Fantastic Direct API record, identified by
   the `_fantastic_internal_id` the adapter stamps. Never an ATS/JSearch/free-feed
   row;
3. `send_safe_facts(cleaned)` passes.

**`send_safe_facts` is disposition-label-INDEPENDENT.** Its actionable set is
`{FINAL_PASS, NEEDS_CHECK, UNVERIFIED}`, so a `NEEDS_CHECK` or `UNVERIFIED` record
whose stored facts are all safe **is** auto-approved. FINAL_PASS alone is not the
criterion and never was. What it actually requires: an actionable Final Decision,
the current validation version, a present and matching fingerprint, a non-empty
email, `Apollo Email Status = verified`, `Email Validation = PASS`,
`Contact Alignment = PASS`, and no `Outbound Hold`.

`Status` is excluded from the signed fingerprint, so promoting it falsifies
nothing — the original decision, reason and evidence bundle are preserved intact.

### The two together

With both on, essentially every row GTM writes is created `Approved`, because the
write gate and the approval gate are the same predicate. That is the intended
design, not a bypass: Approved Sync independently re-runs `send_safe_facts` before
any enrollment, so a row whose facts have since changed is refused at delivery.

### Do not misread the preflight line

```
delivery   airtable=review-staging(Pending) auto_approve=OFF instantly=OFF
```

`auto_approve` there is `RealDelivery.auto_approve` — the *delivery mode* that
would submit only FINAL_PASS rows and enroll them directly. It is correctly OFF.
It is **not** `FANTASTIC_AUTO_APPROVE_SEND_SAFE`, which is ON and operates inside
`_job_to_fields`. Two different switches, similar names, opposite states.

---

## 3. A normal scheduled run

Nobody triggers acquisition by hand. The cron fires, the container starts, the
Start Command runs, the container exits. `restartPolicyType: NEVER` means a
crashed run stays crashed until the next cron rather than looping.

What a healthy GTM run leaves behind, under
`/app/data/state/orchestrator_v2/run_artifacts/<run_id>/`:

```
run_manifest.json  run_status.json  waterfall.json  delivery.json
capacity_report.json  topup.json  acquisition.json  orchestrator_result.json
```

...plus one entry in `run_ledger/<run_id>.json`, which is the compact record the
weekly report actually reads. **Heavy artifacts are pruned to a handful of runs;
the ledger is kept for 180 days.** Reporting from the heavy artifacts alone lost 3
of 7 runs in 2026-W36 without noticing.

The run also prints a full summary to the Railway log, which is the only thing
readable without the volume. Read it with `railway logs -d <deployment-id>`.

## 4. Bounded manual validation

Safe, and none of it touches production state:

```bash
python -m pytest tests -q -p ci_no_network       # offline, credential-free
python ci_check_integrity.py                     # manifest vs tree
python -u run_orchestrator.py --preflight-only   # zero-network readiness only
python -u run_weekly_report.py --artifact-root <root> \
    --start YYYY-MM-DD --end YYYY-MM-DD --out-dir /tmp/acc --quiet
```

The weekly-report form above is the acceptance path specifically because an
explicit `--start/--end` window **cannot move the anchor** the next real report
reads, and `--out-dir` keeps the output out of the production report directory.
No `--slack`, so nothing is delivered and no receipt is written.

## 5. Forbidden without explicit, action-time authorization

Not "discouraged". Each of these has either caused an incident or is
unrecoverable:

* **Triggering an unbounded acquisition run by hand.** The run cap is a governor
  decision, not a default; a hand-started run can spend the month's credits.
* **Deleting or resetting the watermark, the seen-postings store, or the window
  offsets.** The watermark cannot advance past uninspected inventory by design;
  resetting it re-buys everything and loses the in-flight window.
* **Wiping the Railway volume.** It holds the watermark, the seen state, the
  reporting ledger, the run artifacts, the delivery state and the scheduler state.
* **Bulk-updating Airtable.** Display fields are SIGNED: patching one without
  re-signing `Validation Fingerprint` leaves the row failing
  `validation_fingerprint_mismatch`, which is strictly worse than the hold it was
  meant to clear. Any migration needs a rollback snapshot, an exact record-id
  list, a canary with a read-back, and then the rest.
* **Manually enrolling Instantly leads.** Instantly returns `200` for an address
  already in the workspace, so an accepted call is not a delivery and duplicates
  under-report.
* **Activating or reconfiguring Wave 1 campaigns.**
* **Sending Slack outside a real report run.**
* **Rewriting git history** to remove operational data. See §7.

## 6. Changing a Railway service setting

Show all five before changing anything, and collect every change into one request
rather than asking per variable:

1. the current exact value, read live;
2. the proposed exact value;
3. why it is required;
4. the expected impact;
5. the rollback.

A successful deployment proves that a SHA deployed. It does not prove the Start
Command, the environment, the cron, the volume, that a scheduled run occurred,
that a flag was enabled, that the feature executed, or that the result improved.
Those are seven separate facts and each needs its own evidence.

## 7. Operational data in a public repository

`reports/` contains generated run evidence — company names, Airtable record ids,
campaign ids — committed over time. `.gitignore` now denies the families that
carry the most (`reports/wave1_*`, `reports/anchor_conflict_*.json`,
`reports/*control_activation*`, `reports/weekly/`), but files committed before
those rules are still in history.

**Do not rewrite history to remove them.** Every clone, fork and worktree would
diverge, and the exposure is already public. The safe remediation is, in order:
inventory what is actually exposed; confirm no credential or personal email is
among it (mailbox addresses and lead emails are the material risk, company names
are not); decide with the owner whether the repository should be private; and
treat any exposed credential as rotated-by-default rather than assessed. Track it
as its own piece of work.

## 8. Where the numbers come from

| question | answer |
|---|---|
| what did last night's run do? | `railway logs -d <deployment-id>` — the RUN SUMMARY block |
| what did the week do? | `weekly_reports/weekly_report_<ISO week>.txt` on the volume |
| what does the stakeholder see? | the `.slack.txt` beside it — same report object, shorter |
| was it delivered? | `weekly_report_<ISO week>.slack_sent.json` — the only delivery record |
| how many credits did a source cost? | `orchestrator_result.json:acquisition.cumulative.per_source` |
| why were rows submitted but not created? | `delivery.json:skip_breakdown` — `send_safe_withheld` is usually the largest |
| what does each Brett number mean? | [METRIC_CONTRACT.md](METRIC_CONTRACT.md) |
| what did a source yield per credit? | `orchestrator_result.json:yield_ledger.by_source` |
| where is the cursor? | `orchestrator_result.json:lanes.fantastic.attribution.watermark` |

---

## 9. Evidence-backed limitations, as of 2026-09-05

Stated because a runbook that only lists what works is not a runbook.

* **The persistent window cursor (#68) has not been observed resuming across two
  scheduled runs.** It was deployed at 18:54 on 2026-09-04; the last cron before
  that was 13:00, and the cron then moved to `0 3 * * *`. The acceptance
  comparison — `watermark.offsets_at_open` on run N equals `offsets_at_close` on
  run N-1, for the same window — needs two consecutive runs on a build carrying
  the cursor. An initial zero offset after a backward-compatible migration proves
  nothing, and a legitimately new window resets offsets by design.
* **The weekly report has not delivered under the new Start Command.** The
  configuration is applied and its argument path is tested at the real firing
  instants, but `--if-due friday` means the first live delivery is the Friday
  cron. A Saturday or Sunday firing correctly writes nothing.
* **`sent to Instantly` measures enrollment, not sending.** A lead that entered a
  campaign has not necessarily been emailed. `outbound_wave1/outcomes.py` reads
  the `delivered` fact separately.
* **Apollo shared credits are an account limit, not a software defect.** The
  2026-09-04 run stopped Apollo enrichment at `credit_exhausted` after 2,170
  companies and correctly opened the circuit, preserving completed work. No code
  change moves that ceiling.
* **`COMPANY_OPPORTUNITY_COLLAPSE_ENABLED` is off** and stays off: there is no
  measured reason to enable it, and a flag flipped without one is a change nobody
  can evaluate.
* **The #71 company-display backfill is not applied.** 14 rows are candidates for
  a hold repair; 10 of them additionally carry a verified email and would become
  enrollable. Those are two different counts of two different things, and the
  migration needs its own action-time authorization.
