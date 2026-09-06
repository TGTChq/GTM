# Production acceptance — run `20260906T030230Z-2f74ac7c`

Captured 2026-09-06 ~06:35–07:00Z, after the run had finished and its container had
gone. Every figure below is either read from a production log or derived from two
production logs; where something is inferred rather than printed, it says so.

## What actually executed

| | |
|---|---|
| run id | `20260906T030230Z-2f74ac7c` |
| started / finished | 2026-09-06T03:02:30Z → 03:16:33Z (**14 min**) |
| status | `complete` |
| deployment | `b67d580c-25e1-4d4e-8223-bf5e5a8e83fb` (SUCCESS) |
| commit | **`83fa99373731`** — the reported deployed commit |
| preflight | `package_integrity checked=25 mismatch=0 absent=0 (OK)` |

Effective configuration on that build, read back from the service:

```
APOLLO_ORG_ID_ZERO_PEOPLE_FALLBACK_ENABLED           True
APOLLO_ORG_ID_FALLBACK_MAX_PAID_MATCHES_PER_RUN      0
APOLLO_MAX_PERSON_MATCH_CALLS_PER_RUN                0
FANTASTIC_FUNCTIONAL_DISCOVERY_ENABLED               False
FANTASTIC_HISTORICAL_RECOVERY_ENABLED                False
FANTASTIC_HISTORICAL_RECOVERY_MAX_ROWS_PER_RUN       0
FANTASTIC_JOBS_TIME_FRAME                            7d
FANTASTIC_TIME_FRAME_MARGIN_MINUTES                  0
```

## Run census for the reporting window

Window `2026-09-04T07:00:00Z → 2026-09-11T07:00:00Z` — the half-open week the
2026-09-04 report closed at, in `America/Los_Angeles`. **Three** runs, not two: the
2026-09-04 13:01Z run falls inside it.

| run | start | commit | captured | reviewed | contacts | airtable |
|---|---|---|---|---|---|---|
| `20260904T130130Z-13b44a0c` | 09-04 13:01:30Z | `8291a093a94f` | **not recorded** | not recorded | 1,048 | 781 |
| `20260905T030439Z-1d102bec` | 09-05 03:04:39Z | `b31e55a24f39` | 0 | 0 | 0 | 0 |
| `20260906T030230Z-2f74ac7c` | 09-06 03:02:30Z | `83fa99373731` | 226 | 226 † | 0 | 0 |

† `qualification_input` is not printed in a RUN SUMMARY. 226 is arithmetic on
printed values — `target_role_eligible 194` + rejections (28 + 3 + 1) = 226 =
`raw_postings`. Production's artifacts carry the counter directly; this
reconstruction is not a substitute for reading it.

Two deployments spanning a cron instant were checked for runs and had none, which
is what fixes the census at three: `dd1c3973` (covers 09-05 13:00Z) and `b51c7f64`
(covers 09-04 03:00Z). The cron moved from `0 13 * * *` to `0 3 * * *` between the
09-04 and 09-05 runs, so neither slot fired twice.

## Acquisition — what the 226 cost

```
run_cap 5444 (governor, reason=pace)   billed 5444   net-new 226   cross-query dupes 5218
  fantastic_jobs_ats          billed=2722 kept=0   requests=28 novelty=0.0% offset 100->2822 stop=cap_reached
  fantastic_jobs_linkedin     billed=2722 kept=226 requests=28 novelty=8.3% offset 100->2822 stop=cap_reached
  fantastic_jobs_wellfound    billed=0    requests=0 stop=already_drained_this_window
  fantastic_jobs_ycombinator  billed=0    requests=0 stop=already_drained_this_window
window 2026-08-30T03:03:20Z -> 2026-09-04T10:02:11Z  reused=True  drained=False  watermark_committed=False
final_stop_reason max_iterations_guard      jobs_quota_remaining 77642
```

**The ATS source spent half the run's budget for zero net-new rows.** 2,722 billed,
0 kept, novelty 0.0%. That is the single largest measured inefficiency in the run.

## Pagination continuity — observed, not inferred from a saved offset

