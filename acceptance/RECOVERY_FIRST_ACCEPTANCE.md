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

## Configuration — three variables, and one of them stays OFF

| variable | value | why |
|---|---|---|
| `MAINTENANCE_ONLY` | **`0`** | lets the pipeline run at all; with it set the run exits 2 |
| `FANTASTIC_JOBS_ENABLED` | **stays `0`** | **acquires nothing.** The run buys no postings; the whole workload is what custody hands back |
| `PENDING_WORK_RESUME_MAX_PER_RUN` | `2000` (current default) | the budget. One run adopts at most this many; the remainder stays in custody for the next |

That combination is not a special mode — it is the ordinary pipeline with an empty
lane. Verified by execution in
`tests/test_recovery_cohort_attribution.py::ARunThatBuysNOTHINGStillDrainsCustody`:
a lane returning zero jobs still reaches the custody hand-back, `net_new_jobs_captured`
is 0, `pending_work_resumed.adopted` is the cohort, and the resumed rows reach the
enrichment engine.

Cost: **0 Fantastic credits.** Apollo enrichment for up to 2,000 opportunities.

## What to read from the run

The run summary prints one line for this:

    RECOVERY COHORT   resumed N -> leads N -> with_contact N -> final_pass N -> delivered N

and `acquisition.recovery_cohort` in the artifact carries the same figures plus
`delivered_lead_keys`. Attribution is by **posting identity**, the one key the pending
store, the enrichment leads and the delivery record share, and it counts a recovered
posting **collapsed into a lead** alongside other work — dropping those would
understate the cohort exactly where the company+bucket collapse fires.

Check, in order:

1. `net_new_jobs_captured == 0` — nothing was bought.
2. `pending_work_resumed.adopted` equals the budget or the whole remaining cohort.
3. `RECOVERY COHORT` — this is the measurement. `with_contact / resumed` **is the
   opportunity → contact rate**, on a known denominator, for the first time.
4. `pending_work_released` and the new `pending_postings` — custody shrinks only by
   work that reached a terminal disposition.
5. `all_reconcile: true`, and the delivery record's `reviewable_reconciles`.

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
