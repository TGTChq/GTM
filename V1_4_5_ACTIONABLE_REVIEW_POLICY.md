# READY v1.4.5 — Actionable Review Policy

## Operational rule

A lead reaches Airtable when all of the following are true:

1. No gate has a confirmed hard rejection.
2. The vacancy survived the requested role, geography, employment, industry,
   staffing/outsourcing, freshness, CRM and duplication filters.
3. A relevant hiring manager was identified.
4. A professional company-domain email is available and is not explicitly
   invalid, disposable, generic, or mismatched.

Missing proof is not treated as proof of failure. Unknown firmographics,
unavailable source verification, incomplete territory evidence, and unavailable
email-deliverability verification remain visible as review flags.

## Hard blocks retained

- Confirmed role or employment mismatch.
- Confirmed non-US restriction.
- Confirmed staffing, recruiting, RPO, outsourcing, excluded industry, or
  excluded business model.
- Confirmed employee count outside the configured range.
- Confirmed inactive vacancy.
- Wrong-function, wrong-company, explicitly foreign-territory contact.
- Missing email, generic mailbox, company-domain mismatch, or explicitly
  invalid/disposable/webmail email.
- Duplicate, active CRM/Airtable company, or previously processed terminal row.

## Boundary behavior

- `FINAL_PASS`: fully verified and written to Airtable.
- `NEEDS_CHECK`: actionable but incomplete evidence; written to Airtable.
- `UNVERIFIED`: actionable uncertainty from an upstream evidence gate; written
  to Airtable only when the hiring manager and usable email exist.
- `REROUTE`: no usable aligned contact/email; not written.
- `REJECT`: confirmed hard-filter failure; not written.

Human approval is still mandatory before Instantly. Approved rows are
revalidated immediately before enrollment, and confirmed hard failures remain
blocked.
