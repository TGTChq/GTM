\pset footer off
-- Reconciliation of ONE production run. Read-only.
-- Usage: psql -v t0="'<run start>'" -v t1="'<run end + 1 min>'" -v abase=<max approval id before> --             -v rbase=<max receipt id before> -v budget="'<budget id>'" -f run_reconcile.sql
-- fresh = approvals created during the run (id > abase); older queued = approvals created before it.
\set ch '(VALUES (''7b9aa5f3-fe46-49fa-b2ac-fee1da346ed0'',''PRODUCT''), (''69def27c-7799-41a2-9ba8-205e54ab071b'',''OPERATIONS''), (''7b319c7a-cc55-4e08-8a47-7058c345d8ae'',''FINANCE''), (''d2326028-e312-405b-9e16-526bd309d4dd'',''PEOPLE_HR''), (''c3e81c21-db44-40f5-addc-d9945a78394b'',''ECOMMERCE''), (''269cd138-00b1-48c3-9093-16c36120a20e'',''CUSTOMER_EXPERIENCE''), (''1feb6344-6065-49d7-9764-d125985fb9c9'',''MARKETING_CREATIVE''), (''8f25abd5-568a-4e88-b310-9acf85161c6c'',''GTM_SYSTEMS''), (''8bfa0769-4b9a-4346-8e93-17ac8b726dce'',''AI_TECHNICAL'')) AS ch(campaign_id, name)'

\echo == A. Fantastic: billed and unique jobs ==
SELECT status, count(*) requests, sum(estimated_credits) records FROM spend_reservations WHERE budget_id=:budget AND provider='fantastic' GROUP BY 1;
SELECT count(*) AS postings_first_seen, count(*) FILTER (WHERE duplicate_of_posting_id IS NULL) AS unique_jobs,
       count(DISTINCT canonical_key) AS distinct_canonical, count(DISTINCT employer_id) AS employers_seen
FROM postings WHERE first_seen_at >= :t0 AND first_seen_at < :t1;
SELECT source, count(*) FROM postings WHERE first_seen_at >= :t0 AND first_seen_at < :t1 GROUP BY 1 ORDER BY 2 DESC;

\echo == B. classification during the run: hard exclusions by reason ==
SELECT count(*) AS classified, count(*) FILTER (WHERE excluded) AS excluded, count(*) FILTER (WHERE NOT excluded) AS kept
FROM classifications WHERE created_at >= :t0 AND created_at < :t1;
SELECT coalesce(exclusion_reason,'(none)') reason, count(*) FROM classifications
WHERE created_at >= :t0 AND created_at < :t1 AND excluded GROUP BY 1 ORDER BY 2 DESC LIMIT 25;

\echo == C. jobs assigned across the nine campaigns (postings linked to opportunities during the run) ==
SELECT o.campaign_key, count(DISTINCT op.posting_id) AS jobs, count(DISTINCT o.employer_id) AS employers, count(DISTINCT o.id) AS units
FROM opportunity_postings op JOIN opportunities o ON o.id = op.opportunity_id JOIN postings p ON p.id = op.posting_id
WHERE p.first_seen_at >= :t0 AND p.first_seen_at < :t1 GROUP BY 1 ORDER BY 2 DESC;
SELECT count(DISTINCT op.posting_id) jobs_assigned, count(DISTINCT o.employer_id) unique_employers, count(DISTINCT (o.employer_id, o.campaign_key)) company_x_campaign_units
FROM opportunity_postings op JOIN opportunities o ON o.id = op.opportunity_id JOIN postings p ON p.id = op.posting_id
WHERE p.first_seen_at >= :t0 AND p.first_seen_at < :t1;

\echo == D. Apollo ==
SELECT status, count(*) requests, sum(estimated_credits) credits FROM spend_reservations WHERE budget_id=:budget AND provider='apollo' GROUP BY 1;
SELECT operation, status, count(*), sum(estimated_credits) FROM spend_reservations WHERE budget_id=:budget AND provider='apollo' GROUP BY 1,2 ORDER BY 1,2;
SELECT ra.status, ra.error_class, left(coalesce(ra.error_code,''),60) code, ra.http_status, count(*) FROM request_attempts ra
WHERE ra.provider='apollo' AND ra.started_at >= :t0 AND ra.started_at < :t1 AND ra.status <> 'served' GROUP BY 1,2,3,4 ORDER BY 5 DESC;
SELECT provider, state, last_error_code FROM provider_state;

