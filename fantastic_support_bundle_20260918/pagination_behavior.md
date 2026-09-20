# Pagination behavior, read from the code

Two code paths have called Fantastic:

- **A. Current path:** `tgtc_core`, commit `be3af32`, deployed 2026-09-18. This is what runs today.
- **B. Legacy path:** `fantastic_jobs_adapter.py`, commit `c30178d`. It is paused since 2026-09-09. Its last productive run was `20260908T030247Z`. It is included because most of the repetition history comes from it.

Line numbers refer to those exact commits.

---

## A. Current path (`tgtc_core` @ `be3af32`)

### Unit of pagination: a partition

Every `(endpoint, query profile, lane, date_created window)` is one partition, with its
own persisted `next_offset`. The windows come from two lanes:

- **Fresh lane:** 1-hour windows. The latest window ends 3 hours before now, rounded down to the hour.
- **Backfill lane:** 24-hour windows that walk backwards from the oldest window planned so far.

`tgtc_core/services/acquisition.py:356-373`
```python
upper_bound = (moment - self.fresh_lag).replace(minute=0, second=0, microsecond=0)
...
start = row["e"] if row and row["e"] else upper_bound - self.fresh_window
while start < upper_bound and len(created) < max_new:
    end = start + self.fresh_window
```

Effective values (`tgtc_core/config.py:64-72`; none are overridden in production):

| setting | value |
|---|---|
| `fantastic_page_limit` | 100 |
| `fantastic_fresh_window_minutes` | 60 |
| `fantastic_fresh_lag_minutes` | 180 |
| `fantastic_backfill_window_hours` | 24 |
| `fantastic_time_frame` | `7d` |
| `fantastic_cycle_page_slots` | 10 (set explicitly in production) |

### How the next page is requested, and which parameter advances

The query is built once per partition and then **frozen**. Later pages reuse the first
request's parameters. Only `offset` and `limit` change.

`tgtc_core/services/acquisition.py:310-319`
```python
def request_params(self, source, *, lower, upper, offset, profile=LEGACY_PROFILE):
    spec = SOURCE_SPECS[source]
    exclude = spec.supports_exclude_ats_duplicate and SOURCE_ATS in self.sources
    params = build_window_params(lower_iso=_iso(lower), upper_iso=_iso(upper), limit=self.page_limit, offset=offset,
                                 time_frame=covering_time_frame(self.time_frame, lower, upper, self.now()),
                                 location=self.location, exclude_ats_duplicate=exclude,
                                 include_basic_organization_details=spec.supports_basic_organization_details)
    params.update(profile_filters(profile))
```

`tgtc_core/domain/acquisition_query.py:120-135`
```python
def frozen_page_query(proposed, previous, *, offset, limit):
    """Resume the same result set. Only page size/offset may change."""
    if offset and not previous:
        raise ValueError("partition_query_history_missing")
    query = dict(previous if previous is not None else proposed)
    ...
    if "cursor" in query:
        raise ValueError("partition_cursor_mode_unsupported")
    query.update(offset=offset, limit=limit)
```

The offset comes from the partition row: `offset = int(part["next_offset"])`
(`acquisition.py:544`). **`offset` is the only field that advances.** It advances by
the number of rows received. The `date_created_gte` / `date_created_lt` bounds of a
partition never change.

### Initial and maximum offset; page size

- The initial offset is 0 for every partition (`schema.sql:24`, `next_offset integer NOT NULL DEFAULT 0`).
- The page size is `limit=100`. The very first pages on 2026-09-16 used 50.
- **There is no maximum offset.** A partition keeps receiving pages, one per slot, across cycles and runs until it completes.
- In production each cycle has 10 single-page slots (`runner.py:233-240`, `run_partition(chosen, max_pages=1)`). Slots alternate endpoints, and slots 2 and 7 use the broad profile:

`tgtc_core/domain/acquisition_query.py:90-91`
```python
for slot in range(pages):
    yield sources[slot % len(sources)], (DISCOVERY_PROFILE if slot % 5 == 2 else PRIORITY_PROFILE)
```

The priority profile takes the newest open window first; the discovery profile takes
the oldest (`runner.py:233-234`). Every 5th page of a source/profile prefers the backfill
lane (`acquisition.py:408-415`).

