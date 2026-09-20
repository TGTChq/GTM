# TGTC × Fantastic.jobs: technical support bundle (2026-09-18)

This bundle is for Remco at Fantastic.jobs. It describes how TGTC calls the Fantastic
API today, what we observe, and what we would like to change. We are asking for a
recommended setup that reliably maximizes net-new jobs.

No Fantastic, Apify or other API was called to build this bundle. Every number comes
from our code, saved logs or saved exports. Where a value is not in that evidence, the
file says `not_recorded` instead of estimating it.

## Business target

**At least 1,000 new approved job-level opportunities per day in total, across all runs.**
"Approved" means the job passed TGTC's downstream rules:
- a US hiring company with 25–1,000 employees;
- responsibilities that fit one of our offer groups;
- a full-time, active opening;
- a verified buyer contact.

## Current observed output

- **Previous pipeline (paused since 2026-09-09):** its productive runs each wrote between 448 and 1,087 contact-level Airtable rows: 1,087 (2026-08-18), 647 (2026-08-21), 781 (2026-09-04) and 448 (2026-09-08). Most other runs in that period wrote 0–30 rows.
- **These rows are not approved job-level opportunities.** Each row holds one contact, and not every row is in Approved status.
- **Current approved job-level throughput has not been reliably reconciled.**
- **The 2026-09-18 run produced zero** because of an internal TGTC configuration condition. Provider exhaustion was not proven.

See `run_evidence.csv`.

## Current acquisition approach

Current path (`tgtc_core`, commit `be3af32`, deployed 2026-09-18):
- **Endpoints:**
  - `/v1/active-jb`, all job boards including LinkedIn, sent with `exclude_ats_duplicate=true`.
  - `/v1/active-ats`, sent with `include_basic_organization_details=true`.
- **Window:** a repeated **`time_frame=7d`** on every request, plus **`date_created_gte` / `date_created_lt`** windows:
  - 1-hour "fresh" windows that end at least 3 hours before the run;
  - 24-hour "backfill" windows that walk backwards.
- **Pagination:**
  - `offset` paging inside each window, with `limit=100`.
  - Each run spends one page per slot and 10 slots per cycle.
  - The offset of each window is saved and resumed in later runs.
- **Two query profiles on the same windows:**
  - "priority" (8 of 10 pages): `ai_employment_type=FULL_TIME`, headcount 25–1,000, 16 taxonomies, 16-industry exclusion and agency exclusion.
  - "discovery" (2 of 10 pages): industry and agency exclusion only.
- **No title filter** is sent today.
- **TGTC-side budget:** the run command allows 1,000 job rows per 24 hours.
- **Provider plan headers observed:** `jobs_limit=100000`, `requests_limit=50000`.

Previous path (legacy, paused since 2026-09-09):
- one reused 7-day `date_created` window per run, cut into 6-hour slices;
- per-source segments for ATS, LinkedIn, Wellfound and Y Combinator;
- a 4,222-character `title_advanced` expression.

Exact parameters for both paths are in `current_requests_redacted.json`.

## Symptoms

1. **Historical repetition.** The legacy runs re-bought rows they already held:

   | Date | Rows already held / rows billed |
   |---|---|
   | 2026-09-05 | 200 / 200 |
   | 2026-09-06 | 5,218 / 5,444 |
   | 2026-09-07 | 172 / 500 |
   | 2026-09-08 | 4,505 / 5,144 |

2. **Overlap across queries and runs.** On the current path, the discovery query returned 600 rows. Priority queries over the same windows had already bought 272 of them. In the latest run, 163 of 915 returned rows were already held.
3. **Pagination uncertainty.** We resume a saved `offset` hours or days later inside a fixed `date_created` window. We send no sort parameter. We do not know whether ordering is stable enough for that.
   - The legacy path carried an offset across a window whose 7-day floor had moved (2026-09-06).
   - The first core request failed HTTP 400 because a parameter was sent to the wrong endpoint.
4. **Result caps.**
   - On 2026-09-06 both legacy sources stopped on `cap_reached` after 28 pages each. 5,218 of the 5,444 rows billed were already held.
   - The count endpoint timed out (HTTP 504) on a 5.3-day window with the long title expression.
   - Our own per-run row budget is small (1,000 rows per 24 h today).
5. **High downstream rejection.**
   - Of 4,300 postings reviewed by the current path, 3,195 (74.3%) were rejected at job level.
   - 1,327 (30.9%) of all reviewed postings were rejected because their responsibilities fit none of our functions: nurses, drivers, technicians and similar.
   - Only 1,105 (25.7%) passed job-level qualification.

   Most rejections come from TGTC business rules, not provider errors. `rejection_breakdown.csv` separates the two. TGTC business rules may still change, so please mark any recommendation that depends on a specific rule.

## Remco's recommendation, which we want to confirm

> Run once daily during the same hour using `time_frame=24h`, continuing pagination
> until a page returns fewer records than the requested limit.

`proposed_24h_request.json` shows our current requests rewritten that way, as a DRAFT.
Fields we cannot confirm ourselves are marked `NEEDS_PROVIDER_CONFIRMATION`.

## Files

| File | Contents |
|---|---|
| `current_requests_redacted.json` | Request parameters by query variant (current and legacy), two verbatim persisted requests, observed request shapes |
| `pagination_behavior.md` | How our code pages, stops, retries, resumes and commits progress, with short code excerpts |
| `run_evidence.csv` | One row per page, source, profile or run, at the finest granularity the saved evidence allows, plus provider count-endpoint totals |
| `rejection_breakdown.csv` | Downstream rejections split into provider/acquisition issues and TGTC business rules |
| `sample_records_redacted.json` | Real provider records (IDs, URLs, titles, companies, dates, provider fields) and how TGTC treated them |
| `proposed_24h_request.json` | DRAFT 24h configuration for review |
| `questions_for_remco.md` | Our questions |
| `REDACTION_REPORT.md` | What was checked and removed before sharing |

## Evidence conventions

- All times are UTC.
- `not_recorded` means the saved evidence does not contain the value.
- Rows marked "derived" are the difference between two saved snapshots; each such row says so.
- Record-level samples of rows repeated across pages or runs are **not** available in saved evidence. The legacy path did not retain response-level IDs. The aggregate counts are in `run_evidence.csv`.
- The bundle excludes API keys, headers, environment files, contact names, emails, phone numbers and job descriptions.
