# Instantly replies: out of office, departures, opt-outs

Audited 2026-09-24: Instantly was receiving the replies and the core had never seen one.
`outcome_events` and `suppressions` were both empty, `people.opt_out_status` was NULL for
all 5,347 people, and no webhook was configured. The code that would have recorded them
existed (`services/suppression.apply_outcome_event`) and nothing called it.

This is what now runs.

## The defect that had to be fixed first

`OUTCOME_SUPPRESSING_EVENTS` contained a generic `reply`. Of the 540 most recent replies,
**293 were out-of-office auto-answers**. Wiring replies in without changing that would
have permanently suppressed every contact who happened to be on holiday.

Only an explicit **opt-out**, a **departure**, a **bounce** or a **complaint** ends a
relationship now. An out-of-office and a human reply are recorded and suppress nothing.

## Five labels, because they need five reactions

| label | what happens |
|---|---|
| `out_of_office` | the follow-up **waits**. Nothing is suppressed, nobody else is contacted, and it waits until the date the reply gives when it gives one |
| `no_longer_here` | that address stops being used, and the company × campaign unit gets a queued replacement search |
| `opt_out` | respected immediately, through the same suppression acquisition already consults, so it holds before the next run as well as the next send |
| `human_reply` | queued for a person. It can never become an automatic email |
| `unknown` | review. It triggers nothing |

**The text decides, not Instantly.** Measured on the same 540 replies: Instantly's
`i_status` marked 5 of 293 real out-of-office replies and 18 of 75 real departures, while
labelling 78 unrelated replies as out of office. Its label is stored beside each verdict
as evidence and is never the verdict.

**A covering contact is only taken when the reply names one for the matter** ("please
reach out to X"). A name in passing is not a referral, and the successor is recorded as
evidence for whoever works the replacement rather than acted on — we do not tell somebody
that a colleague referred us when they did not.

## How it runs

Service **GTM Replies** `0f030043-82c2-40a4-bf97-4356652c0155`, built from the same image,
cron `15 * * * *`, restart NEVER. Variables are references to the core service's
credentials; it has no Fantastic or Apollo key, so it cannot spend.

```bash
python -m tgtc_core replies-poll              # read, decide, record, queue
python -m tgtc_core replies-poll --dry-run    # decide everything, write nothing
python -m tgtc_core replies-poll --resume     # continue a first sweep deeper into history
```

* **Exactly once**: keyed by Instantly's own message id, inserted with
  `ON CONFLICT DO NOTHING`. Re-polling a page records nothing new.
* **Newest first**: each poll starts at the top and stops when a page holds replies of
  ours that are all already known. Resuming from the last cursor would walk into history
  and never see what arrived since. A page holding none of ours proves nothing — 483 of
  540 replies belong to other campaigns — so it does not end the sweep.
* **Reading never sends.** What follows is a work item: `reply_followup_when_back`,
  `replace_departed_contact`, `review_reply`. The daily runner does not pick those kinds
  up, so nothing acts on them until a step is built for it.
* **Only the nine Challenger campaigns** are ingested.

## First production ingestion (2026-09-24)

540 replies read, **57 belong to the nine campaigns**: 40 out of office, 10 departures,
1 opt-out, 6 human replies, 0 unknown. 20 came from addresses the core does not know
(leads from before the rebuild) and were recorded without queueing anything.

Written: **11 suppressions** (10 departures, 1 opt-out) and **37 queued items** (28
follow-ups, 8 replacement searches, 1 review). No out-of-office suppressed anybody.
15 of the 40 out-of-office replies carried a usable return date.

Two things the first run exposed and that are now fixed and tested: a return date was
being read against the poll's clock instead of the reply's own (which turned "back
September 22" in a reply from the 19th into September 2027), and a rollover beyond 120
days is now treated as no date at all rather than a follow-up parked for a year.

## What is NOT built

The replacement search is **queued, not executed**. Acting on it means re-opening a
closed company × campaign unit and running contact discovery again, which is the
acquisition and qualification path this work was told not to change. The queue makes the
vacancy visible with its evidence; a deliberate step, with its own gates and budget, is
what should drain it.

Rollback: set the service's `cronSchedule` to null (ingestion stops; nothing else reads
those tables), or delete the service. The suppressions it wrote are ordinary suppression
rows and stay valid on their own.
