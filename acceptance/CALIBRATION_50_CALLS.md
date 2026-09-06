# Recovery-first calibration — 50 chargeable Apollo calls

Run `20260906T202534Z-0395cf0a`, 2026-09-06T20:25Z, inside the production container.
Acquisition disabled, external test delivery off, Instantly off. **Apollo is serving
again** — `acceptance/apollo_readiness.py` returned HTTP 200 on a credit-consuming
call (which itself spent one lead credit).

## What it measured

| | |
|---|---|
| postings adopted from custody | **2,000** (the batch limit) |
| distinct company × function opportunities | **1,660** |
| chargeable Apollo calls consumed | **50 of 50** |
| companies actually reached | **5** |
| **chargeable calls per company** | **10.0** |
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

The budget behaved exactly as designed:

    Apollo unavailable for the whole run (apollo_budget_exhausted) after 5 companies;
    opening the Apollo circuit and preserving completed work:
    apollo recovery budget exhausted: consumed 50 of 50 under authorization calib-2026-09-06-50

Stopped cleanly, preserved completed work, left the rest in custody, and reported as
a **budget** stop rather than a credit stop.

## Credits per 1,000 approved leads: NOT COMPUTABLE from this run

**The calibration produced zero approved leads, so there is no rate to divide by.**
Extrapolating a cost-per-approved from zero successes would be inventing the number
the exercise exists to measure.

What it does establish, and the bound that follows:

* **10.0 chargeable calls per company**, measured (n=5). At 1.2 opportunities per
  company in this cohort that is ~8.3 calls per opportunity.
* A **floor** on the cost of 1,000 approved leads: even at an impossible 100%
  opportunity → approved conversion, 1,000 approved needs ≥1,000 opportunities
  reached, i.e. **≥8,300 chargeable Apollo calls per day**. Every real conversion
  rate makes it larger — at the 09-04 run's 18.8% it would be roughly 44,000/day.
* The binding loss in this sample is **`no_contact` — 24 of 26 leads**, consistent
  with the separate 09-04 finding that the gap is hiring-manager coverage rather than
  email verification.

**n = 5 companies is too small to conclude anything about conversion.** Two of five
yielding a verified email could be 40% or could be noise. The honest output of a
50-call calibration is the per-company cost and the shape of the loss, and that is
what is claimed here.

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

A larger sample. **1,000 chargeable calls reaches ~100 companies** at the measured
rate, which is enough to see whether the approval rate is a few percent or a third —
the range that decides whether 1,000/day is reachable at all.

That is a spending decision and is not taken here. The current authorization
(`calib-2026-09-06-50`) is spent; a larger one requires a new
`APOLLO_RECOVERY_BUDGET_ID` and count, which is what makes a fresh grant deliberate.
