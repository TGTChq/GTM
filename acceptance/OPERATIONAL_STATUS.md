# Operational status of each capability

Read back from the running services, not from the code. A capability that is
implemented but disabled, unfunded or unreachable is **not** operational and is not
counted as improving production.

Effective values read from GTM and GTM Approved Sync on 2026-09-06.

| capability | flag / setting | effective | operational? |
|---|---|---|---|
| Paid acquisition (Fantastic) | `FANTASTIC_JOBS_ENABLED` | **False** | **PAUSED** — deliberate containment while Apollo is refusing |
| Window slice cursor | `FANTASTIC_WINDOW_SLICING_ENABLED` / `..._SLICE_HOURS` | True / 6 | **ON**, but unexercised in production — acquisition is paused. Validated offline only |
| Custody of paid-for work | `PENDING_WORK_ENABLED` | True | **ON**; exercised by the production maintenance pass |
| Send-safe auto-approval | `FANTASTIC_AUTO_APPROVE_SEND_SAFE` | True (both services) | **ON** — verified on the real field builder |
| Airtable function suppression | `AIRTABLE_SUPPRESS_EXISTING_COMPANY_FUNCTION` | True | **ON** — one active row per company × role bucket, cross-run |
| Account-level suppression | `AIRTABLE_SUPPRESS_ACCOUNT_LEVEL` | False | off by design |
| Enrollment person/employer dedupe | `ENROLLMENT_PERSON_EMPLOYER_UNIQUENESS` | True (both) | **ON** — one enrollment per person-employer PAIR; does not cap an employer |
| Alternate-contact cascade | `ALTERNATE_CONTACT_CASCADE_ENABLED` | True (GTM) / False (Sync) | **ON in GTM**, but cannot run while Apollo refuses |
| Org-ID zero-people fallback | `APOLLO_ORG_ID_ZERO_PEOPLE_FALLBACK_ENABLED` | True | **PARTIAL** — the 0-credit recovery runs, but paid enrichment of recovered buckets is deferred: `APOLLO_ORG_ID_FALLBACK_MAX_PAID_MATCHES_PER_RUN = 0`. Never yet exercised (Apollo refused before the stage was reached) |
| Overall paid-match ceiling | `APOLLO_MAX_PERSON_MATCH_CALLS_PER_RUN` | 0 | off by design — it would limit already-authorized work |
| Functional discovery | `FANTASTIC_FUNCTIONAL_DISCOVERY_ENABLED` | **False** | **NOT OPERATIONAL.** Implemented and tested; classification integration verified; live incremental yield unmeasured |
| Historical recovery (6m cursor) | `FANTASTIC_HISTORICAL_RECOVERY_ENABLED` / `..._MAX_ROWS_PER_RUN` | **False / 0** | **NOT OPERATIONAL.** Implemented and offline-verified; needs a flag *and* a row budget, neither granted |
| Direct ATS boards (145) | `ATS_DIRECT_ACQUISITION_ENABLED` = True | — | **NOT OPERATIONAL.** The lane is built only `if "ats" in lanes`, and the start command passes `--lanes fantastic`. Registered ≠ active |
| Wellfound / Y Combinator | enabled | — | **ON**, but reported `already_drained_this_window` on 2026-09-06, so they contributed nothing that day |
| Weekly report → Slack | start command `--slack --if-due friday` | — | **ON**; next due Friday 2026-09-11 |
| Maintenance mode | `MAINTENANCE_ONLY` | set to 1 for the passes on 2026-09-06 | **must be returned to 0** before normal acquisition resumes |

## What that means for lead volume today

Nothing downstream of acquisition can run: Apollo refuses every credit-consuming
call, so contact discovery, the alternate cascade and the org-ID fallback are all
inert regardless of their flags. Two acquisition paths that could add inventory —
functional discovery and the 145 direct ATS boards — are switched off, and the
historical backfill has no budget.

None of those are claimed as contributing to production output.


## One transition cost, so it is not a surprise

The window in flight (`2026-08-30T03:03:20Z` → `2026-09-04T10:02:11Z`) was paged by
the OFFSET cursor and is largely acquired already, but nothing records WHICH
`date_created` ranges were covered — an offset cannot be translated into slices.

So if acquisition were resumed **while that window is still open**, the slice cursor
would re-page it slice by slice and `seen_ids` would dedupe essentially all of it:
one window's worth of spend for near-zero net-new. The run summary would show it
plainly (`cursor: date_created slices`, slices drained, `kept` ≈ 0).

It resolves itself if acquisition stays paused. The window's upper bound is
2026-09-04T10:02Z, so once the 7-day frame floor passes it — **2026-09-11T10:02Z** —
the window is unreachable, `open()` starts a fresh one, and the slice cursor begins
on a window it owns from the first request. Apollo's own next billing date is
2026-09-18, so on the current trajectory the transition is free.

If acquisition must resume sooner, the cheapest options are to accept the one-time
re-walk (bounded by the governor) or to run with
`FANTASTIC_WINDOW_SLICING_ENABLED=0` until the window expires. Both are fine; the
first is simpler and self-correcting.
