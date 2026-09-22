-- Call-list sidecar input (read-only): every approved contact and its Instantly delivery.
COPY (
SELECT a.id AS approval_id, a.opportunity_id, a.state AS approval_state, a.campaign_key, a.campaign_id,
       a.outreach_eligible, a.contact_country, a.approved_at, a.employer_id,
       p.apollo_person_id, p.first_name, p.last_name, p.title, p.linkedin_url, lower(p.email) AS email,
       p.email_status, p.organization_domain, p.facts_json->'enriched'->>'seniority' AS seniority,
       ir.external_campaign AS instantly_campaign, ir.receipt_kind AS instantly_receipt, ir.received_at AS instantly_at
FROM approvals a JOIN people p ON p.id = a.person_id
LEFT JOIN LATERAL (
  SELECT r.external_campaign, r.receipt_kind, r.received_at FROM delivery_outbox o
  JOIN delivery_receipts r ON r.outbox_id = o.id
  WHERE o.approval_id = a.id AND o.channel = 'instantly' AND r.receipt_kind IN ('created','existing','reconciled')
  ORDER BY r.id LIMIT 1) ir ON true
) TO STDOUT WITH CSV HEADER;