\echo == E. approvals produced by this run (verified contacts) ==
SELECT count(*) approvals, count(DISTINCT a.person_id) people, count(DISTINCT lower(p.email)) emails,
       count(*) FILTER (WHERE p.email_status='verified') verified_email,
       count(*) FILTER (WHERE a.outreach_eligible) outreach_eligible,
       count(DISTINCT a.employer_id) employers, count(DISTINCT (a.employer_id, a.campaign_key)) company_x_campaign
FROM approvals a JOIN people p ON p.id=a.person_id WHERE a.id > :abase AND a.approved_at < :t1;
SELECT coalesce(a.outreach_block_reason,'eligible') reason, count(*) FROM approvals a WHERE a.id > :abase AND a.approved_at < :t1 GROUP BY 1 ORDER BY 2 DESC;
SELECT coalesce(p.facts_json->>'email_alignment','') alignment, count(*) FROM approvals a JOIN people p ON p.id=a.person_id WHERE a.id > :abase AND a.approved_at < :t1 GROUP BY 1 ORDER BY 2 DESC;

\echo == E2. fresh approvals by unit origin (unit created during the run = new acquisition; before = 2nd/3rd-contact retry) ==
SELECT CASE WHEN o.created_at >= :t0 THEN 'new_acquisition_unit' ELSE 'retry_of_earlier_unit' END AS origin, count(*) approvals,
       count(*) FILTER (WHERE a.outreach_eligible) eligible
FROM approvals a JOIN opportunities o ON o.id = a.opportunity_id WHERE a.id > :abase AND a.approved_at < :t1 GROUP BY 1;

\echo == F. Airtable writes during the run ==
SELECT r.receipt_kind, count(*) FROM delivery_receipts r WHERE r.id > :rbase AND r.channel='airtable' AND r.received_at < :t1 GROUP BY 1 ORDER BY 1;

\echo == G. Instantly receipts during the run, new approvals vs old backlog ==
SELECT CASE WHEN o.approval_id > :abase THEN 'new_this_run' ELSE 'old_backlog' END origin, r.receipt_kind, count(*), count(DISTINCT lower(o.payload_json->>'email')) distinct_emails
FROM delivery_receipts r JOIN delivery_outbox o ON o.id=r.outbox_id WHERE r.id > :rbase AND r.channel='instantly' AND r.received_at < :t1 GROUP BY 1,2 ORDER BY 1,2;

\echo == H. Instantly outbox state now for fresh approvals and for the older queue ==
SELECT CASE WHEN o.approval_id > :abase THEN 'new_this_run' ELSE 'old_backlog' END origin, o.state, left(coalesce(o.blocked_reason, o.last_error,''),50) reason, count(*)
FROM delivery_outbox o WHERE o.channel='instantly' AND (o.approval_id > :abase OR o.state IN ('pending','failed') OR o.updated_at >= :t0)
GROUP BY 1,2,3 ORDER BY 1,4 DESC;

