# The first production run after Apollo returns

**Process the recovered work before buying anything new.** 3,595 postings — **2,998
company × function opportunities** — are already paid for and in custody. They are
also the only cohort that can answer the question the interrupted run left open, and
they cost nothing to answer it with.

No new acquisition spend should be proposed until this run's complete path is
measured.

## Why this cohort, and not a fresh run

The `opportunity → contact` rate is **unknown**. The one observation comes from the
2026-09-04 run, which Apollo interrupted partway through contact discovery, so its
denominator cannot be separated into "searched and found nobody" from "never
attempted" (see `CAPACITY_ASSESSMENT.md`). A fresh acquisition run would spend
credits to produce another cohort with the same question attached.

The recovered cohort is different in exactly the way that matters: it is a **fixed,
enumerated set of opportunities**, so its denominator is known before the run starts.
Whatever fraction of it produces a contact IS the rate, measured rather than inferred.

## Preconditions

1. `python acceptance/apollo_readiness.py` returns READY. It calls
   `organizations/enrich` once — free while Apollo refuses, **one lead credit if it
   succeeds**.
2. Custody intact: the maintenance pass reports `pending_postings 3595` across two
   runs, `resumable: true`, `unidentifiable_employer: 0`.

## Configuration — and the budget that actually bounds spend

**`PENDING_WORK_RESUME_MAX_PER_RUN` is not a budget.** It bounds how much WORK a run
adopts. 2,000 resumed postings can issue an organisation enrich, a people search and
one or more person matches each, plus the alternate cascade and the org-id fallback
behind them — so a workload cap of 2,000 could authorise several times that many
chargeable calls. The earlier version of this document presented it as the spending
control. It is not one.

`orchestrator/apollo_budget.py` is. It counts **every chargeable path** — organisation
enrich, people search, person match, wherever called from, retries included — it is
**durable across runs** so an interrupted run cannot restart its own budget, and an
**unset budget is zero, not unlimited**.

| variable | value | why |
|---|---|---|
| `MAINTENANCE_ONLY` | **`0`** | lets the pipeline run at all |
| `FANTASTIC_JOBS_ENABLED` | **stays `0`** | **acquires nothing**; the workload is what custody hands back |
| `PENDING_WORK_RESUME_MAX_PER_RUN` | `2000` | workload cap — *not* a spend cap |
| `APOLLO_RECOVERY_BUDGET_ENABLED` | **`1`** | off by default, so today the run would refuse |
| `APOLLO_RECOVERY_BUDGET_ID` | **an authorization label** | a NEW id resets the durable counter; raising the number alone does not |
| `APOLLO_RECOVERY_BUDGET_CALLS` | **the granted number** | 0 = refuse |

All three budget settings are required together. `apollo_budget.preflight()` answers
before any work is adopted and names which one is missing, because a refusal at the
top costs nothing and a refusal partway through has already spent.

**Deferral needed no new mechanism.** On exhaustion the caller stops, the unfinished
work never reaches a terminal disposition, and `pending_work` keeps it for a later
run. Exhaustion is a pause, not a loss — which is what makes a hard ceiling safe to
set low, and the reason to start low.

### Sizing the grant

Worst case is roughly **3 chargeable calls per company** (org enrich + people search +
one person match), plus cascade and fallback retries where they fire. For the full
2,998-opportunity cohort across 2,808 companies that is on the order of **8,400–12,000
calls**; a first acceptance need not authorise all of it.

**Recommended first grant: 1,000 calls.** Enough to process several hundred
opportunities end to end and measure the conversion, small enough that being wrong
about the per-opportunity cost is cheap. The remainder stays in custody, and the run
summary reports exactly what was consumed and what deferred.

That combination is not a special mode — it is the ordinary pipeline with an empty
lane. Verified by execution in
`tests/test_recovery_cohort_attribution.py::ARunThatBuysNOTHINGStillDrainsCustody`:
a lane returning zero jobs still reaches the custody hand-back, `net_new_jobs_captured`
is 0, and the resumed rows reach the enrichment engine.

Cost: **0 Fantastic credits**, and Apollo bounded by the grant above.

## What to read from the run

The run summary prints the cohort in its three units, with the rate naming its own
denominator:

    RECOVERY COHORT        postings N / opportunities N / leads N
      attempted            N opportunities -> with_contact N -> final_pass N -> delivered N
      opp->contact         0.NNNN (denominator: opportunities_attempted)
      no reconciled outcome N

and `acquisition.recovery_cohort` in the artifact carries the same figures plus
`delivered_lead_keys`. Attribution is by **posting identity**, the one key the pending
store, the enrichment leads and the delivery record share, and it counts a recovered
posting **collapsed into a lead** alongside other work — dropping those would
understate the cohort exactly where the company+bucket collapse fires.

Check, in order:

1. `net_new_jobs_captured == 0` — nothing was bought.
2. `postings_resumed`, `opportunities_resumed` and `leads` — **three different units,
   reported separately.** Custody stores postings; approvals are capped per company ×
   function; the stage emits leads. Conflating them is what produced every bad
   capacity number this week.
3. `opportunities_attempted` and `opportunity_to_contact_rate` — the rate divides by
   **distinct opportunities the stage produced an outcome for**, and
   `rate_denominator` names it. This is the first honest opportunity → contact
   measurement, because this cohort's denominator is known before the run starts.
4. `opportunities_without_reconciled_outcome` — work with no outcome. **Not** called
   "never attempted": an absent outcome is not evidence of an absent attempt.
5. `pending_work_released` and the new `pending_postings` — custody shrinks only by
   work that reached a terminal disposition.
6. The Apollo budget summary — consumed, remaining, deferrals.
7. `all_reconcile: true`, and the delivery record's `reviewable_reconciles`.

## Then follow it through approval and the normal sync

Approval is automatic for send-safe Fantastic rows
(`FANTASTIC_AUTO_APPROVE_SEND_SAFE=True`), and enrolment happens in a **different
service on a different schedule** — GTM Approved Sync, cron `0 0 * * *`. So the
cohort's path does not finish inside the run that created it.

`delivered_lead_keys` is kept in full for exactly this: after the next Approved Sync,
join those keys against the Instantly enrolment to get the cohort's complete path.

    resumed -> leads -> with_contact -> final_pass -> Airtable rows -> enrolled

Nothing about that join requires new tooling; it requires the keys, which the run now
records. **A count could not be joined to anything. A key can.**

## The rule this run establishes

Only after the above reads end-to-end should more acquisition spend be proposed. If
the cohort converts near the 18.8% the truncated run suggested, more inventory is not
the lever and buying it would repeat the same result at scale. If it converts far
higher, the truncation was the story and the capacity arithmetic changes again.

Either way the answer costs **zero acquisition credits**, and it is the cheapest
question available.
