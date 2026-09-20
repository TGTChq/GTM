# Redaction report: fantastic_support_bundle_20260918

Prepared 2026-09-18. No external API was called while building this bundle, and no
secret value was read for it.

## Files checked

`README.md`, `current_requests_redacted.json`, `pagination_behavior.md`, `run_evidence.csv`,
`rejection_breakdown.csv`, `sample_records_redacted.json`, `proposed_24h_request.json`,
`questions_for_remco.md`, `EMAIL_TO_REMCO.txt`.

## Automated scan

Every file was scanned for:

| check | result |
|---|---|
| credential assignments (`api_key`, `secret`, `password`, `token`, `cookie`, `session_id` = value) | 0 |
| `Authorization: Bearer` with a real value | 0 (only `Bearer <REDACTED>`) |
| provider key formats (`sk-…`, Airtable `pat…`, Slack `xox…`, AWS `AKIA…`, JWT, private-key blocks) | 0 |
| database connection strings, credentials inside URLs | 0 |
| Airtable object IDs (`app…`, `tbl…`, `rec…`) | 0 |
| UUIDs (Railway project/service/deployment IDs, Instantly campaign IDs) | 0 |
| 24-hex IDs (Apollo person/organization IDs) | 0 |
| email addresses | 0 |
| phone numbers | 0 |
| `.env` file references | 0 |
| URLs with query strings | 0 |
| internal budget/grant identifiers | 0 |
| contact-record field names (hiring manager, email, LinkedIn profile, names) | 0 |
| long opaque tokens (32+ mixed characters) | 14 hits, all reviewed as false positives: two JSON key names and twelve public LinkedIn job-URL slugs (job title + company + LinkedIn job number) |

A manual review also looked for personal names in quoted job-text excerpts. None were
found.

## Removed or withheld on purpose

- **Credentials:** API keys and tokens were never read. The authorization header is shown as `Bearer <REDACTED>`. The Railway base-URL override value is not included.
- **Job descriptions:** removed entirely. The source export contained 8 email addresses and 1 phone number, all inside description text (each also repeated in a stored copy of the description).
- **Recruiter / hiring-manager fields:** our client strips them from every provider row on receipt (`pii_fields_dropped` is shown as a count only).
- **Contact data:** no buyer names, emails, LinkedIn profiles, Apollo IDs or Airtable contact fields. Contact-stage outcomes appear as counts only.
- **Rule-trigger excerpts:** `rejection_breakdown.csv` excerpts were scrubbed of emails, phone numbers and URLs and cut to 120 characters. No scrub marker was needed in the final set.
- **URLs:** query strings and fragments were removed.
- **Internal identifiers:** Railway project, service and deployment IDs, local file paths, TGTC spend-budget IDs, Instantly campaign IDs, and the Airtable base and table IDs were all left out.
- **`exclude_organization_slug` values:** not exported (lists of company slugs).

## Kept on purpose

These are public job-posting data or non-sensitive internal references, as requested:
- provider job IDs;
- job URLs;
- job titles;
- company names and company LinkedIn URLs and slugs;
- posting dates and locations;
- provider fields such as `ai_employment_type`, `ai_taxonomies_a`, size and industry;
- provider quota header values;
- TGTC run IDs and git commit SHAs.
