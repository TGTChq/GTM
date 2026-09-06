# Retrospective — week of Aug 28 – Sep 03, 2026

**Ready to share. Not sent.**

The last **completed** weekly reporting period on the established schedule:
Friday-to-Friday, `America/Los_Angeles`, computed by the production `weekly_window`
rather than chosen. Every previous rendering this week covered `Sep 04 → now`, which
is the current *partial* week and a different question.

    period      Aug 28 - Sep 03, 2026   (ISO 2026-W36)
    timezone    America/Los_Angeles (zoneinfo:tzdata)
    boundaries  2026-08-28T07:00:00Z  ->  2026-09-04T07:00:00Z
    runs        20260901T130121Z-0cbadbc6
                20260902T130429Z-a54f4f19
                20260903T130019Z-65ca91e1

Rendered inside the production container against the production volume,
2026-09-06T18:58Z.

---

```
Week of Aug 28 - Sep 03, 2026
Jobs: captured not measured for the full period / reviewed not measured
Qualified opportunities: not measured
Contacts found: 0
sent to Instantly: 0

Biggest bottleneck from past week
2 of 3 runs in this period did not record net-new captured postings, so the
period's entry total is not established and no downstream rate can be read
against it. The runs that did record it are in the report document.

Action plan for the following week
1. Some runs in this period did not record net-new captured postings, so the
   period total cannot be stated. Confirm every run in the window is on a
   build that emits it before reading this report's rates.
2. jobs_reviewed comes from
   orchestrator_result.json:enrichment.funnel.qualification_input. A run that
   stopped before enrichment never produced it -- check that run's
   stop_reason.
3. Both jobs_captured and jobs_reviewed must be measurable for a rate to
   exist. A rate of 100% means every captured posting entered review; below
   100 means a run stopped between acquiring postings and reviewing them.
```

## Provenance — every number and its completeness

| metric | value | status | unit |
|---|---|---|---|
| jobs_captured | 0 | **partial** | posting |
| jobs_reviewed | — | unavailable | posting |
| qualified_opportunities | — | unavailable | company × role bucket |
| contacts_found | 0 | measured | company × role bucket |
| sent_to_airtable | 0 | measured | company × role bucket |
| sent_to_instantly | 0 | measured | Instantly lead |

`partial` and `unavailable` are preserved exactly as the evidence leaves them. A
zero that is **measured** and a zero that is **partial** are different claims, and
this table keeps them apart: `contacts_found = 0` is a real observation, while
`jobs_captured = 0` covers only the one run in three that recorded the field.

## What this week actually was

The three zeros are not a reporting failure — **this is the ten-day
zero-acquisition outage**. The Fantastic window had been left in flight since
2026-08-23, when the ATS source consumed the entire 100-credit cap and LinkedIn
received `requests=0`; replaying an eleven-day-old window against a seven-day frame
can only return empty pages. Acquisition returned nothing every day, so nothing
entered enrichment, so no contacts were found and nothing was delivered.

It self-corrected on 2026-09-04, the first day *outside* this window: that run
captured 6,205 postings. So the retrospective covers the last full week of the
outage, and the recovery lands in the following period.

**Two of the three runs also could not report their own capture.** They ran on builds
that predate the net-new capture counter, which is why the period total is `partial`
rather than a confident zero. That is a measurement gap layered on top of a real
outage, and the report distinguishes them instead of merging them into one number.

## What has changed since

Stated plainly because the action plan above is written for the week it covers, and
three of its four causes are already addressed:

* **The stale-window failure is fixed.** Acquisition now pages `date_created` slices
  whose persisted state is a set of finished date ranges, not an offset into a result
  set the provider never promised would be stable. A window below the frame floor is
  abandoned and re-derived rather than replayed.
* **The capture counter is emitted by every run since 2026-09-04**, so a future
  period total will not be `partial` for this reason.
* **`jobs_reviewed` is emitted by every run since 2026-09-05.**
* **Paid-for work is now held in custody** — 3,595 postings, 2,998 company × function
  opportunities — so an interruption no longer loses what was already bought.

## The current period, for contrast

The partial week `Sep 04 → Sep 06` reads very differently and is reported separately
in `BRETT_REPORT_2026-09-06.md`: 6,431 jobs captured, 1,048 contacts found, 781
Airtable rows, 769 leads sent to Instantly — and one open finding, 900 delivery rows
the Sep 4 run could not account for.

Acquisition is presently **paused** on purpose: Apollo returned
`BILLING.LIMIT.CREDITS_EXHAUSTED` with a zero lead-credit balance and refuses rather
than billing an overage, so nothing downstream of acquisition can run.
