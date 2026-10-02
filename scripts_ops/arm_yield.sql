\pset footer off
-- Yield by acquisition arm for the first full production run (budget prod-core-20260921).
WITH pages AS (
  SELECT pr.id, sp.query_profile, pr.row_count, unnest(pr.row_ids) AS provider_job_id
  FROM page_receipts pr JOIN source_partitions sp ON sp.id = pr.partition_id
  WHERE pr.received_at >= '2026-09-21 03:32:53+00' AND pr.received_at <= '2026-09-21 04:52:40+00'
),
billed AS (
  SELECT sp.query_profile, sum(pr.row_count) AS records
  FROM page_receipts pr JOIN source_partitions sp ON sp.id = pr.partition_id
  WHERE pr.received_at >= '2026-09-21 03:32:53+00' AND pr.received_at <= '2026-09-21 04:52:40+00'
  GROUP BY 1
),
arm_postings AS (
  SELECT DISTINCT pg.query_profile, p.id AS posting_id
  FROM pages pg JOIN postings p ON p.provider_job_id = pg.provider_job_id
),
cls AS (
  SELECT ap.query_profile, ap.posting_id, c.excluded, c.exclusion_reason
  FROM arm_postings ap
  JOIN LATERAL (SELECT excluded, exclusion_reason FROM classifications c WHERE c.posting_id = ap.posting_id
                ORDER BY c.created_at DESC LIMIT 1) c ON true
),
appr AS (
  SELECT ap.query_profile, a.id, a.outreach_eligible
  FROM arm_postings ap JOIN approvals a ON (a.lead_json->>'posting_id')::bigint = ap.posting_id
  WHERE a.run_id = '20260921T033253.666863Z-6c015841'
)
SELECT b.query_profile,
       b.records AS billed_records,
       (SELECT count(*) FROM arm_postings x WHERE x.query_profile = b.query_profile) AS postings,
       (SELECT count(*) FROM cls x WHERE x.query_profile = b.query_profile AND NOT x.excluded) AS assigned,
       (SELECT count(*) FROM cls x WHERE x.query_profile = b.query_profile AND x.excluded AND x.exclusion_reason LIKE 'employment:%') AS excl_employment,
       (SELECT count(*) FROM cls x WHERE x.query_profile = b.query_profile AND x.excluded AND x.exclusion_reason LIKE 'deliverability:%') AS excl_deliverability,
       (SELECT count(*) FROM cls x WHERE x.query_profile = b.query_profile AND x.excluded) AS excluded_total,
       (SELECT count(*) FROM appr x WHERE x.query_profile = b.query_profile) AS approvals,
       (SELECT count(*) FROM appr x WHERE x.query_profile = b.query_profile AND x.outreach_eligible) AS eligible
FROM billed b ORDER BY 1;
