# Production acceptance harness

Everything here is **read-only against production**. Nothing in this directory
starts a run, sends Slack, advances a reporting anchor, writes to Airtable or
Instantly, or mutates a watermark, cursor or receipt.

It exists because acceptance kept depending on a container that is almost never
alive, and the only copy of the tooling lived in a temporary scratchpad.

---

## The access constraint, stated once

The GTM service is a **cron** (`0 3 * * *` UTC). Its container exists only while a
run is in flight. Two of the three ways into production need that container:

| method | needs a live container | gets you |
|---|---|---|
| `railway volume files` | **yes** — SFTP is served through the running deployment | the durable ledger and the heavy run artifacts |
| `railway ssh` | **yes** | the same files, via `tar` |
| `railway logs -d <id>` | **no** — works for `REMOVED` deployments and well past 7 days | each run's `RUN SUMMARY` |

Between runs the first two fail, and the failures look like this:

```
railway volume files  ->  Failed to initialize SFTP session / Timeout   (~23s, CLI 5.30.1)
railway ssh           ->  Your service's container is not running (status: created)
```

Both are **expected between runs**, not defects, and neither may be worked around
by restarting or redeploying the service: the start command is
`sh -c 'report || true; exec <pipeline>'`, so *any* start is a paid acquisition run.

**Capture window.** 03:00 UTC, for as long as the run lasts. With Apollo credits
exhausted a run finishes in ~14 minutes (2026-09-06: 03:02:30Z → 03:16:33Z). With
credits available it ran 3h20m (2026-09-04). Plan for the short case.

Nothing here runs on a schedule. A capture happens because someone runs the script
inside that window.

---

## Scripts

### `capture_production_evidence.sh`

Tries all three access methods, best first, and always captures logs — so it
produces usable evidence even between runs.

```bash
bash acceptance/capture_production_evidence.sh --check   # reachability only
bash acceptance/capture_production_evidence.sh           # capture, then accept
```

Writes to `acceptance/evidence/<YYYYMMDD>/` (override with `EVIDENCE_DIR`).
If it captured production files it runs the A/B on them; otherwise it builds the
log corpus and runs the A/B on that, saying plainly which happened.

### `corpus_from_run_logs.py`

Turns captured `RUN SUMMARY` logs into the artifact shapes the report parses.

A `RUN SUMMARY` is a *rendering* of the artifacts, not the artifacts. It prints
about thirty counters; the report reads fields it never mentions — most
importantly `enrichment.funnel.qualification_input`, the only source of
`jobs_reviewed`. **A log corpus is therefore a lower bound on what production can
report.** A metric reading `unavailable` here may be measured perfectly well in
production. Use it to exercise the reporting code against real production
*values*; never to conclude production cannot measure something.

A key the summary does not print is **absent** — never zero, never inferred from a
neighbouring counter, never carried from another run.

It writes the *separate* artifact files (`waterfall.json`, `delivery.json`,
`run_status.json`) as well as the `orchestrator_result.json` roll-up, because
`weekly_report.metrics` accepts either shape while
`run_ledger._BACKFILL_FIELDS` reads `contacts_found` only from the `waterfall`
stem and `sent_to_airtable` only from `delivery`. A corpus carrying only the
embedded copies renders fine from artifacts and then drops both metrics from the
ledger — which looks exactly like a durability defect and is not one. That false
alarm happened once; the shape is now pinned here so it cannot happen again.

### `ab_report_equivalence.py`

Renders Brett's report twice and requires the two to agree.

* **A** — heavy run artifacts + the reporting ledger, as production holds them.
* **B** — the backfilled ledger **alone**, in a copy where `run_artifacts` does not
  exist. This is what the report will read once retention has evicted the period's
  heavy artifacts.

Equality is an acceptance check on the durable record, not on formatting: it asks
whether the compact store still answers everything the artifacts did. The backfill
is the real `orchestrator.run_ledger.backfill_from_artifacts`, run on an isolated
copy, in the order the pipeline runs it. Production state is neither read nor
written; the window is built in memory, so no anchor is consumed and no receipt is
created.

Compared: stakeholder text, contributing runs, values, completeness status, counted
unit. Not compared: which artifact field each number came from — that is expected
to differ, and is the entire point of a compact record.

```bash
PYTHONPATH=. PREVIEW_INSTANTLY=1 railway run --no-local --service GTM -- \
  python acceptance/ab_report_equivalence.py <corpus_dir> 2026-09-04T07:00:00Z
```

`PREVIEW_INSTANTLY=1` adds the live read-only Instantly count for the window
(`POST /leads/list`, counting `timestamp_created` in-window, distinct by lowercased
email). Exit code 0 means accepted; non-zero prints the differing metric.

---

## What a full acceptance still needs

The A/B on **production files** — A and B both drawn from `gtm-volume` — has not
run. It needs a capture during a live run. Everything else in this directory works
between runs.