\echo == I. SAFETY (all must be 0) ==
SELECT
 (SELECT count(*) FROM delivery_receipts r WHERE r.id > :rbase AND r.channel='instantly' AND r.receipt_kind='created' AND r.external_campaign NOT IN (SELECT campaign_id FROM :ch)) created_outside_challenger,
 (SELECT count(*) - count(DISTINCT lower(o.payload_json->>'email')) FROM delivery_receipts r JOIN delivery_outbox o ON o.id=r.outbox_id WHERE r.id > :rbase AND r.channel='instantly' AND r.receipt_kind='created') duplicate_emails_in_run,
 (SELECT count(*) FROM delivery_receipts r JOIN delivery_outbox o ON o.id=r.outbox_id WHERE r.id > :rbase AND r.channel='instantly' AND r.receipt_kind='created'
    AND EXISTS (SELECT 1 FROM delivery_receipts r2 JOIN delivery_outbox o2 ON o2.id=r2.outbox_id WHERE r2.id <= :rbase AND r2.channel='instantly' AND r2.receipt_kind='created' AND lower(o2.payload_json->>'email')=lower(o.payload_json->>'email'))) created_before_this_run,
 (SELECT coalesce(max(n),0) FROM (SELECT o.id, count(*) FILTER (WHERE r.receipt_kind IN ('created','existing','reconciled')) n FROM delivery_outbox o JOIN delivery_receipts r ON r.outbox_id=o.id WHERE o.channel='instantly' GROUP BY o.id) x) - 1 extra_terminal_receipts,
 (SELECT count(*) FROM delivery_outbox WHERE channel='instantly' AND state IN ('claimed','in_flight')) mid_flight,
 (SELECT count(*) FROM approvals a JOIN people p ON p.id=a.person_id WHERE a.id > :abase AND a.outreach_eligible AND coalesce(p.email_status,'') <> 'verified') eligible_not_verified,
 (SELECT count(*) FROM approvals a WHERE a.id > :abase AND a.outreach_eligible AND coalesce(a.contact_country,'') <> 'US') eligible_non_us;

\echo == G2. fresh Instantly creations by unit origin ==
SELECT CASE WHEN op.created_at >= :t0 THEN 'new_acquisition_unit' ELSE 'retry_of_earlier_unit' END AS origin, count(*) created, count(DISTINCT lower(o.payload_json->>'email')) distinct_emails
FROM delivery_receipts r JOIN delivery_outbox o ON o.id=r.outbox_id JOIN approvals a ON a.id=o.approval_id JOIN opportunities op ON op.id=a.opportunity_id
WHERE r.id > :rbase AND r.channel='instantly' AND r.receipt_kind='created' AND a.id > :abase GROUP BY 1;

\echo == J. per campaign: new approvals, eligible, Instantly created (new / backlog), blocked, pending ==
SELECT ch.name,
 (SELECT count(*) FROM approvals a WHERE a.id > :abase AND a.campaign_id=ch.campaign_id) approvals,
 (SELECT count(*) FROM approvals a WHERE a.id > :abase AND a.campaign_id=ch.campaign_id AND a.outreach_eligible) eligible,
 (SELECT count(*) FROM delivery_receipts r JOIN delivery_outbox o ON o.id=r.outbox_id WHERE r.id > :rbase AND r.channel='instantly' AND r.receipt_kind='created' AND r.external_campaign=ch.campaign_id AND o.approval_id > :abase) created_new,
 (SELECT count(*) FROM delivery_receipts r JOIN delivery_outbox o ON o.id=r.outbox_id WHERE r.id > :rbase AND r.channel='instantly' AND r.receipt_kind='created' AND r.external_campaign=ch.campaign_id AND o.approval_id <= :abase) created_backlog,
 (SELECT count(*) FROM delivery_receipts r JOIN delivery_outbox o ON o.id=r.outbox_id WHERE r.id > :rbase AND r.channel='instantly' AND r.receipt_kind IN ('existing','reconciled') AND o.payload_json->>'campaign'=ch.campaign_id) existing,
 (SELECT count(*) FROM delivery_outbox o JOIN approvals a ON a.id=o.approval_id WHERE o.channel='instantly' AND a.id > :abase AND a.campaign_id=ch.campaign_id AND o.state='blocked') blocked,
 (SELECT count(*) FROM delivery_outbox o JOIN approvals a ON a.id=o.approval_id WHERE o.channel='instantly' AND a.campaign_id=ch.campaign_id AND o.state IN ('pending','failed')) pending_now
FROM :ch ORDER BY 3 DESC;

\echo == K. qualify technical failures during the run ==
SELECT left(coalesce(last_error,''),80) err, state, count(*) FROM work_items WHERE kind='qualify_opportunity' AND updated_at >= :t0 AND updated_at < :t1 AND (last_error ILIKE '%technical%' OR last_error ILIKE '%error%' OR last_error ILIKE '%timeout%') GROUP BY 1,2 ORDER BY 3 DESC LIMIT 10;
SELECT left(waiting_on,50) waiting_on, count(*) FROM work_items WHERE state='waiting' GROUP BY 1 ORDER BY 2 DESC LIMIT 8;