### What a page does

`tgtc_core/services/acquisition.py:625-631`
```python
ids = [str(r.get("id") or "") for r in page.rows]
fp = hashlib.sha256("\x1f".join(ids).encode()).hexdigest()
duplicate_page = bool(ids) and fp == prev_fp
complete = (not duplicate_page) and len(page.rows) < self.page_limit
advance = 0 if duplicate_page else len(page.rows)
consecutive_duplicates = consecutive_duplicates + 1 if duplicate_page else 0
stall = duplicate_page and consecutive_duplicates >= MAX_CONSECUTIVE_DUPLICATE_PAGES   # = 3 (line 76)
```

### Stop conditions

| stop | meaning | code |
|---|---|---|
| `complete` | a non-duplicate page returned fewer rows than `limit`; the partition is finished | `acquisition.py:628, 717-719` |
| `page_budget` | this slot's single page is used; the partition stays open for the next slot or run | `acquisition.py:723-724` |
| `duplicate_page_loop` | the same id set came back 3 times in a row at one offset; the partition is **stalled**, not complete | `acquisition.py:631, 720-722` |
| `quota_reserve:requests` / `quota_reserve:jobs` | the last quota headers leave less than 20 requests or 90 jobs after this page | `acquisition.py:488-494` |
| `spend_budget_exhausted:*` | TGTC's own per-24h budget refused the reservation (100 credits reserved per page) | `acquisition.py:578-585` |
| `auth_refused` / `quota_refused` | HTTP 401/403 or 429; the partition is stalled and the provider marked refusing | `acquisition.py:596-611` |
| `request_error:http_NNN` | any other non-200 response; nothing advances | `acquisition.py:612-622` |
| `timeout_uncertain` | a timeout after sending; recorded as possibly billed; nothing advances | `acquisition.py:589-595` |
| `partition_outside_provider_time_frame` | the frozen `time_frame` no longer covers the window (older than 7 days); non-legacy partitions are marked `failed` | `acquisition.py:552-560` |

Any of the provider or budget stops also ends the remaining slots of the cycle
(`runner.py:246-249`).

### Retry behavior

The client can retry 408/5xx and network errors (`fantastic.py:190-210`). In
production a spend budget is always set, so retries are **off**. Each page is exactly
one physical request.

`tgtc_core/runner.py:105-107`
```python
self.fantastic = FantasticClient(fantastic_transport, ..., max_retries=0 if self.spend_budget else 2)
```

A failed page is retried only by a later slot, with a new budget reservation.

### How already-seen rows ("no_new_ids") are handled

The current path has **no `no_new_ids` stop**. A page whose rows are all already held,
but with a different id set from the previous page, advances the offset normally. Those
rows are counted as `unchanged`. Only an identical id set counts as a duplicate page.

Measured: 0 duplicate pages in the 41 served pages up to 2026-09-17T09:00Z (the
ledger printed then). No saved ledger covers the 10 pages of 2026-09-18.

### When progress is committed ("watermark")

There is no global watermark. The page's rows, its receipt and the offset advance are
written in **one database transaction**. The write is fenced by the partition lease and
the expected offset.

`tgtc_core/services/acquisition.py:669-683`
```sql
UPDATE source_partitions SET next_offset = next_offset + %s, pages_received = pages_received + 1, ...
       state = CASE WHEN %s THEN 'complete' WHEN %s THEN 'stalled' ELSE state END, ...
WHERE id = %s AND lease_token = %s AND next_offset = %s AND state = 'open' AND lease_expires_at > %s
```

A crash before commit leaves the offset unchanged. That page may then be requested
(and billed) again, but it cannot be lost.

### Can a full duplicate page wrongly stop acquisition?

- **Rows already held but a different id set:** no. The offset advances.
- **Identical id set three times:** the partition is `stalled`, not `complete`. A later run can reopen it once the provider is not refusing (`acquisition.py:441-453`). While stalled, that window receives no pages.

### Does pagination resume across runs?

Yes. `next_offset` persists per partition, and the frozen query keeps the parameters
identical. Consequences we cannot verify without you:

