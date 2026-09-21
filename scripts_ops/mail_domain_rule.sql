\pset footer off
-- Offline sizing of the CORROBORATED_MAIL_DOMAIN rule on the 222 rejected, already-paid people.
DROP TABLE IF EXISTS _mm;
CREATE TEMP TABLE _mm AS
SELECT p.id, p.employer_id,
       lower(split_part(p.email, '@', 2)) AS d,
       lower(coalesce(e.domain, '')) AS employer_domain,
       lower(coalesce(p.organization_domain, '')) AS person_org_domain,
       p.facts_json->'enriched'->'employment_history' AS h
FROM people p JOIN employers e ON e.id = p.employer_id
WHERE (p.facts_json->>'enriched_at')::timestamptz BETWEEN '2026-09-21 03:32+00' AND '2026-09-21 05:17+00'
  AND p.email_status = 'verified' AND p.facts_json->>'employment_verified' = 'true'
  AND coalesce(p.facts_json->>'email_alignment', '') = ''
  AND NOT EXISTS (SELECT 1 FROM approvals a WHERE a.person_id = p.id);

DROP TABLE IF EXISTS _r;
CREATE TEMP TABLE _r AS
SELECT m.*,
  (m.person_org_domain = m.employer_domain AND m.employer_domain <> '') AS c_apollo_org_is_employer,
  (m.d ~ '(zendesk|freshdesk|myshopify|hubspot|salesforce|force|atlassian|google|outlook|onmicrosoft|gmail|yahoo|hotmail|icloud|aol|proton)\.'
     OR m.d IN ('gmail.com','yahoo.com','hotmail.com','outlook.com','icloud.com','aol.com','protonmail.com','live.com','msn.com')) AS d_hosted_or_free,
  EXISTS (SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof(m.h)='array' THEN m.h ELSE '[]'::jsonb END) j
          WHERE NOT coalesce((j->>'current')::boolean, false)
            -- a past role at the SAME organisation (a promotion) is not a former employer
            AND coalesce(j->>'organization_id', '') NOT IN (
                SELECT coalesce(c->>'organization_id', '') FROM jsonb_array_elements(m.h) c
                WHERE coalesce((c->>'current')::boolean, false))
            AND length(split_part(m.d, '.', 1)) >= 3
            AND regexp_replace(lower(coalesce(j->>'organization_name','')), '[^a-z0-9]', '', 'g') LIKE '%' || split_part(m.d, '.', 1) || '%') AS e_past_employer_domain,
  ((SELECT count(DISTINCT m2.id) FROM _mm m2 WHERE m2.employer_id = m.employer_id AND m2.d = m.d) >= 2
    OR (SELECT count(*) FROM people q JOIN approvals a ON a.person_id = q.id
        WHERE q.employer_id = m.employer_id AND lower(split_part(q.email,'@',2)) = m.d) >= 1) AS f_cross_person,
  (split_part(m.d, '.', 1) = split_part(m.employer_domain, '.', 1) AND m.d <> m.employer_domain) AS f_same_label
FROM _mm m;

\echo '== rule gates on the 222 =='
SELECT count(*) AS n,
  count(*) FILTER (WHERE c_apollo_org_is_employer) AS c_ok,
  count(*) FILTER (WHERE d_hosted_or_free) AS d_reject_hosted_or_free,
  count(*) FILTER (WHERE e_past_employer_domain) AS e_reject_past_employer,
  count(*) FILTER (WHERE f_cross_person) AS f_cross_person,
  count(*) FILTER (WHERE f_same_label) AS f_same_label,
  count(*) FILTER (WHERE c_apollo_org_is_employer AND NOT d_hosted_or_free AND NOT e_past_employer_domain
                   AND (f_cross_person OR f_same_label)) AS accepted_by_rule,
  count(DISTINCT employer_id) FILTER (WHERE c_apollo_org_is_employer AND NOT d_hosted_or_free AND NOT e_past_employer_domain
                   AND (f_cross_person OR f_same_label)) AS employers_recovered
FROM _r;

\echo '== what the rule rejects, company-level =='
SELECT d, employer_domain, c_apollo_org_is_employer AS c, d_hosted_or_free AS d_bad, e_past_employer_domain AS e_past,
       f_cross_person AS f_x, f_same_label AS f_l
FROM _r WHERE NOT (c_apollo_org_is_employer AND NOT d_hosted_or_free AND NOT e_past_employer_domain AND (f_cross_person OR f_same_label))
ORDER BY random() LIMIT 25;
