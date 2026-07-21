# Airtable review queue setup

Create a table named `Leads` (or set `AIRTABLE_TABLE_NAME` to your table name) with these exact fields.

| Field | Type | Notes |
|---|---|---|
| Lead Key | Single line text | Required; used for idempotency |
| Company | Single line text | |
| Website | URL | |
| Open Role | Single line text | Primary opening used in copy |
| Open Roles | Long text | All related openings in the same functional bucket |
| Role Focus | Single line text | Editable fragment inserted after “focused on”; required before approval |
| Focus Quality | Single select | `specific`, `manual_required` |
| Focus Evidence | Long text | Exact JD signals that produced the suggested focus |
| Matched Role | Single select or text | One of the configured target roles |
| Role Bucket | Single select or text | `gtm_revenue`, `engineering`, `marketing`, `customer_success`, `customer_support`, `finance`, `operations`, `people_hr`, `product`, `ecommerce` |
| Job URL | URL | |
| Job Source | Single line text | |
| Posted At | Single line text or date | Single line text is safest across source formats |
| Location | Single line text | |
| Employment Type | Single line text | |
| Relevance | Single select | `accept`, `review` |
| Relevance Score | Number | Integer |
| Relevance Reason | Long text | |
| Hiring Manager | Single line text | |
| HM Title | Single line text | |
| LinkedIn | URL | |
| Email | Email | |
| Email Source | Single select or text | `apollo`, `hunter` |
| Apollo Email Status | Single line text | |
| Hunter Email Status | Single line text | |
| Confidence | Single select | `verified` for strict `FINAL_PASS`/`NEEDS_CHECK`; legacy values may remain on old rows |
| Employees | Number | Integer |
| Size Band | Single select | `small`, `mid`, `large`, `unknown` |
| Founded | Number | Integer |
| Industry | Single line text | |
| Campaign ID | Single line text | Instantly campaign UUID selected by routing |
| Job ID | Single line text | |
| Final Decision | Single select | `FINAL_PASS`, `NEEDS_CHECK` (only these are written) |
| Decision Reason | Single line text | Stable primary reason code |
| Secondary Reasons | Long text | Additional reason codes |
| Official Source | URL | ATS/careers source used by Job Gate |
| Evidence Status | Single select or text | Job Gate state |
| Firmographics Status | Single select or text | Account Gate state |
| Contact Alignment | Single select or text | Contact Gate state |
| Email Validation | Single select or text | Email Gate state |
| Validation Version | Single line text | Example: `tgtc-final-pass-v0.2` |
| Evidence Bundle | Long text | Machine-readable JSON for audit |
| Status | Single select | `Pending`, `Approved`, `Rejected`, `Enrolled`, `Error` |
| Error | Long text | Enrollment errors are written here |

Strict output boundary:

- `FINAL_PASS` is written with `Relevance = accept`.
- `NEEDS_CHECK` is written with `Relevance = review` and does not count toward the daily target.
- `REJECT`, `UNVERIFIED`, and unresolved `REROUTE` never create Airtable rows.
- `run_approved.py` enrolls only Approved rows whose `Final Decision = FINAL_PASS` and `Validation Version` is present.

Recommended views:

1. **Needs Focus** — filter `Status = Pending` and either `Role Focus` is empty or `Focus Quality = manual_required`.
2. **Pending Review** — filter `Status = Pending`.
3. **Approved / Waiting** — filter `Status = Approved`.
4. **Enrolled** — filter `Status = Enrolled`.
5. **Errors** — filter `Status = Error`.

The code does not auto-create schema fields. Airtable's normal records API will reject unknown field names, so create the fields before the first production run.

## Role Focus review rule

`Role Focus` is not a copied JD sentence. The pipeline converts explicit JD signals into a controlled phrase such as:

- `CRM automation, lead enrichment, and outbound infrastructure`
- `customer onboarding, retention, and expansion`
- `short-form video, post-production, and motion graphics`

The phrase is designed to follow the words **“focused on”**, so do not add a period, a leading “focused on,” or a full sentence. Preserve acronyms and product names (`AI`, `CRM`, `LLM`, `APIs`, `HubSpot`, etc.).

If the extractor cannot find enough explicit evidence, it inserts a controlled role-level fallback and sets `Focus Quality = manual_required`. Review or edit that fallback before changing `Status` to `Approved`. The Instantly worker refuses to enroll an approved record that is missing `Role Focus`.