The thing that had to be shown is that the *persisted* continuation drove the
*requests actually issued*, not merely that a plausible offset was saved at the end.
Two consecutive runs give that directly:

* 09-05 closed with `offsets_at_close {ats: 100, linkedin: 100}`.
* 09-06 opened with `offsets_at_open {ats: 100, linkedin: 100}` and each source
  then issued **28 requests from offset 100**, ending at 2822.

The same pair also shows the duplicate-page fix working in production. On 09-05
linkedin stopped after one request with `stop=no_new_ids` — 200 billed, 0 net-new.
On 09-06 the same source, same window, paged past that point and found 226 net-new
rows beyond offset 100. The rows were always there; the old build refused to page
to them.

**Frame-horizon clamp — observed by effect.** The window was reused with an
unchanged upper bound while `window_lower` moved from `2026-08-23T09:02:36Z` to
`2026-08-30T03:03:20Z`, which is exactly `run_start − 7d`. Only the clamp produces
that. The `lower_clamped_to_frame` flag and the `coverage_rewinds` counter live in
the run artifact and have **not** been read.

## Apollo — the run's actual outcome

```
Apollo organization enrichment stopped for jerry.ai/Jerry: credit_exhausted (HTTP 422).
Apollo unavailable for the whole run (apollo_credit_exhausted) after 0 companies;
opening the Apollo circuit and preserving completed work.
```

The circuit opened on the **first** company. Everything downstream is therefore
zero for a single stated reason, not a pipeline fault:

```
companies_considered 0   eligible_company_buckets 0   hm_searches 0   hm_found 0
contacts_found 0   FINAL_PASS 0   airtable_submitted 0   airtable_created 0
```

Qualification did run: 226 postings in, `target_role_eligible 194`, rejections
`REJECT_QUALITY_GUARD_OTHER 28`, `REJECT_ROLE_MISMATCH 3`,
`REJECT_EXCLUDED_SENIORITY 1` — 194 + 32 = 226, reconciled.

**Org-ID fallback and paid spend.** The RUN SUMMARY prints no fallback or
paid-match counter, so their absence from the log proves nothing by itself. What
the log does establish is `hm_searches 0` and `eligible_company_buckets 0`: the
hiring-manager stage never reached a people search, and the fallback fires inside
that stage. It therefore cannot have run, and cannot have spent. The budgets were
`0` and `0` regardless. **The fallback's recovery rate remains unmeasured** — that
needs a run where Apollo is reachable.

## Reporting

A/B on the log-derived corpus: **ACCEPTED** (`ab_result.txt`, exit 0).

* included runs identical in A and B — all three, each exactly once
* stakeholder text identical
* values, completeness status and counted unit identical
* census reconciles in both: `all_reconcile: True`
* local-day grouping puts **2 runs on 2026-09-04** and 1 on 2026-09-05 — multiple
  runs in one day, both counted
* the zero-output run contributes a measured `0`; the run that never recorded a
  capture contributes **`None`**, and `jobs_captured` is reported `partial`, not as
  a total
* B has no `run_artifacts` directory at all, so reporting history demonstrably
  survives artifact removal

One false alarm is worth recording because it will recur. The first corpus wrote
only `orchestrator_result.json`, and the A/B failed with `contacts_found` and
`sent_to_airtable` measured in A and absent in B. That looked exactly like a
durability defect. It was not: `weekly_report.metrics` accepts the embedded copies,
while `run_ledger._BACKFILL_FIELDS` reads `contacts_found` only from the
`waterfall` stem and `sent_to_airtable` only from `delivery` — and production
writes both files on both pipeline paths (`pipeline.py` 653/658, 1178/1179). The
corpus was unfaithful; production is not affected. `corpus_from_run_logs.py` now
writes the separate stems and the shape is pinned there.

## Delivery — Approved Sync, on its own schedule

Approved Sync runs `0 0 * * *`, three hours *before* GTM.

| sync | eligible | delivered | outcome |
|---|---|---|---|
| 2026-09-05T00:02Z | 781 of 992 seen | **770 net-new** | 2 already in target campaign, 9 pre-existing elsewhere |
| 2026-09-06T00:03Z | 0 of 211 seen | 0 | backlog is held, not delivered |

