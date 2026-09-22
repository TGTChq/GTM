-- Call-list sidecar input (read-only): every open/approved company x campaign unit with its
-- best still-active posting and whether the employer already has an Instantly contact.
COPY (
WITH exposed AS (
  SELECT DISTINCT a.employer_id FROM approvals a
  JOIN delivery_outbox o ON o.approval_id = a.id AND o.channel = 'instantly'
  JOIN delivery_receipts r ON r.outbox_id = o.id AND r.receipt_kind IN ('created','existing','reconciled')),
best_post AS (
  SELECT DISTINCT ON (op.opportunity_id) op.opportunity_id, p.title, p.url, p.date_posted, p.source
  FROM opportunity_postings op JOIN postings p ON p.id = op.posting_id
  WHERE p.state = 'classified' AND p.expired_at IS NULL
  ORDER BY op.opportunity_id, p.date_posted DESC NULLS LAST, p.id DESC),
nposts AS (
  SELECT op.opportunity_id, count(*) AS n FROM opportunity_postings op JOIN postings p ON p.id = op.posting_id
  WHERE p.state = 'classified' AND p.expired_at IS NULL GROUP BY 1)
SELECT o.id AS opportunity_id, o.function_key, o.campaign_key, o.state AS opp_state, o.employer_id,
       e.canonical_name AS employer_name, e.domain AS employer_domain, e.linkedin_slug, e.apollo_org_id,
       e.employee_count, e.company_country, bp.title AS job_title, bp.url AS job_url, bp.date_posted, bp.source,
       coalesce(np.n, 0) AS active_postings, (o.employer_id IN (SELECT employer_id FROM exposed)) AS email_exposed,
       (SELECT count(*) FROM approvals a WHERE a.opportunity_id = o.id) AS unit_approvals,
       (SELECT w.waiting_on FROM work_items w WHERE w.kind = 'qualify_opportunity' AND w.subject_id = o.id
         ORDER BY w.id DESC LIMIT 1) AS waiting_on
FROM opportunities o JOIN employers e ON e.id = o.employer_id
LEFT JOIN best_post bp ON bp.opportunity_id = o.id LEFT JOIN nposts np ON np.opportunity_id = o.id
WHERE o.state IN ('open', 'approved')
) TO STDOUT WITH CSV HEADER;
