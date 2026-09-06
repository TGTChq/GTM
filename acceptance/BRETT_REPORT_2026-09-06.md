# Weekly report — 2026-09-06

Generated inside the production container on the production volume
(`MAINTENANCE_ONLY` pass, deployment `059164e8`, 2026-09-06T12:13Z). Verbatim.

```
Period: Sep 04 00:00 - Sep 06 05:13 PDT (2.2 days)
Jobs: 6,431 captured / reviewed not measured for the full period
Qualified opportunities: not measured
Contacts found: 1,048        sent to Instantly: 769

Biggest bottleneck from past week
No boundary this window can be named as the bottleneck. airtable delivery
shows a difference, but its two counters are not a proven subset and no reason
code was recorded there, so the difference is a transition and not a measured
loss.

Action plan for the following week
1. Identify which runs are silent and why. A partial total is a floor over the
   runs that answered; it is never presented as the period's number, and it
   must not be quoted as one.
2. Both jobs_captured and jobs_reviewed must be measurable for a rate to
   exist. A rate of 100% means every captured posting entered review; below
   100 means a run stopped between acquiring postings and reviewing them.
3. Confirm enrichment ran; the counter lives in
   enrichment.funnel.contact_discovery_entered, emitted by the hiring-manager
   stage when an opportunity enters contact discovery. Runs written before
   2026-09-05 do not carry it and cannot be backfilled with it.
```

## Provenance — every number, its unit and its completeness

| metric | value | status | unit |
|---|---|---|---|
| jobs_captured | 6,431 | **measured** | posting |
| jobs_reviewed | 226 | partial | posting |
| qualified_opportunities | — | unavailable | company × role bucket |
| contacts_found | 1,048 | **measured** | company × role bucket |
| sent_to_airtable | 781 | **measured** | company × role bucket |
| sent_to_instantly | 769 | **measured** | Instantly lead |

The census reconciles against the ledger on every measured metric
(`agrees=True`, all of them), and the report renders **identically** from the heavy
run artifacts and from the compact ledger alone — `ACCEPTED: True` — so it will keep
saying this after retention has evicted the artifacts.

## Two things this report deliberately does NOT say

**It does not name a bottleneck.** It did, an hour earlier: *"781 of 1,048 contacts
found became sent to airtable (74.5%), the largest observed drop"*, with an action
telling the reader to verify that account-level suppression is deliberately on.
Account-level suppression is **off** and had suppressed nothing. The delivery skip
breakdown is a fixed-shape record — every bucket is written on every run — so a
policy that never fired still contributed its name with a count of **zero**, and
those zeros were being read as causes. Worse, `contacts_found → sent_to_airtable` is
not a nested pair, so it may be named the bottleneck only when a reason code
attributes it: an all-zero set was satisfying that gate. Zeros are now dropped. The
difference between 1,048 and 781 is real and remains unexplained — which is what the
report now says.

**It does not report a review rate.** `jobs_reviewed` is partial because the
2026-09-04 run wrote no enrichment funnel at all. It ran on commit `8291a09`, seven
hours before `b332577` fixed the topup path that was writing `enrichment.funnel =
{}` unconditionally. That is a data gap, not a reporting gap: it cannot be recovered
from any payload because it was never written, and every run from 2026-09-05 onward
carries the field.

## Context Brett will want that is not in the report

* **Acquisition is deliberately paused.** Apollo returned
  `BILLING.LIMIT.CREDITS_EXHAUSTED` with `credit_balance 0` on 2026-09-06 and
  refuses rather than billing an overage. Nothing downstream of acquisition can run,
  so buying more postings would spend money to produce nothing.
  See `INCIDENT_2026-09-06_apollo_credits.md`.
* **Nothing that was paid for was lost.** 3,595 postings — **2,998 company × function
  opportunities** — are in durable custody, verified loadable and complete enough to
  enrich. They resume without repurchase.
* **The 769 leads showing zero sends are queued, not stuck.** Campaigns run
  Mon–Fri 08:00–18:00 America/Chicago; the leads landed Friday 19:02 CT.
* **The 1,000 approved-leads/day target is not reachable from the two currently
  active sources**, and the gap is quantified on the correct unit in
  `CAPACITY_ASSESSMENT.md`. Three inventory sources are unmeasured and one has a
  purpose-built measurement path that is switched off.
