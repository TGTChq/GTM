\pset footer off
-- Complete reconciliation of one scheduled production day. Read-only.
-- Usage: psql -v day="'2026-09-22'" -f post_run_reconcile.sql
-- Challenger campaign ids (the only valid Instantly destinations):
-- (a psql variable, not a temp table: the session is read-only)
\set ch '(VALUES (''7b9aa5f3-fe46-49fa-b2ac-fee1da346ed0'',''PRODUCT''), (''69def27c-7799-41a2-9ba8-205e54ab071b'',''OPERATIONS''),  (''7b319c7a-cc55-4e08-8a47-7058c345d8ae'',''FINANCE''), (''d2326028-e312-405b-9e16-526bd309d4dd'',''PEOPLE_HR''),  (''c3e81c21-db44-40f5-addc-d9945a78394b'',''ECOMMERCE''), (''269cd138-00b1-48c3-9093-16c36120a20e'',''CUSTOMER_EXPERIENCE''),  (''1feb6344-6065-49d7-9764-d125985fb9c9'',''MARKETING_CREATIVE''), (''8f25abd5-568a-4e88-b310-9acf85161c6c'',''GTM_SYSTEMS''),  (''8bfa0769-4b9a-4346-8e93-17ac8b726dce'',''AI_TECHNICAL'')) AS ch(campaign_id, name)'

\echo == 1. runs that day ==
SELECT run_id, min(created_at) AS started, max(created_at) AS last_event,
       max(CASE WHEN stage='target' AND event='end' THEN left(details::text, 300) END) AS target_end
FROM run_log WHERE created_at >= :day::date AND created_at < :day::date + 1 GROUP BY 1 ORDER BY 2;

\echo == 2. demand: approvals that day by campaign (eligible = verified, compliant, US) ==
SELECT coalesce(ch.name, a.campaign_id) AS campaign, count(*) AS approvals,
       count(*) FILTER (WHERE a.outreach_eligible) AS eligible,
       count(*) FILTER (WHERE NOT a.outreach_eligible) AS not_eligible
FROM approvals a LEFT JOIN :ch ON ch.campaign_id = a.campaign_id
WHERE a.approved_at >= :day::date AND a.approved_at < :day::date + 1 GROUP BY 1 ORDER BY 3 DESC;

\echo == 3. added to Instantly that day (created receipts), by campaign ==
SELECT coalesce(ch.name, r.external_campaign) AS campaign, count(*) AS created,
       count(DISTINCT lower(o.payload_json->>'email')) AS distinct_emails
FROM delivery_receipts r JOIN delivery_outbox o ON o.id = r.outbox_id LEFT JOIN :ch ON ch.campaign_id = r.external_campaign
WHERE r.channel = 'instantly' AND r.receipt_kind = 'created' AND r.received_at >= :day::date AND r.received_at < :day::date + 1
GROUP BY 1 ORDER BY 2 DESC;

\echo == 4. SAFETY (all must be 0) ==
SELECT
 (SELECT count(*) FROM delivery_receipts r WHERE r.channel='instantly' AND r.receipt_kind='created'
    AND r.received_at >= :day::date AND r.received_at < :day::date + 1
    AND r.external_campaign NOT IN (SELECT campaign_id FROM :ch)) AS created_outside_challenger,
 (SELECT count(*) - count(DISTINCT lower(o.payload_json->>'email')) FROM delivery_receipts r JOIN delivery_outbox o ON o.id=r.outbox_id
    WHERE r.channel='instantly' AND r.receipt_kind='created' AND r.received_at >= :day::date AND r.received_at < :day::date + 1) AS duplicate_emails_that_day,
 (SELECT count(*) FROM delivery_receipts r JOIN delivery_outbox o ON o.id=r.outbox_id
    WHERE r.channel='instantly' AND r.receipt_kind='created' AND r.received_at >= :day::date AND r.received_at < :day::date + 1
      AND EXISTS (SELECT 1 FROM delivery_receipts r2 JOIN delivery_outbox o2 ON o2.id=r2.outbox_id
                  WHERE r2.channel='instantly' AND r2.receipt_kind='created' AND r2.received_at < :day::date
                    AND lower(o2.payload_json->>'email') = lower(o.payload_json->>'email'))) AS emails_created_on_an_earlier_day,
 (SELECT coalesce(max(n),0) FROM (SELECT o.id, count(*) FILTER (WHERE r.receipt_kind IN ('created','existing','reconciled')) n
    FROM delivery_outbox o JOIN delivery_receipts r ON r.outbox_id=o.id WHERE o.channel='instantly' GROUP BY o.id) x) - 1 AS extra_terminal_receipts,
 (SELECT count(*) FROM delivery_outbox o WHERE o.channel='instantly' AND o.state IN ('claimed','in_flight')) AS instantly_rows_mid_flight,
 (SELECT count(*) FROM delivery_outbox o JOIN approvals a ON a.id=o.approval_id WHERE o.channel='instantly'
    AND o.state IN ('pending','claimed','in_flight','delivered') AND a.campaign_id NOT IN (SELECT campaign_id FROM :ch)) AS rows_bound_for_non_challenger;

