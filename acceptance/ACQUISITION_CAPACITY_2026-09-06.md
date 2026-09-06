# Acquisition capacity — what the evidence supports, and what it does not

**This file supersedes an earlier version that stated ~111 approved leads/day and
called 1,000/day "arithmetically impossible". Both claims went beyond the
evidence and are retracted here.** What follows separates observation from
conditional estimate from unknown, and the unknowns are load-bearing.

Apollo blocks new enrichment measurement. It does not block the count endpoint,
which returns a number and quota headers and no job rows: **0 Jobs credits, 1
request** per call.

## 1. What the duplicate counter actually counted

The earlier version said 5,218 of the 2026-09-06 run's 5,444 billed rows were
"billed more than once inside the same run". **That was wrong.**

`seen_ids` is initialised empty per fetch and then SEEDED at window open from
persisted state (`fantastic_jobs_adapter.py`, `DateCreatedWatermarkEngine.open`):

```python
for key in ("boundary_ids", "overlap_band_ids", "window_acquired_ids"):
    self.seen_ids |= {f"fantastic_{i}" for i in (self.state.get(key) or [])}
```

`window_acquired_ids` holds every id **kept in the open window by previous runs**
(`checkpoint()` writes `self.acquired`, which `_fetch_segment` extends with kept
rows only). The 2026-09-04 run kept **6,205** postings in the window that was still
in flight on 2026-09-06 — `window_reused=True`, `previous_watermark`
2026-08-23T10:02:36Z, `in_flight_window_end` 2026-09-04T10:02:11Z, never drained,
never committed. So roughly 6,205 ids were in `seen_ids` before the first request
of the 09-06 run went out.

**A duplicate is therefore predominantly a cross-run re-delivery that dedupe
correctly suppressed** — the provider returning rows an earlier run already took —
not a within-run double purchase. It is still repeated *paid* delivery: billing
counts every returned row (`seg["returned"] += 1; quota.jobs_consumed += 1` before
any disposition), which is why 5,218 wasted credits is real regardless.

### Reconciling ATS: 2,722 billed, 0 kept, `cross_source_duplicates` 0

`cross_source_duplicates` increments only when `metrics["_first_seen"]` names a
*different* source for that id — and `_first_seen` is written only for rows **kept
in this run**. A seeded id has no owner, so `owner` is `None` and the counter
cannot fire. **A zero there does not mean the sources failed to overlap; it means
the duplicates were not attributable to a source that kept the row in this run.**

Reproduced offline in `tests/test_duplicate_accounting_semantics.py` against a
well-behaved feed — nothing depends on provider misbehaviour:

| behaviour | pinned |
|---|---|
| every row already acquired by an earlier run → all duplicates, `cross_source_duplicates` 0, all billed | ✅ |
| genuine cross-source overlap → `cross_source_duplicates` fires | ✅ (so 0 is informative) |
| a new id among seeded ones survives | ✅ first occurrences are not discarded |
| a source-filtered row never claims its id | ✅ nothing pre-inserts an id it then rejects |
| a within-run repeat is suppressed but still billed | ✅ |

**No dedupe correctness defect was found.** Insertion happens only after a row is
kept; the seed format (`fantastic_{internal_id}`) matches `job["job_id"]` exactly;
`_first_seen` affects attribution only, never the keep/drop decision. The adapter's
`duplicates` and the orchestrator's `historical_previously_seen` count different
things at different stages and were both reported — conflating them was my error,
not double counting in the code.

### One demonstrated calibration defect

`FANTASTIC_MAX_CONSECUTIVE_DUPLICATE_PAGES` exists so that "one run cannot spend its
entire budget discovering" a fully-overlapping window. It is **40** in production,
and the 09-06 governor grant bought **28 pages per source**. The guard could not be
reached. ATS spent its whole allocation on consecutive all-duplicate pages and
stopped on `cap_reached`, not `duplicate_page_cap`. Pinned as arithmetic in the
tests; **not adjusted**, because the right threshold depends on why the window is
being re-paged at all, which is unresolved.

### Limits of this evidence

Production retains **no response-level id list**. `postings.json` holds only kept
rows; `window_acquired_ids` holds only kept ids; `duplicate_pages_skipped` is in the
segment metrics but is not printed in the RUN SUMMARY. **The actual
first-occurrence-and-duplicate trace for the 5,218 events cannot be reconstructed
from the 2026-09-06 evidence**, and no representative id list is offered here. The
reproductions establish the mechanism, not the history.

## 2. Inventory counts — observations, not ceilings

Count endpoint, **one 24-hour `date_created` window ending 2026-09-06T~08:30Z**.
These are single observations of one window on one day. They are **not** stable
daily ceilings, and no variance is known.

| query | count in that window |
|---|---|
| LinkedIn + production title expression (4,222 chars) | 361 |
| LinkedIn, same firmographic filters, **no** title expression | 5,511 |
| ATS + production title expression | 520 |

### Filters actually sent, and comparability

LinkedIn: `source`, `title_advanced`, `location`, `ai_employment_type`,
`organization_headcount_gte`, `organization_headcount_lt`, `organization_agency`,
`exclude_organization_industry`, `exclude_ats_duplicate`, `date_created_gte`,
`date_created_lt` — built by the production `build_jb_params` /
`build_title_query_plan`.

