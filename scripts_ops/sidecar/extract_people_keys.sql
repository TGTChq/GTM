-- Call-list sidecar input (read-only): identity keys of every person the core has ever enriched,
-- plus the production suppression registry.
COPY (
SELECT 'person' AS kind, apollo_person_id, linkedin_url, lower(email) AS email, employer_id FROM people
UNION ALL
SELECT 'suppression', NULL, NULL, lower(key), NULL FROM suppressions
) TO STDOUT WITH CSV HEADER;
