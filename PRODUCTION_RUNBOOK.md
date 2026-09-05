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

**GTM** (as deployed 2026-09-04):

```sh
sh -c 'python -u run_weekly_report.py --artifact-root /app/data/state/orchestrator_v2 --if-due friday --once-per-window --instantly --slack --max-seconds 480 || true; exec python -u run_orchestrator.py --mode live_acquisition_and_enrichment --lanes fantastic --target 300 --airtable-write --global-budget 1500 --artifact-root /app/data/state/orchestrator_v2'
```

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
| `OUTBOUND_WAVE1_*` | not set | set (`ENABLED=1`, 50% split, 10 challenger campaigns) |
| `SLACK_WEEKLY_REPORT_WEBHOOK_URL` | set | not set (correct) |
| `PIPELINE_ARTIFACT_ROOT` | set | not set (no volume) |
| `VALIDATION_VERSION` | set | not set |

`TZ=America/Merida` on both. It affects log timestamps, not the reporting window
(Pacific) or the `--if-due` gate (UTC).

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
| what did a source yield per credit? | `orchestrator_result.json:yield_ledger.by_source` |
| where is the cursor? | `orchestrator_result.json:lanes.fantastic.attribution.watermark` |
