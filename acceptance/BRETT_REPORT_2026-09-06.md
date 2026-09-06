# Weekly report — 2026-09-06

Generated inside the production container against the production volume
(`MAINTENANCE_ONLY` pass, deployment `1e22097f`, 2026-09-06T13:22Z), and verified to
render identically from the durable ledger alone. Verbatim.

```
Period: Sep 04 00:00 - Sep 06 06:23 PDT (2.3 days)
Jobs: 6,431 captured / reviewed not measured for the full period
Qualified opportunities: not measured
Contacts found: 1,048        sent to Instantly: 769

Biggest bottleneck from past week
781 of 1048 contacts found became sent to airtable (74.5%), the largest
observed drop among the 1 boundary this window could compare. The remaining
267 did not all fail: suppressed-as-existing, updated, withheld as not send-
safe and never-submitted records are all in that difference, and only the
delivery skip breakdown separates them.

Action plan for the following week
1. Contacts are found but not written to Airtable. Review the delivery skip
   breakdown before adding acquisition volume.
2. The delivery step could not account for these rows: it submitted them, did
   not create them, and recorded no skip reason. Its own reconciliation flag
   reports the failure. NOTE THE POPULATION -- this count is over the rows
   SUBMITTED to the writer, which includes leads with no contact and is
   therefore larger than the contacts counted upstream; it may exceed the
   difference at this boundary without contradicting it. Read the run's
   delivery record before treating it as a yield problem: an unnamed skip
   category looks identical to a loss.
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

The census reconciles against the ledger on every measured metric (`agrees=True`,
all of them), and the report renders **identically** from the heavy run artifacts and
from the compact ledger alone — `ACCEPTED: True` — so it will keep saying this after
retention has evicted the artifacts.

## The bottleneck, attributed

`delivery_unreconciled = 900`. The 2026-09-04 delivery record reads:

    entered 2,410    reviewable_submitted 1,681    created 781    failed 0
    every skip reason  0
    reviewable_reconciles  FALSE

900 rows were submitted to the Airtable writer, not created, and given no reason. The
run said so in its own reconciliation flag; nothing was reading it, so the report
first said the difference was "a transition and not a measured loss" and, before
that, blamed a suppression policy that is switched off.

**The cause is traced and already fixed.** `git show 8291a09:orchestrator/pipeline.py`
— the build that ran that day — sums exactly ten delivery counters:

    entered, reviewable_submitted, created, skipped, skipped_existing,
    failed, enrolled, final_pass, needs_check, other_reviewable

`updated_existing`, `company_function_suppressed`, `account_suppressed`, `no_contact`,
`other_unreconciled`, `send_safe_withheld`, `person_employer_duplicate` and `detail`
are absent. The per-slice delivery reports carried the reasons; the run-level
aggregate threw them away. The current build walks the whole field list. The 09-04
composition is unrecoverable, because only the aggregate is persisted.

## The other two gaps, same shape

**No review rate.** `jobs_reviewed` is partial because the 09-04 run wrote no
enrichment funnel at all (`funnel_keys: []`). It ran on `8291a09`, seven hours before
`b332577` fixed the top-up path writing an empty funnel unconditionally. A data gap,
not a reporting gap: unrecoverable, and already fixed for every run since 09-05.

**A reconciliation flag that could not fail.** The same record reported
`airtable_reconciles: true`. That check defaulted its withheld count to
`entered − reviewable_submitted`, making the comparison
`entered == submitted + entered − submitted` — true for every input, on a method whose
own docstring says it is not tautological. An absent count is now a failure, not a
pass.

All three are the same thing: an instrumentation defect fixed within hours of the run
that suffered it. None is a pipeline yield problem.

## One claim withdrawn today

An earlier rendering blamed the 267 on account-level suppression and told the reader
to verify it was deliberately on. It is **off**, and it had suppressed nothing. The
delivery skip breakdown is a fixed-shape record — every bucket is written on every run
— so a policy that never fired still contributed its name with a count of **zero**,
and those zeros were being read as causes. Worse, `contacts_found` and
`sent_to_airtable` are not a nested pair, so that boundary may be named the bottleneck
only when a reason code attributes it — and an all-zero set was satisfying that gate.
Zeros are dropped now, and the real attribution above took their place.

## Context Brett will want that is not in the report

* **Acquisition is deliberately paused.** Apollo returned
  `BILLING.LIMIT.CREDITS_EXHAUSTED` with `credit_balance 0` and refuses rather than
  billing an overage. Nothing downstream of acquisition can run, so buying more
  postings would spend money to produce nothing.
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
