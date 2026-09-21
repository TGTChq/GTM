\pset footer off
-- Acceptance measurement for the B live canary. Usage: psql -v run_id="'...'" -f canary_b_measure.sql
DROP TABLE IF EXISTS _ca;
CREATE TEMP TABLE _ca AS
SELECT a.*, p.email AS p_email, lower(split_part(p.email, '@', 2)) AS email_domain,
       coalesce(p.facts_json->>'email_alignment', '') AS alignment,
       coalesce(p.facts_json->>'mail_domain_basis', '') AS basis,
       lower(coalesce(p.organization_domain, '')) AS person_org_domain,
       lower(coalesce(e.domain, '')) AS employer_domain, e.canonical_name AS employer_name
FROM approvals a JOIN people p ON p.id = a.person_id JOIN employers e ON e.id = a.employer_id
WHERE a.run_id = :run_id;

\echo '== 1. canary approvals by email alignment and evidence basis =='
SELECT alignment, basis, count(*) AS approvals, count(*) FILTER (WHERE outreach_eligible) AS outreach_eligible
FROM _ca GROUP BY 1,2 ORDER BY 3 DESC;

\echo '== 2. safety: must all be ZERO =='
SELECT
  count(*) FILTER (WHERE alignment = 'CORROBORATED_MAIL_DOMAIN' AND basis NOT IN ('provider_confirmed','strict_seed','independent_employees')) AS bad_basis,
  count(*) FILTER (WHERE alignment = 'CORROBORATED_MAIL_DOMAIN' AND person_org_domain <> employer_domain) AS employer_mismatch,
  count(*) FILTER (WHERE email_domain ~ '(gmail|yahoo|hotmail|outlook|icloud|aol|proton|zendesk|freshdesk|greenhouse|lever|workday|icims|indeed|linkedin)') AS free_saas_ats,
  count(*) FILTER (WHERE outreach_eligible AND coalesce(contact_country,'') <> 'US') AS non_us_eligible,
  count(*) - count(DISTINCT person_id) AS duplicate_people,
  count(*) - count(DISTINCT lower(p_email)) AS duplicate_emails
FROM _ca;

\echo '== 3. any email of a canary approval already present on ANOTHER approval (must be 0) =='
SELECT count(*) AS emails_already_approved_elsewhere FROM _ca c
WHERE EXISTS (SELECT 1 FROM approvals x JOIN people q ON q.id = x.person_id
              WHERE lower(q.email) = lower(c.p_email) AND x.id <> c.id);

\echo '== 4. Instantly outcome for canary approvals =='
SELECT o.state, coalesce(o.blocked_reason, o.last_error, '') AS reason, count(*)
FROM delivery_outbox o JOIN _ca c ON c.id = o.approval_id WHERE o.channel = 'instantly' GROUP BY 1,2 ORDER BY 3 DESC;

\echo '== 5. Instantly receipts for canary approvals (created / existing / attempted) =='
SELECT r.receipt_kind, count(*) FROM delivery_receipts r JOIN delivery_outbox o ON o.id = r.outbox_id
JOIN _ca c ON c.id = o.approval_id WHERE o.channel = 'instantly' GROUP BY 1 ORDER BY 1;

\echo '== 6. created in Instantly, by campaign id (Challenger only; exactly once) =='
SELECT c.campaign_key, c.campaign_id, count(*) AS created, count(DISTINCT lower(c.p_email)) AS distinct_emails
FROM _ca c JOIN delivery_outbox o ON o.approval_id = c.id AND o.channel = 'instantly'
JOIN delivery_receipts r ON r.outbox_id = o.id AND r.receipt_kind = 'created' GROUP BY 1,2 ORDER BY 3 DESC;

\echo '== 7. terminal receipts per outbox row (must be <= 1) =='
SELECT max(n) AS max_terminal_receipts_per_row FROM (
  SELECT o.id, count(*) FILTER (WHERE r.receipt_kind IN ('created','existing','reconciled')) AS n
  FROM delivery_outbox o JOIN _ca c ON c.id = o.approval_id LEFT JOIN delivery_receipts r ON r.outbox_id = o.id
  WHERE o.channel = 'instantly' GROUP BY o.id) x;

\echo '== 8. zero-cost confirmation: provider reservations created during the canary (must be 0) =='
SELECT provider, count(*) AS new_reservations, coalesce(sum(estimated_credits), 0) AS credits
FROM spend_reservations WHERE created_at >= (SELECT min(created_at) FROM run_log WHERE run_id = :run_id)
GROUP BY 1;

\echo '== 9. company-level sample of mail-domain acceptances (for employer verification) =='
SELECT employer_name, employer_domain, email_domain, basis, campaign_key, contact_country
FROM _ca WHERE alignment = 'CORROBORATED_MAIL_DOMAIN' ORDER BY employer_name LIMIT 40;
