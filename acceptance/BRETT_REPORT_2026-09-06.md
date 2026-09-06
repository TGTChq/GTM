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

## Where the numbers that look missing actually went

Three figures in this window read as gaps. All three are the same thing: an
instrumentation defect that was fixed within hours of the run that suffered it. None
is a pipeline yield problem, and the report now names them instead of inventing
causes.

**1,048 contacts, 781 Airtable rows — and 900 rows nobody could account for.** The
2026-09-04 delivery record reads `entered 2,410`, `reviewable_submitted 1,681`,
`created 781`, `failed 0`, every skip reason **zero**, and its own
`reviewable_reconciles` flag **false**. Traced to the build: that day's delivery
aggregation summed ten counters and dropped the rest, so the per-slice reports carried
the reasons and the run-level total threw them away. Fixed since; the accumulation now
walks the whole field list. The 09-04 composition is unrecoverable because only the
aggregate is kept.

**No review rate.** `jobs_reviewed` is partial because the 09-04 run wrote no
enrichment funnel at all. It ran on `8291a09`, seven hours before `b332577` fixed the
top-up path writing an empty funnel unconditionally. Every run since carries the
field.

**A reconciliation flag that could not fail.** The same record reported
`airtable_reconciles: true`. That check defaulted its withheld count to
`entered − reviewable_submitted`, making the comparison `entered == submitted +
entered − submitted` — true for every input, on a method whose own docstring says it
is not tautological. An absent count is now a failure, not a pass.

## One claim this report withdrew today

An earlier rendering said: *"781 of 1,048 contacts found became sent to airtable
(74.5%), the largest observed drop"*, and told the reader to verify that account-level
suppression was deliberately on. It is **off**, and it had suppressed nothing. The
delivery skip breakdown is a fixed-shape record — every bucket is written on every run
— so a policy that never fired still contributed its name with a count of **zero**,
and those zeros were being read as causes.

The harm went past one wrong sentence. `contacts_found` and `sent_to_airtable` are not
a nested pair, so that boundary may be named the bottleneck only when a reason code
attributes it — and an all-zero set was satisfying that gate. Zeros are now dropped,
and the real attribution above took their place.

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