- A saved offset is reused hours or days later inside a fixed `date_created` window. That is only safe if the provider's ordering and membership for that window are stable. We send no sort parameter.
- The feed is `time_frame=7d`. Once a window is older than 7 days, its unfinished partitions fail. The code would pick `time_frame=6m` for a partition whose **first** request happens after that point. Every core request so far used `7d` (`current_requests_redacted.json`).
- The two profiles page the same windows independently. Discovery re-buys rows that priority already bought: 272 of 600 through 2026-09-17.

---

## B. Legacy path (`fantastic_jobs_adapter.py` @ `c30178d`, paused)

- **Window:** one `[lower, upper)` `date_created` window per run.
  - `upper = now - lag`, where the lag defaults to 180 minutes.
  - `lower` is the previous watermark minus a 60-minute overlap, clamped to `now - 7d` by `time_frame=7d`.
  - The window is reused by later runs until every source drains it.
- **Slices:** the window is cut into 6-hour slices, oldest first (`L2667`, `FANTASTIC_WINDOW_SLICE_HOURS` default 6).

### Next page, page size, offset

`fantastic_jobs_adapter.py:1526, 1549`
```python
want = min(cap - returned, 100)
...
params.update({"limit": want, "offset": start_offset + returned})
```

`offset` advances by the rows returned and restarts at 0 in each slice. The last page
of a budget grant can be smaller than 100.

The page cap is `FANTASTIC_JOBS_MAX_PAGES_PER_SEGMENT`: default 50, and 70 on the legacy
service when read on 2026-09-18.

### Stop conditions

| stop | code line |
|---|---|
| `empty_page` | 1571 |
| `repeated_page` (identical id tuple seen earlier in the same pass) | 1580 |
| `cap_reached` (run/segment grant used with a full page) | 1691 |
| `short_page` (fewer rows than `want`) | 1694 |
| `no_new_ids` (a full page of already-held rows; **only** when there is no durable cursor) | 1712-1713 |
| `duplicate_page_cap` (40 consecutive all-held pages, `FANTASTIC_MAX_CONSECUTIVE_DUPLICATE_PAGES`) | 1718 |
| `page_cap` | 1724 |

### Retries

Inside one request (`_request`, lines 869-910):
- Network errors and 408/5xx are retried with backoff. `FANTASTIC_JOBS_MAX_RETRIES` defaults to 2, so there are up to 3 attempts, with a 30 s timeout each.
- 401/403 and 429 are raised immediately.

A request that still fails stops that source segment for the run, with `request_error`.
Other segments continue.

### How `no_new_ids` behaved

On 2026-09-05 there was no saved offset yet. The run re-paged the window from 0, found
only held rows on page 1, stopped `no_new_ids`, and billed 200 rows for 0 net-new. After
a durable offset was added, the 2026-09-06 run resumed at offset 100 and paged to 2,822.
It found 226 new rows on LinkedIn and 0 on ATS, stopping `cap_reached`.

### Watermark commit

`fantastic_jobs_adapter.py:3051-3082`: the watermark advances **only** when the whole
window drained, and only after the postings were saved. A truncated window stays in
place and is replayed by the next run.

```python
if not bool(st.get("window_drained", False)):
    # the window was TRUNCATED (cap / quota reserve / page_cap / error).
    # Leave the watermark where it is so the next run replays the SAME window
    return {"committed": False, "reason": "window_truncated_replay_next_run", ...}
```

This replay is how the legacy path re-requested windows it had partly read.

We also reproduced offline one request-generation defect: a 37-second change in run start
time shifted every 6-hour slice key, so completed ranges were requested again. It is
documented in `acceptance/VOLUME_RECOVERY_2026-09-08.md`. That document does **not**
attribute all 4,505 already-held rows of 2026-09-08 to it; response-level IDs were not
retained.

### Cross-run resume

Finished slices and per-slice offsets are persisted. The window is reused
(`window_reused=True` on 2026-09-06 and 2026-09-08) until it drains or ages out. On
2026-09-08 the log reports "3 slice(s) conceded over 0.86 day(s); 1 had been drained
first". Those rows aged out of the 7-day feed before the run reached them.

### Sort order

No sort parameter was sent. A code comment in `_fetch_segment` records that the
direction was observed, not requested.
