# Acquisition capacity — what the evidence supports, and what it does not

Apollo blocks new enrichment measurement. It does not block analysis of the
acquisition that already ran, and it does not block the **count endpoint**, which
returns a number and quota headers, no job rows: **0 Jobs credits, 1 request**.

Everything below is either read from a production run log or from a count probe
whose cost is stated. Where a mechanism is not established, it says so.

## Measured addressable inventory

Count endpoint, 24h `date_created` window, built with the **same production
functions the run uses** (`build_title_query_plan`, `build_jb_params`,
`build_ats_params`), so it is comparable to what the run actually queries.

| query | postings / 24h |
|---|---|
| LinkedIn + production title expression (4,222 chars) | **361** |
| LinkedIn, same firmographic filters, **no** title expression | **5,511** |
| ATS + production title expression | **520** |

**Addressable titled inventory across the two active sources: ~881 postings/day.**

Wellfound and Y Combinator were not requested at all on 2026-09-06
(`drained_at_open=True`, `stop=already_drained_this_window`); on 2026-09-04 they
contributed 160 and 45 rows respectively. They are small.

The same count over the run's own 5.3-day window returned **HTTP 504 — "The count
timed out. Narrow your filters, or use a shorter time window."** The production
title expression is expensive enough server-side that the provider cannot count it
over five days. That is a fact about the query, not about the jobs endpoint.

## The 2026-09-06 run, reconciled

```
billed                       5,444   = ats 2,722 + linkedin 2,722
cross_query_duplicates       5,218   rows the provider returned MORE THAN ONCE in this run
cross_source_duplicates          0
historical_previously_seen       0
canonical_duplicates_in_run      0
postings_missing_identity        0
schema_rejected / source_filtered 0 / 0   (both sources)
-------------------------------------------------
net-new captured               226
  role-eligible                194
  REJECT_QUALITY_GUARD_OTHER    28
  REJECT_ROLE_MISMATCH           3
  REJECT_EXCLUDED_SENIORITY      1        194 + 32 = 226, reconciles
companies considered             0        Apollo circuit opened on company one
```

**The dominant measured loss is within-run re-purchase: 5,218 of 5,444 rows,
95.8% of the spend.** `seen_ids` is initialised empty per fetch, so a "duplicate"
here is a row this run was billed for more than once — not inventory a previous run
already worked.

**Do not read `historical_previously_seen = 0` as proof we are not re-buying across
runs.** That counter matches against the suppression store, which only holds
postings that reached a TERMINAL disposition. Almost nothing has reached terminal
while Apollo has been down, so suppression is nearly empty and the check is close
to vacuous right now.

### Why the governor allowed 5,444

`governor_budget 5444 reason=pace remaining=82986 reserve=10000`. It paces the
monthly job quota and nothing else. **It has no novelty signal** — no input for how
much unseen inventory the window holds. It granted 5,444 credits against a window
whose measured titled inventory is roughly 881/day × 5.3 days ≈ 4,700 rows, of
which 226 turned out to be new. That is the mechanism by which a run spends a full
budget for almost nothing: the budget is a spend limit, not a yield estimate.

### Why ATS billed 2,722 and kept zero

Every ATS row was already in the run's `seen_ids`. ATS's own measured inventory is
520/day ≈ 2,756 over the 5.3-day window — almost exactly the 2,722 it billed. So it
appears to have swept its whole window and found nothing the run had not already
seen.

**The mechanism is NOT established.** The obvious hypothesis — ATS returning the
ATS-side copy of postings LinkedIn already returned — is contradicted by
`cross_source_duplicates = 0`. Either that counter does not capture this case or
the duplication is internal to ATS's own paging. Resolving it needs a probe that
has not been run.

### Why LinkedIn kept only 226

2,722 billed, 226 unique, 2,496 already seen within the run. Measured LinkedIn
inventory is 361/day ≈ 1,914 over the window, and offsets 0–100 were consumed by
the 2026-09-05 run. So the window plausibly held on the order of 1,700 rows the run
never kept, while it paged offsets 100 → 2,822.

**Not established**, and this is the single most valuable open measurement: either
the paging is not reaching those rows (offset walking a `date_posted`-ordered feed
while the filter is on `date_created`), or the count endpoint and the jobs endpoint
disagree about what the query matches.

### Did either source exhaust its window?

No — and the flag disagrees with the arithmetic. Both recorded
`stop=cap_reached`, `drained=False`, `undrained_sources=['fantastic_jobs_ats',
'fantastic_jobs_linkedin']`, `window_drained=False`, `watermark_committed=False`.
So the run stopped on **budget**, with the window formally uninspected. But a 95.8%
duplicate rate is what an exhausted feed looks like. `cap_reached` is not in
`_DRAINED_STOPS`, so a source that has in fact run out cannot say so.

## Can this produce 1,000 new approved leads per day?

**No — and the shortfall is arithmetic, not efficiency.**

The only end-to-end evidence with Apollo working is 2026-09-04:

```
6,205 postings -> 2,410 opportunities -> 1,048 contacts -> 781 Airtable rows -> 770 Instantly
```

posting → approved Airtable row = **12.6%**. Counting units differ along that chain
and are kept distinct: postings, then company×role_bucket opportunities, then
persons.

Note what those 6,205 postings were: a **ten-day backlog** cleared after the
acquisition outage, not a daily rate. 6,205 / 10 ≈ 620/day, consistent with the
881/day ceiling measured above.

| scenario | postings/day | approved/day at 12.6% |
|---|---|---|
| observed, current sources and filters | 881 | **~111** |
| hypothetical: LinkedIn title filter removed entirely | 5,511 + ATS | ~700 (relevance destroyed) |
| required for the target | **~7,940** | 1,000 |

**Even at 100% conversion, 881 postings/day cannot yield 1,000 approved leads/day.**
The target is unreachable from the currently enabled sources and filters regardless
of any downstream improvement — Apollo, enrichment throughput and delivery are all
irrelevant to that particular gap.

**The limiting factor is measured: addressable daily acquisition inventory.**

The one lever of the right order of magnitude is the title filter: it removes 93.5%
of LinkedIn's ICP-shaped inventory (5,511 → 361). Widening it trades relevance for
volume, and the 12.6% conversion above was measured on title-matched postings — it
would not survive the trade unchanged.

### Distinguishing what this is

* **Observed:** 361/520/5,511 per day; the 09-06 reconciliation; the 09-04 funnel.
* **Conditional estimate:** ~111 approved/day, which applies one run's 12.6%
  conversion to the measured inventory. One run is not a rate.
* **Unknown:** ATS inventory without the title filter; whether other geographies or
  sources add materially; why LinkedIn kept 226 from a window measured to hold far
  more; whether ATS duplicates LinkedIn's rows.

No target is promised here, and nothing above establishes that 1,000/day is
achievable with any change.