The 09-06 sync could not have carried the 09-06 GTM run's output: it ran three
hours earlier, and that run created zero Airtable rows anyway. Nothing is awaiting
a sync.

The 211 remaining Approved rows are blocked on review state, not on delivery:
`outbound_company_held_for_review 142`, `apollo_email_not_verified 46`,
`outbound_role_held_for_review 19`, `validation_version_mismatch 3`,
`missing_role_focus 1`.

**Instantly, re-queried live for this window** (not carried forward):

```
lead_records_created_in_window 769   distinct_people 769   double_counted 0
control 408   challenger 361   with_a_sequence_step_executed 0
```

769 imported, 0 with an executed sequence step recorded on the lead
(`status_summary.lastStep.timestamp_executed`). Approved Sync counted 770
delivered; Instantly shows 769 created in-window. The one-record difference is not
explained here. **Imports are not sends.**

## Still blocked, and on exactly what

The A/B against **production files** — both A and B drawn from `gtm-volume`. It
needs the durable ledger and the run artifacts, and those need a live container:

```
railway volume files  ->  Failed to initialize SFTP session / Timeout  (~23s, CLI 5.30.1)
railway ssh           ->  container not running (status: created)
```

Both were attempted. Logs are not a substitute: a RUN SUMMARY omits
`qualification_input` (the only source of `jobs_reviewed`),
`contact_discovery_entered` (`qualified_opportunities`), and the
`acquisition_dedup` waterfall stage that is the only thing able to measure the
09-04 run's capture. Those three gaps are why this window's report reads
`not measured` where production may well read a number.

## RESOLVED, same day — 2026-09-06T10:20Z onwards

The blocker above was the assumption in its own last sentence: *"do not restart the
service to obtain a container; the start command makes any start a paid acquisition
run."* True of the start command, and the start command cannot be changed from here
— but a container's BEHAVIOUR is not fixed by its start command alone.

`MAINTENANCE_ONLY=1` (an authorized variable) makes `run_orchestrator` delegate to
`run_maintenance` **before** a `RunContext`, run directory, lane runner, enrichment
engine or delivery manager is constructed. It refuses unless acquisition is already
paused. Setting a cron a few minutes ahead then produces a live container that
acquires nothing, enriches nothing, delivers nothing, and gives full access to the
volume.

Five passes have run this way. Results, from `evidence/20260906-maintenance/`:

* **A/B on production files: `ACCEPTED: True`** — text, values, completeness
  statuses and counted units identical between artifacts+ledger and ledger-only.
* The 09-06 run reconciles exactly: `net_new_jobs_captured 226`,
  `opportunities_retained 226`, `identities_distinct 226`, `unavailable []`.
* `jobs_captured` is **6,431 measured**, not "not measured": the artifacts hold the
  09-04 run's 6,205 postings, which no log could show.
* `contacts_found 1,048` and `sent_to_airtable 781` are **measured**, and the census
  reconciles on all of them.

### `jobs_reviewed partial` — settled, and it is not an open defect

The provenance probe answers it exactly. Per run, on the volume:

| run | commit | `funnel.qualification_input` | funnel keys present |
|---|---|---|---|
| `20260903T130019Z` | — | absent | **none** |
| `20260904T130130Z` | `8291a09` | absent | **none** |
| `20260905T030439Z` | `b31e55a` | `0` | all 18 |
| `20260906T030230Z` | `83fa993` | `226` | all 18 |

The 09-04 run wrote **no enrichment funnel at all** — the topup path set
`enrichment.funnel = {}` unconditionally. That was fixed by `b332577`
(2026-09-04 11:03 MDT); the 09-04 run started on `8291a09` (03:52 MDT), seven hours
before the fix, and `git merge-base --is-ancestor b332577 8291a09` confirms the fix
is not in it. Every run since carries the field.

So the missing contribution is a **data gap, not a reporting gap**: it cannot be
recovered from any payload, because it was never written. `partial` is the correct
status, the report says so plainly, and no run from 2026-09-05 onward will repeat
it. Nothing to fix.
