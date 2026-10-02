-- Company-level shadow export of every person enriched in today's production runs.
-- NO email local part, NO person name, NO LinkedIn URL. One JSON object per line.
\pset tuples_only on
\pset format unaligned
SELECT json_build_object(
  'k', p.id,
  'employer_id', p.employer_id,
  'email_domain', lower(split_part(p.email, '@', 2)),
  'email_status', p.email_status,
  'employment_verified', p.facts_json->>'employment_verified',
  'a_alignment', coalesce(p.facts_json->>'email_alignment', ''),
  'a_approved', EXISTS (SELECT 1 FROM approvals a WHERE a.person_id = p.id),
  'employer_domain', lower(coalesce(e.domain, '')),
  'org_domain', lower(coalesce(p.facts_json->'enriched'->'organization'->>'primary_domain', p.organization_domain, '')),
  'org_website', lower(coalesce(p.facts_json->'enriched'->'organization'->>'website_url', '')),
  'org_suborgs', (SELECT coalesce(json_agg(json_build_object('website_url', so->>'website_url', 'primary_domain', so->>'primary_domain')), '[]'::json)
                  FROM jsonb_array_elements(CASE WHEN jsonb_typeof(p.facts_json->'enriched'->'organization'->'suborganizations') = 'array'
                                                 THEN p.facts_json->'enriched'->'organization'->'suborganizations' ELSE '[]'::jsonb END) so),
  'employer_name', coalesce(e.canonical_name, ''),
  'history', (SELECT coalesce(json_agg(json_build_object(
                 'organization_id', j->>'organization_id',
                 'organization_name', j->>'organization_name',
                 'current', (j->>'current')::boolean)), '[]'::json)
              FROM jsonb_array_elements(CASE WHEN jsonb_typeof(p.facts_json->'enriched'->'employment_history') = 'array'
                                             THEN p.facts_json->'enriched'->'employment_history' ELSE '[]'::jsonb END) j),
  'stored_country', coalesce(nullif(p.contact_country, ''), p.facts_json->'enriched'->>'country', ''),
  'stored_state', coalesce(p.facts_json->'enriched'->>'state', '')
)::text
FROM people p LEFT JOIN employers e ON e.id = p.employer_id
WHERE (p.facts_json->>'enriched_at')::timestamptz BETWEEN :'t0'::timestamptz AND :'t1'::timestamptz;
