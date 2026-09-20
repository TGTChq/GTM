# Questions for Remco

## Main questions

1. **What exact pagination parameter should advance between requests?**
   Today we advance `offset` by the rows received, with `limit=100`. We send no `cursor` and no sort parameter.
   See `pagination_behavior.md`.

2. **Is the 24-hour window based on provider discovery/indexing time or on the job's original posting/creation time?**
   Today we filter on `date_created_gte` / `date_created_lt` and treat `date_created` as your indexing time. Some rows carry a `date_posted` more than 7 days older than their `date_created`: 233 of 4,300 postings.

3. **How should failed runs and late-indexed jobs be recovered without losing inventory?**
   Our current path keeps a saved offset per window and resumes it in later runs. The legacy path replayed whole windows, which re-bought held rows (`run_evidence.csv`, 2026-09-05 to 09-08). With one daily 24h pass, what is the recommended catch-up for a missed or partial day, and for jobs indexed just after our pass?

4. **Are provider job IDs stable across repeated calls and query partitions?**
   Our deduplication key is `(endpoint, id)`. We assume one job keeps its `id` across days, pages and filter sets, and that `exclude_ats_duplicate=true` removes the active-jb twin of an active-ats job.

5. **Is ordering stable during pagination?**
   We send no sort parameter. Is the default order stable within one pass, and across hours or days for the same `date_created` window? Our current code resumes a saved offset hours or days later.

6. **Are there per-query, per-run, daily, or dataset-level result caps?**
   - Our plan headers show `x-api-jobs-limit=100000` and `x-api-requests-limit=50000`.
   - We have seen HTTP 504 "count timed out" on a 5.3-day window with a long title expression.
   - Is there any cap on offset depth or on rows per query?

7. **Which filters should be applied upstream versus downstream to maximize recall?**
   - Today's two filter sets are in `current_requests_redacted.json`.
   - We need companies with unknown headcount or industry kept. Do `organization_headcount_gte/lt`, `exclude_organization_industry` and `ai_employment_type=FULL_TIME` drop rows whose field is null? We saw this drop 100% of Wellfound and Y Combinator rows on 2026-09-04.

8. **What query partitioning and provider budget would you recommend for at least 1,000 new approved job-level opportunities per day after our downstream rules?**
   - Of 4,300 postings reviewed, 25.7% passed job-level qualification. 30.9% were off-portfolio work: nursing, driving, technicians and similar.
   - Our functions are AI/engineering/automation, GTM/revenue and sales operations, marketing and creative, and customer success and support.
   - Should we add title or taxonomy seeds upstream? Is one pass per endpoint enough, or should we partition further?

## Short follow-ups from the evidence

9. Is `offset` paging valid with `time_frame=6m`? Our code would pick `6m` for a window older than 7 days (never used so far).
10. Which parameters are valid per endpoint? `include_basic_organization_details` returned HTTP 400 on `/v1/active-jb`.
11. `ai_employment_type` can hold several labels, e.g. `["FULL_TIME","PART_TIME"]`. Does the `FULL_TIME` filter match any overlap? 41 such postings reached us and were rejected.
12. What does `org_linkedin_headcount` measure compared with `org_linkedin_size`? 13 of 141 size rejections contradict the size band, and one record has a null headcount but a "2-10 employees" band.
13. On job-board postings published by a third party, which field names the actual hiring employer? Known case: a public-power association's job board listing a member cooperative's job.
14. Is `location_type=TELECOMMUTE` the only remote signal on these endpoints? `remote_derived` was null on every row we stored.
15. Is every row billed, including rows returned again on a repeated page? We read `x-api-jobs-this-request`.