Removed for the count: `limit`, `offset`, `cursor`, `time_frame`, `order`. These are
pagination and window controls, not row filters; the explicit 24h `date_created`
window sits inside the production `time_frame=7d`, so the matched set is unchanged.

ATS additionally required removing **`include_basic_organization_details`**, which
the count endpoint rejects with HTTP 400 "Unknown query parameter". That parameter
controls *response shape*, not which rows match, so comparability is preserved.

The same count over the run's own 5.3-day window returned **HTTP 504 — "The count
timed out. Narrow your filters, or use a shorter time window."** The production
title expression is expensive enough server-side that the provider cannot count it
over five days.

### Overlap and missing sources — why 881 is not a total

The earlier version summed 361 + 520 = 881 and treated it as the daily total. That
sum assumes the two sources carry **disjoint** postings, which is not established —
and the LinkedIn query itself sends `exclude_ats_duplicate`, which exists precisely
because the same posting reaches us from both. **The union is unmeasured**, so 881
is an upper bound on the union, not the union.

Three configured acquisition paths are **absent from the estimate entirely**:

* **Wellfound** and **Y Combinator** — enabled, but `already_drained_this_window` on
  09-06 so they were never requested; they contributed 160 and 45 rows on 09-04.
* **Direct ATS board acquisition** — `ATS_DIRECT_ACQUISITION_ENABLED=True`, **145
  boards** in the registry (workday 68, smartrecruiters 31, ashby 22, greenhouse 13,
  lever 6, …). This is a separate, non-Fantastic path and none of it is counted
  above.

**So no total daily inventory figure is established by this evidence.**

### What the no-title count does and does not show

5,511/day is LinkedIn's ICP-shaped inventory with the title filter removed. It
establishes that the title expression excludes roughly 93% of postings that pass the
firmographic filters. It does **not** establish that any of those excluded postings
are commercially relevant. Symmetrically, the cached corpus we have is
**title-selected**, so it cannot estimate relevance among title-excluded jobs
either. Both limitations stand; neither number can be turned into addressable
inventory.

## 3. The 2026-09-04 funnel — why 781/6,205 is not a forecast

```
6,205 postings -> 2,410 opportunities -> 1,048 contacts_with_email -> 781 Airtable rows created
                                                                   -> 770 delivered to Instantly (09-05 sync)
```

Four reasons that ratio cannot be projected forward:

1. **Creations are not approvals.** The run logged
   `delivery airtable=review-staging(Pending) auto_approve=OFF`. Those 781 rows were
   created **Pending**. By the 09-05 00:02Z sync the Approved view held 992 rows of
   which 781 were valid — and 992 = the 211 pre-existing blocked rows + 781. So all
   781 became Approved within ~11 hours. **By what mechanism is not established** —
   human review, or fact-based send-safe auto-approval independent of the run's
   `auto_approve` flag. This determines whether "approved leads/day" is machine-
   bounded or human-bounded, and it is the single most important unknown here.
2. **The cohort was a backlog, not a day.** Those 6,205 postings were ~10 days of
   inventory cleared after the acquisition outage. Its company mix, and therefore
   its posting→opportunity density (2,410/6,205 = 0.388), need not hold for a daily
   slice.
3. **The run was truncated.** Apollo exhausted after **2,170 of 2,410**
   opportunities, so 240 were never processed. The observed contact rate is a floor,
   and the end-to-end ratio understates what an uninterrupted run would produce.
4. **Counting units differ at every step** and are not interchangeable: postings →
   company×role_bucket opportunities → persons → Instantly lead records.
   `AIRTABLE_SUPPRESS_EXISTING_COMPANY_FUNCTION` defaults True, so the practical
   ceiling is about **one approved contact per company×function**, which means
   *opportunities*, not postings, bound the outcome. The observed 1,048 contacts
   against 2,410 opportunities is consistent with ≤1 per opportunity, but the
   configured maximum has not been read back from production.

## 4. What the evidence supports about 1,000 approved leads/day

**Supported:** in one 24-hour window, the two Fantastic sources that were actually
queried on 09-06 matched 361 and 520 postings under production filters, with the
union unmeasured. The only end-to-end funnel observed (2026-09-04) turned a 10-day
backlog of 6,205 postings into 781 Airtable rows and 770 Instantly imports, with the
enrichment stage truncated part-way.

**Not supported, in either direction:**

* No daily inventory total exists — the source union is unmeasured and three
  configured paths (Wellfound, Y Combinator, 145 direct ATS boards) are excluded.
* No approval rate exists — creations were Pending, and the approval mechanism is
  unidentified.
* No projection to 1,000/day is warranted, and neither is any specific lower ceiling.
  The earlier "~111/day" figure applied one truncated run's ratio to a partial
  inventory count and should not be used.

**The specific missing measurements**, in the order they would settle the question:

1. Which mechanism approved 781 Pending rows on 2026-09-04, and at what rate it
   scales. Without this there is no "approved leads/day" quantity at all.
2. The **union** of LinkedIn and ATS inventory under production filters, rather than
   the sum, plus counts for Wellfound, Y Combinator and the 145 direct ATS boards.
3. An **uninterrupted** enrichment run, to measure opportunity→contact→approval on a
   daily cohort rather than a truncated backlog.
4. Why the 09-06 window was re-paged at all when a previous run had already acquired
   its contents — that determines how much of the daily budget buys anything.

Items 1, 2 and 4 need no Apollo credits. Item 3 does.
