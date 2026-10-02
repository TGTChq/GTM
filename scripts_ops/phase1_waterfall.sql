\pset footer off
-- Same-cohort waterfall for ONE scheduled run. Usage:
--   psql -v run_id="'20260922T0300...'" -f phase1_waterfall.sql
-- Cohort = records bought by this run's pages. Backlog approvals from other runs and
-- outbox rows deferred on earlier days are EXCLUDED and reported separately.
DROP TABLE IF EXISTS _w;
CREATE TEMP TABLE _w AS SELECT min(created_at) AS t0, max(created_at) AS t1 FROM run_log WHERE run_id = :run_id;

DROP TABLE IF EXISTS _rows;
CREATE TEMP TABLE _rows AS
SELECT sp.source, sp.query_profile, unnest(pr.row_ids) AS provider_job_id, pr.row_count
FROM page_receipts pr JOIN source_partitions sp ON sp.id = pr.partition_id, _w
WHERE pr.received_at BETWEEN _w.t0 AND _w.t1;

DROP TABLE IF EXISTS _post;
CREATE TEMP TABLE _post AS
SELECT DISTINCT r.source, r.query_profile, p.id AS posting_id, p.employer_id, p.created_at < (SELECT t0 FROM _w) AS rebought
FROM _rows r JOIN postings p ON p.provider_job_id = r.provider_job_id;

DROP TABLE IF EXISTS _cls;
CREATE TEMP TABLE _cls AS
SELECT DISTINCT ON (c.posting_id) c.posting_id, c.excluded, c.exclusion_reason, c.compatible_functions
FROM classifications c JOIN _post p ON p.posting_id = c.posting_id ORDER BY c.posting_id, c.created_at DESC;

DROP TABLE IF EXISTS _units;
CREATE TEMP TABLE _units AS
SELECT DISTINCT o.id AS opportunity_id, o.employer_id, o.campaign_key, o.state, o.close_reason,
       o.created_at < (SELECT t0 FROM _w) AS preexisting
FROM _post p JOIN opportunity_postings op ON op.posting_id = p.posting_id JOIN opportunities o ON o.id = op.opportunity_id;

DROP TABLE IF EXISTS _appr;
CREATE TEMP TABLE _appr AS
SELECT a.* FROM approvals a JOIN _units u ON u.opportunity_id = a.opportunity_id WHERE a.run_id = :run_id;

\echo '== 1. acquisition =='
SELECT count(*) AS records_billed, (SELECT count(*) FROM _post) AS postings,
       (SELECT count(*) FROM _post WHERE rebought) AS rebought_known,
       (SELECT count(DISTINCT employer_id) FROM _post) AS employers FROM _rows;

\echo '== 2. hard exclusions by reason =='
SELECT exclusion_reason, count(*) FROM _cls WHERE excluded GROUP BY 1 ORDER BY 2 DESC;

\echo '== 3. assigned jobs by campaign function =='
SELECT fn, count(*) FROM (SELECT unnest(compatible_functions) fn FROM _cls WHERE NOT excluded) x GROUP BY 1 ORDER BY 2 DESC;

\echo '== 4. units: formed / preexisting (historical) / terminal state =='
SELECT count(*) AS units, count(*) FILTER (WHERE preexisting) AS historical_units,
       count(*) FILTER (WHERE state = 'approved') AS approved_units,
       count(*) FILTER (WHERE state = 'open') AS open_units,
       count(*) FILTER (WHERE state = 'closed') AS closed_units FROM _units;
SELECT coalesce(close_reason, state) AS outcome, count(*) FROM _units WHERE state <> 'approved' GROUP BY 1 ORDER BY 2 DESC LIMIT 15;

\echo '== 5. contacts per unit (0 / 1 / 2 / 3 approved contacts) =='
SELECT n_contacts, count(*) AS units FROM (
  SELECT u.opportunity_id, (SELECT count(*) FROM _appr a WHERE a.opportunity_id = u.opportunity_id) AS n_contacts FROM _units u) x
GROUP BY 1 ORDER BY 1;

\echo '== 6. contacts: approved / verified / outreach-eligible / unique =='
SELECT count(*) AS approvals, count(*) FILTER (WHERE lower(lead_json->>'email_status') = 'verified') AS verified,
       count(*) FILTER (WHERE outreach_eligible) AS outreach_eligible,
       count(DISTINCT person_id) AS people, count(DISTINCT lower(lead_json->>'email')) AS emails FROM _appr;

\echo '== 7. Instantly for THIS cohort =='
SELECT o.state, coalesce(o.blocked_reason, o.last_error, '') AS reason, count(*)
FROM delivery_outbox o JOIN _appr a ON a.id = o.approval_id WHERE o.channel = 'instantly' GROUP BY 1,2 ORDER BY 3 DESC;

\echo '== 8. final unique contacts by campaign (this cohort, created in Instantly) =='
SELECT a.campaign_key, count(DISTINCT lower(a.lead_json->>'email')) AS created
FROM _appr a JOIN delivery_outbox o ON o.approval_id = a.id AND o.channel = 'instantly'
JOIN delivery_receipts r ON r.outbox_id = o.id AND r.receipt_kind = 'created' GROUP BY 1 ORDER BY 2 DESC;

\echo '== 9. EXCLUDED from the cohort: backlog delivered during the run window =='
SELECT count(*) AS backlog_created_in_window FROM delivery_receipts r JOIN delivery_outbox o ON o.id = r.outbox_id
JOIN approvals a ON a.id = o.approval_id, _w
WHERE r.channel = 'instantly' AND r.receipt_kind = 'created' AND r.received_at BETWEEN _w.t0 AND _w.t1
  AND a.run_id <> :run_id;
