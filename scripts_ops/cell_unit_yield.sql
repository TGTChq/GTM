\pset footer off
-- Unit yield by acquisition cell (source x profile) for run 20260921T033253.666863Z-6c015841.
-- A "unit" is one (employer, function) opportunity -- one outreach account.
DROP TABLE IF EXISTS _cell_rows;
CREATE TEMP TABLE _cell_rows AS
SELECT sp.source, sp.query_profile, pr.id AS receipt_id, unnest(pr.row_ids) AS provider_job_id
FROM page_receipts pr JOIN source_partitions sp ON sp.id = pr.partition_id
WHERE pr.received_at >= '2026-09-21 03:32:53+00' AND pr.received_at <= '2026-09-21 04:52:40+00';

DROP TABLE IF EXISTS _cell_postings;
CREATE TEMP TABLE _cell_postings AS
SELECT DISTINCT r.source, r.query_profile, p.id AS posting_id, p.employer_id,
       (p.created_at < '2026-09-21 03:32:53+00') AS already_known
FROM _cell_rows r JOIN postings p ON p.provider_job_id = r.provider_job_id;

DROP TABLE IF EXISTS _cell_units;
CREATE TEMP TABLE _cell_units AS
SELECT DISTINCT cp.source, cp.query_profile, o.id AS opportunity_id, o.employer_id, o.function_key,
       (o.created_at < '2026-09-21 03:32:53+00') AS unit_preexisting, o.state, o.close_reason
FROM _cell_postings cp
JOIN opportunity_postings op ON op.posting_id = cp.posting_id
JOIN opportunities o ON o.id = op.opportunity_id;

\echo '== per cell: records -> postings -> employers -> units -> contacts =='
SELECT c.source, c.query_profile,
  (SELECT count(*) FROM _cell_rows x WHERE x.source=c.source AND x.query_profile=c.query_profile) AS records,
  (SELECT count(*) FROM _cell_postings x WHERE x.source=c.source AND x.query_profile=c.query_profile AND x.already_known) AS rebought_known,
  (SELECT count(*) FROM _cell_postings x WHERE x.source=c.source AND x.query_profile=c.query_profile AND NOT x.already_known) AS new_postings,
  (SELECT count(DISTINCT employer_id) FROM _cell_postings x WHERE x.source=c.source AND x.query_profile=c.query_profile) AS employers,
  (SELECT count(*) FROM _cell_units x WHERE x.source=c.source AND x.query_profile=c.query_profile) AS units,
  (SELECT count(*) FROM _cell_units x WHERE x.source=c.source AND x.query_profile=c.query_profile AND NOT x.unit_preexisting) AS new_units,
  (SELECT count(*) FROM _cell_units x WHERE x.source=c.source AND x.query_profile=c.query_profile AND x.close_reason LIKE 'employer_too_%') AS size_closed_units,
  (SELECT count(DISTINCT a.opportunity_id) FROM _cell_units x JOIN approvals a ON a.opportunity_id = x.opportunity_id
     WHERE x.source=c.source AND x.query_profile=c.query_profile AND a.run_id='20260921T033253.666863Z-6c015841') AS units_with_contact,
  (SELECT count(*) FROM _cell_units x JOIN approvals a ON a.opportunity_id = x.opportunity_id
     WHERE x.source=c.source AND x.query_profile=c.query_profile AND a.run_id='20260921T033253.666863Z-6c015841' AND a.outreach_eligible) AS eligible_contacts
FROM (SELECT DISTINCT source, query_profile FROM _cell_rows) c ORDER BY 1,2;

\echo '== employer concentration across the run: postings per employer =='
SELECT bucket, count(*) AS employers, sum(n) AS postings FROM (
  SELECT employer_id, count(*) AS n,
         CASE WHEN count(*) = 1 THEN '1' WHEN count(*) <= 3 THEN '2-3' WHEN count(*) <= 10 THEN '4-10' ELSE '11+' END AS bucket
  FROM _cell_postings GROUP BY employer_id) e GROUP BY bucket ORDER BY min(n);

\echo '== postings per unit (duplicate openings inside one company x campaign) =='
SELECT bucket, count(*) AS units FROM (
  SELECT op.opportunity_id, count(*) AS n,
    CASE WHEN count(*) = 1 THEN '1' WHEN count(*) <= 3 THEN '2-3' ELSE '4+' END AS bucket
  FROM opportunity_postings op JOIN _cell_postings cp ON cp.posting_id = op.posting_id GROUP BY 1) u
GROUP BY bucket ORDER BY bucket;

\echo '== unit terminal outcomes (run cohort) =='
SELECT state, coalesce(close_reason, '') AS close_reason, count(*) FROM (SELECT DISTINCT opportunity_id, state, close_reason FROM _cell_units) u
GROUP BY 1,2 ORDER BY 3 DESC LIMIT 20;
