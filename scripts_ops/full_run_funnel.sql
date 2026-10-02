\pset footer off
\echo '== provider consumption (budget prod-core-20260921) =='
SELECT provider, status, count(*) AS requests, coalesce(sum(estimated_credits),0) AS credits,
       coalesce(sum(input_tokens_used),0) AS in_tok, coalesce(sum(output_tokens_used),0) AS out_tok
FROM spend_reservations WHERE budget_id = 'prod-core-20260921' GROUP BY 1,2 ORDER BY 1,2;

\echo '== acquisition: postings created in the run window =='
SELECT count(*) AS new_postings, count(DISTINCT employer_id) AS employers FROM postings
WHERE created_at >= '2026-09-21 03:32:53+00' AND created_at <= '2026-09-21 04:52:40+00';

\echo '== classifications in the run window: excluded vs assigned =='
SELECT excluded, count(*) FROM classifications
WHERE created_at >= '2026-09-21 03:32:53+00' AND created_at <= '2026-09-21 04:52:40+00' GROUP BY 1;

\echo '== hard exclusions by reason (classification) =='
SELECT exclusion_reason, count(*) FROM classifications
WHERE excluded AND created_at >= '2026-09-21 03:32:53+00' AND created_at <= '2026-09-21 04:52:40+00'
GROUP BY 1 ORDER BY 2 DESC LIMIT 20;

\echo '== assigned jobs by function =='
SELECT fn, count(*) FROM (SELECT unnest(compatible_functions) AS fn FROM classifications
  WHERE NOT excluded AND created_at >= '2026-09-21 03:32:53+00' AND created_at <= '2026-09-21 04:52:40+00') x
GROUP BY 1 ORDER BY 2 DESC;

\echo '== opportunity closures in the run window (qualify stage) =='
SELECT close_reason, count(*) FROM opportunities
WHERE state = 'closed' AND updated_at >= '2026-09-21 03:32:53+00' AND updated_at <= '2026-09-21 04:52:40+00'
GROUP BY 1 ORDER BY 2 DESC LIMIT 20;

\echo '== approvals attributed to the run =='
SELECT count(*) AS approvals, count(DISTINCT person_id) AS people, count(DISTINCT lower(lead_json->>'email')) AS emails,
       count(DISTINCT employer_id) AS employers, count(DISTINCT employer_id::text || ':' || campaign_key) AS company_campaign_units,
       count(*) FILTER (WHERE outreach_eligible) AS outreach_eligible,
       count(*) FILTER (WHERE lower(coalesce(lead_json->>'email_status','')) = 'verified') AS verified_email
FROM approvals WHERE run_id = '20260921T033253.666863Z-6c015841';

\echo '== approvals by campaign (run) =='
SELECT campaign_key, count(*) AS approvals, count(*) FILTER (WHERE outreach_eligible) AS eligible
FROM approvals WHERE run_id = '20260921T033253.666863Z-6c015841' GROUP BY 1 ORDER BY 2 DESC;

\echo '== instantly outbox for the run approvals: state x reason =='
SELECT o.state, coalesce(o.blocked_reason, o.last_error, '') AS reason, count(*)
FROM delivery_outbox o JOIN approvals a ON a.id = o.approval_id
WHERE o.channel = 'instantly' AND a.run_id = '20260921T033253.666863Z-6c015841' GROUP BY 1,2 ORDER BY 3 DESC;

\echo '== instantly receipts in the run window, by kind =='
SELECT r.receipt_kind, count(*) FROM delivery_receipts r JOIN delivery_outbox o ON o.id = r.outbox_id
WHERE o.channel = 'instantly' AND r.created_at >= '2026-09-21 03:32:53+00' AND r.created_at <= '2026-09-21 04:52:40+00'
GROUP BY 1 ORDER BY 2 DESC;

\echo '== instantly delivered in the run window, by campaign (exactly-once check) =='
SELECT a.campaign_key, a.campaign_id, count(*) AS delivered, count(DISTINCT a.person_id) AS people,
       count(DISTINCT lower(a.lead_json->>'email')) AS emails
FROM delivery_outbox o JOIN approvals a ON a.id = o.approval_id
WHERE o.channel = 'instantly' AND o.state = 'delivered'
  AND o.updated_at >= '2026-09-21 03:32:53+00' AND o.updated_at <= '2026-09-21 04:52:40+00'
GROUP BY 1,2 ORDER BY 3 DESC;

\echo '== control ids ever delivered in the run window (must be 0) =='
SELECT count(*) AS control_deliveries FROM delivery_outbox o JOIN approvals a ON a.id = o.approval_id
WHERE o.channel = 'instantly' AND o.state = 'delivered'
  AND o.updated_at >= '2026-09-21 03:32:53+00'
  AND a.campaign_id IN ('45ac1e03-67e7-4bdd-b372-808042104e4c','4effab2f-9073-46a9-b7ae-986ccc8f49c6',
    '1db88bbe-b2cf-4574-a5b7-1cb948151a86','cf01e56b-e5ad-489e-a02c-c35c93cf3b53','0f0f57d5-fab1-436d-b0d8-8cb43b031f03',
    '1747c87e-12e9-4477-bc4d-048223d39513','165c9e87-c3e7-4e9c-9ccb-a8dbf5779726','917973f3-c282-4a84-8da4-525a7a91819b',
    '04670c6a-828b-42cd-9dad-904592a63d9b');

\echo '== duplicate emails across ALL instantly delivered rows (must be 0) =='
SELECT count(*) AS duplicate_emails FROM (
  SELECT lower(a.lead_json->>'email') e, a.campaign_id, count(*) FROM delivery_outbox o JOIN approvals a ON a.id = o.approval_id
  WHERE o.channel = 'instantly' AND o.state = 'delivered' GROUP BY 1,2 HAVING count(*) > 1) d;

\echo '== work queue terminal states (every input ends in a reason) =='
SELECT kind, state, count(*) FROM work_items GROUP BY 1,2 ORDER BY 1,2;