\echo == 5. pipeline backlog now: Instantly rows not yet written, by campaign and reason ==
SELECT coalesce(ch.name, a.campaign_id) AS campaign, o.state, left(coalesce(o.last_error, o.blocked_reason, ''), 40) AS reason, count(*)
FROM delivery_outbox o JOIN approvals a ON a.id = o.approval_id LEFT JOIN :ch ON ch.campaign_id = a.campaign_id
WHERE o.channel = 'instantly' AND o.state IN ('pending','failed','claimed','in_flight') GROUP BY 1,2,3 ORDER BY 1,4 DESC;

\echo == 6. Apollo that day: reservations by status ==
SELECT status, count(*) AS requests, coalesce(sum(estimated_credits),0) AS credits FROM spend_reservations
WHERE provider='apollo' AND created_at >= :day::date AND created_at < :day::date + 1 GROUP BY 1 ORDER BY 1;
\echo == 6b. Apollo refusals by error code, provider state, rolling 30d credits ==
SELECT error_class, error_code, count(*) FROM request_attempts WHERE provider='apollo' AND status='refused'
  AND started_at >= :day::date AND started_at < :day::date + 1 GROUP BY 1,2;
SELECT provider, state, refusing_since, last_error_code, consecutive_refusals FROM provider_state WHERE provider IN ('apollo','fantastic');
SELECT coalesce(sum(estimated_credits),0) AS apollo_credits_rolling_30d FROM spend_reservations
WHERE provider='apollo' AND status <> 'refused' AND created_at > now() - interval '30 days';
\echo == 6c. opportunities waiting on Apollo (kept for retry) ==
SELECT left(waiting_on, 40) AS waiting_on, count(*) FROM work_items WHERE state='waiting' AND waiting_on LIKE 'apollo%' GROUP BY 1;

\echo == 7. Fantastic that day ==
SELECT status, count(*) AS requests, coalesce(sum(estimated_credits),0) AS records FROM spend_reservations
WHERE provider='fantastic' AND created_at >= :day::date AND created_at < :day::date + 1 GROUP BY 1;

\echo == 8. PLANNER INPUT: campaign_id|eligible_that_day|pending_total|pending_carried_from_earlier_days ==
-- demand already contains the day's deferred rows; only the carried part is backlog.
SELECT 'DEMAND|' || ch.campaign_id || '|' ||
  (SELECT count(*) FROM approvals a WHERE a.campaign_id = ch.campaign_id AND a.outreach_eligible
     AND a.approved_at >= :day::date AND a.approved_at < :day::date + 1) || '|' ||
  (SELECT count(*) FROM delivery_outbox o JOIN approvals a ON a.id = o.approval_id
     WHERE o.channel='instantly' AND a.campaign_id = ch.campaign_id AND o.state IN ('pending','failed')) || '|' ||
  (SELECT count(*) FROM delivery_outbox o JOIN approvals a ON a.id = o.approval_id
     WHERE o.channel='instantly' AND a.campaign_id = ch.campaign_id AND o.state IN ('pending','failed')
       AND a.approved_at < :day::date)
FROM :ch ORDER BY ch.name;
