# Recovery-first calibration — 50 internal budget reservations

**Correction, 2026-09-06:** the deployed counter charged free People API Search
calls, and reserved before wrappers rather than each physical paid request. The
50-counter stop is preserved as execution evidence; it does not establish 50
billable requests or credits. See `THROUGHPUT_FIX_2026-09-06.md`.

Run `20260906T202534Z-0395cf0a`, 2026-09-06T20:25Z, inside the production container.
Acquisition disabled, external test delivery off, Instantly off. **Apollo is serving
again** — `acceptance/apollo_readiness.py` returned HTTP 200 on a credit-consuming
call (its billed credit amount has not been independently reconciled here).

## What it measured

| | |
|---|---|
| postings adopted from custody | **2,000** (the batch limit) |
| distinct company × function opportunities | **1,660** |
| internal Apollo budget reservations consumed | **50 of 50** |
| companies actually reached | **5** |
| internal reservations per company | **10.0**, including free searches |
| ICP-eligible companies | 5 of 5 (0 rejected) |
| hiring managers found | **2 of 5 (40%)** |
| contacts with an email | 2 |
| verified emails | **2** — unverified 0 |
| dispositions | FINAL_PASS **0**, NEEDS_CHECK 2, UNVERIFIED 24 |
| Airtable submitted / created | 26 / **0** |
| skip reasons | `no_contact` 24, `send_safe_withheld` 2 |
| **approved leads** | **0** |
| opportunities with no reconciled outcome | 1,634 |
| custody after the run | 3,595 (unchanged — nothing reached a terminal disposition) |

`reviewable_reconciles: True` — 26 submitted − 0 created − 0 failed = 24 + 2.

The run stopped at its internal counter:

    Apollo unavailable for the whole run (apollo_budget_exhausted) after 5 companies;
    opening the Apollo circuit and preserving completed work:
    apollo recovery budget exhausted: consumed 50 of 50 under authorization calib-2026-09-06-50

Stopped cleanly, preserved completed work, left the rest in custody, and reported as
a **budget** stop rather than a credit stop.

## Credits per 1,000 approved leads: NOT COMPUTABLE from this run

**The calibration produced zero approved leads, so there is no rate to divide by.**
Extrapolating a cost-per-approved from zero successes would be inventing the number
the exercise exists to measure.

The earlier **≥8,300 calls/day floor and ~44,000/day extrapolation are withdrawn**.
The sample average is not a lower bound, its counter included zero-credit searches,
and the historical 18.8% decomposition was already retracted for incompatible
populations. No minimum required budget follows from these figures.

The recorded outcomes include 24 `no_contact` and two `send_safe_withheld` rows.
On an interrupted run those labels alone cannot distinguish natural absence from
an incomplete search/cascade. The two withheld verified contacts need their original
gate evidence inspected; increasing the budget does not explain that withholding.

Official endpoint contract: [People API Search](https://docs.apollo.io/reference/people-api-search)
does not consume credits. [API pricing](https://docs.apollo.io/docs/api-pricing)
depends on endpoint/data and plan. Physical requests, internal reservations,
provider credits, contacts and approved leads must be reconciled separately.

## What the run proved about the machinery

Three internal constraints were found and fixed *because* this run was attempted, and
the third attempt is the one reported above. The first two produced nothing at all:

1. **A zero acquisition budget ended the whole run.** `governor_zero_budget` was set
   before the loop and `billed >= safety_cap_jobs` stopped the controller on
   iteration one. A recovery run — acquisition deliberately off, 3,595 paid-for
   postings owed — exited with `acquisition_entered: false` having adopted nothing.
   Both places now let a spent *source* budget suppress acquisition while the queue
   still drains.
2. **Adoption ran once per run**, so `PENDING_WORK_RESUME_MAX_PER_RUN` bounded the
   day as well as the batch.
3. **"Inventory exhausted" ignored the queue** — `kept == 0` is permanently true when
   acquisition is off.

And one instrumentation defect in the calibration's own output: it first printed
`with_contact 26` and `opp->contact 1.0` — a 100% conversion — because it counted
`contact_key` presence, and a no-contact lead still carries one. The run's own funnel
said `contacts_found 2`. Fixed; the table above uses the corrected count.

## What is needed to compute the real number

A cohort with attributable outcomes, original withheld-gate evidence, endpoint-level
request accounting and actual provider billing. A larger authorized sample may be
needed, but 1,000 reservations cannot be promised to reach 100 companies or establish
the approval rate from this run's counter.

That is a spending decision and is not taken here. The current authorization
(`calib-2026-09-06-50`) is spent; a larger one requires a new
`APOLLO_RECOVERY_BUDGET_ID` and count, which is what makes a fresh grant deliberate.


## Correction from the original Railway log

The retry recovered 146 original runtime log entries from deployment
`0c2692c8-e7eb-469f-8069-0f38989671bd`. Its preflight says
`delivery airtable=write submit_set=reviewable instantly=OFF`, not disabled
Airtable delivery. All 26 candidates were withheld, so zero rows were created.
Do not describe this historical run as a test with all external delivery disabled.
The two `send_safe_withheld` rows still have no per-lead reason in the logs.
Original evidence: `evidence/railway_calibration_20260906.json`.
