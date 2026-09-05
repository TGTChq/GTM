# What each number in Brett's report means

Five numbers reach the stakeholder message. This traces each one from the line of
code that increments it to the line of text that prints it, and states the four
things a number needs before it can be defended: **what it counts**, **which
window it belongs to**, **which dedupe rule produced it**, and **what happens when
it is missing**.

The machine-readable version of this is in every report's JSON: each metric
carries `definition`, `unit`, `counted_unit`, `cohort`, `evidence` (the exact
artifact field read, per run) and `contributing_run_ids`.

---

## The chain, for every metric

```
producer (orchestrator/hiring_manager/Instantly)
  -> run artifact        run_artifacts/<run_id>/*.json     pruned after a few runs
  -> durable ledger      run_ledger/<run_id>.json          kept 180 days
  -> weekly aggregation  weekly_report/metrics.py          summed over the window
  -> Slack renderer      weekly_report/render.py
```

The ledger is read **first** for every run-derived metric and the heavy artifacts
are fallbacks. Reporting from heavy artifacts alone silently lost 3 of 7 runs in
2026-W36, because a pruned run was not "missing" to the reporter — it was invisible.

**Silence is never zero.** A run that does not carry a counter is listed in
`runs_missing_field` and the metric drops to `partial`. A metric no run carries is
`unavailable` **with a reason** and renders as `not measured`. "The pipeline
processed nothing" and "the artifact lacks this counter" are different facts and
only one of them is a business result.

**Excluded from every metric:** dry-run, preflight and synthetic runs
(`full_dry_run` writes the same artifact shape as a live run), and runs that
cannot be dated. Both sets are named in `provenance`.

---

## `Jobs: {X} captured`

| | |
|---|---|
| Producer | `orchestrator/pipeline.py::_dedup` — one increment per posting that passes |
| Counts | **postings**, net-new |
| Artifact | `orchestrator_result.json:acquisition.cumulative.net_new_jobs_captured` |
| Ledger | `metrics.net_new_jobs_captured` (falls back to `metrics.jobs_captured`) |
| Window | the run's completion timestamp (`run_manifest.finished_at`) |
| Missing | `not measured` |

**Dedupe rule.** A posting is captured when its canonical identity
(`posting_identity`: provider job id → apply URL → content digest) is new to this
run **and** absent from the cross-run seen-postings store. The three exits are
counted separately at the decision point — `historical_previously_seen`,
`canonical_duplicates_in_run`, `postings_missing_identity` — and
`jobs_unique_kept = net_new + those three` is asserted by the run itself
(`dedupe_reconciles`).

**What this is NOT.** Not provider rows returned (6,205 on 2026-09-04) — those are
`provider_jobs_returned`, an acquisition cost. Not `unique_opportunities` (2,410
that day), which is a company × bucket lead count, not a posting count.

## `/ {Y} reviewed ({Z}%)`

| | |
|---|---|
| Producer | `qualification_pipeline.py` — `input_jobs = len(jobs)` |
| Counts | **postings** that entered qualification |
| Artifact | `orchestrator_result.json:enrichment.funnel.qualification_input` |
| Ledger | `metrics.jobs_reviewed` |
| Missing | `Jobs: X captured / reviewed not measured` |

`{Z}` is `reviewed / captured × 100`. Both sides count postings, which is what
makes this the one ratio in the message worth printing. Equal non-zero populations
render `(100%)`; a zero denominator renders `(N/A)`, because a rate over an empty
population is undefined and `0%` would assert that nothing captured was reviewed.

## `Qualified opportunities: {Q}`

| | |
|---|---|
| Producer | `hiring_manager.py` — `stats["contact_discovery_entered"] += 1` |
| Counts | **company × role bucket**, one per people-search decision |
| Artifact | `orchestrator_result.json:enrichment.funnel.contact_discovery_entered` |
| Ledger | `metrics.qualified_opportunities` |
| Missing | `not measured` |

An opportunity is counted **once**, at the point a people search is about to run:
job/role policy passed, the company/account ICP decision passed, and a search
domain resolved. It is emitted at the decision point, never reconstructed by
subtracting reason codes from a total.

**Not** `target_role_eligible` — the pre-contact role/source gate, which 92.6% of
postings passed on the 2026-09-04 control run. Reporting that as "qualified" shows
a stage that appears to do nothing while the real ICP decision (606 rejections
that day) stays invisible. It is still reported, as `role_qualified_postings`.

**Not** FINAL_PASS, auto-approvals, or contacts.

## `Contacts found: {C}`

| | |
|---|---|
| Producer | `orchestrator/pipeline.py` — leads carrying a resolved email |
| Counts | **company × role bucket** with a hiring-manager email |
| Artifact | `waterfall.json:unit_totals.contacts` |
| Ledger | `metrics.contacts_found` |
| Missing | `not measured` |

Email **presence**, never a sum of disposition labels. It does **not** mean the
email is Apollo-verified (`verified_emails` is reported separately and was 1,034 of
1,048 on 2026-09-04), and it does not mean anything was delivered.

`hiring_manager` produces one Lead per company × bucket, so this shares its unit
with `Qualified opportunities` — which is why *that* boundary is a valid
comparison and the one above it is not.

## `sent to Instantly: {I}`

| | |
|---|---|
| Producer | Instantly, `POST /leads/list` (listing read only) |
| Counts | **Instantly leads** (person in a campaign) whose `timestamp_created` falls in the window |
| Window | the lead's own creation instant, not a run timestamp |
| Cohort | **`external_backlog`** |
| Missing | `not measured`, with the reason |

Instantly answers `200` for an address already in the workspace, so an accepted API
call is not a delivery — a new `timestamp_created` is. Campaigns covered are every
Control **and** Wave 1 Challenger id, deduplicated (`customer_success` and
`customer_support` share one campaign in production).

**Enrollment is not email delivery.** This counts leads that entered a campaign.
Whether a step actually executed for them is a separate fact, read by
`outbound_wave1/outcomes.py` as `delivered`.

**The cohort matters more than the unit here.** Enrollment is performed by GTM
Approved Sync from the Airtable `Approved` backlog, which accumulates across
weeks — on 2026-09-05 it delivered 770 leads from 781 rows built up over the
preceding fortnight. This number and `Contacts found` share a date range and
nothing else.

---

## Why the report refuses some subtractions

`weekly_report/bottleneck.py` walks adjacent funnel stages and reports the largest
loss. Before subtracting, a boundary must pass three checks, or it is recorded in
`incomparable_boundaries` and skipped:

1. **Same `counted_unit`.** `jobs_reviewed` counts postings; `qualified_opportunities`
   counts company × bucket. 6,205 − 2,410 is not "3,795 lost" — the second number
   does not count a subset of the first.
2. **Same `cohort`.** `contacts_found` is this window's work; `sent_to_instantly`
   is a provider observation over a backlog built across previous windows.
3. **Same contributing runs.** Two partial counters summed over different run sets
   are not each other's before-and-after.

If nothing survives, the report says the bottleneck **cannot be measured** rather
than picking the biggest-looking gap. Historical duplication is reported as
acquisition efficiency and is never an action to narrow upstream targeting.
