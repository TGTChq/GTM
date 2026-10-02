\pset footer off
-- The 222 enriched, verified, currently-employed people rejected as email:domain_not_employer.
-- Company-level only: email DOMAINS, employer domains and organisation names. No local part, no person name.
DROP TABLE IF EXISTS _mm;
CREATE TEMP TABLE _mm AS
SELECT p.id,
       lower(split_part(p.email, '@', 2)) AS email_domain,
       lower(coalesce(e.domain, '')) AS employer_domain,
       e.canonical_name AS employer_name,
       lower(coalesce(p.organization_domain, '')) AS person_org_domain,
       coalesce(p.organization_name, '') AS person_org_name,
       e.linkedin_slug AS employer_slug
FROM people p JOIN employers e ON e.id = p.employer_id
WHERE (p.facts_json->>'enriched_at')::timestamptz BETWEEN '2026-09-21 03:32+00' AND '2026-09-21 05:17+00'
  AND p.email_status = 'verified' AND p.facts_json->>'employment_verified' = 'true'
  AND coalesce(p.facts_json->>'email_alignment', '') = ''
  AND NOT EXISTS (SELECT 1 FROM approvals a WHERE a.person_id = p.id);

\echo '== relation between the email domain and the employer =='
SELECT CASE
         WHEN email_domain = '' THEN 'no_email_domain'
         WHEN employer_domain = '' THEN 'employer_has_no_domain'
         WHEN email_domain = person_org_domain AND person_org_domain <> '' THEN 'email = person''s Apollo org domain'
         WHEN email_domain LIKE '%.' || employer_domain OR employer_domain LIKE '%.' || email_domain THEN 'subdomain relation'
         WHEN split_part(email_domain, '.', 1) = split_part(employer_domain, '.', 1) THEN 'same label, different TLD'
         ELSE 'different domain'
       END AS relation,
       count(*)
FROM _mm GROUP BY 1 ORDER BY 2 DESC;

\echo '== person org domain vs employer domain (does Apollo agree with our employer domain?) =='
SELECT (person_org_domain = employer_domain) AS apollo_org_domain_equals_ours,
       (email_domain = person_org_domain) AS email_on_apollo_org_domain, count(*)
FROM _mm GROUP BY 1,2 ORDER BY 3 DESC;

\echo '== 40 company-level examples (domains and org names only) =='
SELECT employer_name, employer_domain, email_domain, person_org_domain, person_org_name
FROM _mm ORDER BY random() LIMIT 40;
