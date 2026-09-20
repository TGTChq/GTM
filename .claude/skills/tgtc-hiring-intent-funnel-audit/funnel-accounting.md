# Funnel accounting contract

## Stages, in order

Each stage reconciles to the previous one.

1. provider inventory count (from `-count`)
2. records returned
3. records billed
4. intra-query unique
5. cross-query unique
6. cross-source unique
7. historically net-new jobs
8. employer identity resolved
9. jobs evaluated
10. role eligible
11. company/ICP eligible
12. qualified opportunities (jobs)
13. company × campaign units
14. Apollo candidates found
15. candidates selected
16. people enriched
17. current employer confirmed
18. functional contact confirmed
19. verified email
20. CRM/Instantly/suppression net-new
21. final approved leads

**Invariant at every boundary:** `input = pass + reject + unknown + duplicate + error`.
- Anything left over is written as `unexplained` with its count. It is never folded into another bucket.
- `n/a`, a missing counter or a null is **not** zero. Report it as missing.

## Decision record

There is one record per job per stage. Contacts get their own records keyed by `person_key`, linked to the `job_id`.

```json
{"job_id": "...", "stage": "...", "decision": "pass|reject|unknown|duplicate|error",
 "primary_campaign": "product|operations|finance|people_hr|ecommerce|customer_experience|marketing_creative|gtm_systems|ai_technical|null",
 "secondary_campaign": "<another campaign key, != primary>|null",
 "qualification_status": "qualified|rejected|unknown|duplicate|error",
 "primary_reason_code": "...", "all_reason_codes": [], "rule_version": "...",
 "query_arm": "...", "source": "...", "geography": "US|UK|DE|AE|SA|other|unknown", "evidence": {}}
```

**Campaign keys** are the `campaigns.py` keys. Customer Experience covers both `customer_success` and `customer_support`.

**`evidence`** holds the matched text span and field names, never PII. People appear only as hashed `person_key` / `email_hash`.

## Reason codes

- **Format:** `<stage>:<family>:<detail>`, for example `role:seniority:vp_title` or `company:size:above_max`.
- Every `reject`, `unknown`, `duplicate` and `error` has a primary code. `other`, `misc` and empty codes are forbidden.
- `all_reason_codes` lists every rule that fired, not only the first. This is what makes single-rule counterfactuals possible.
- `unknown` codes name the missing input, for example `unknown:company:headcount_missing` or `unknown:legal_basis_undocumented`.

## Reporting

- Report the full funnel for each of the **9 campaigns** and each of the **45 campaign × geography cells**, split by provider source and query arm.
- Secondary-campaign fits go in an "also fits" column that is never summed.
- Keep three counts separate: companies (distinct employer), company × campaign units, and contacts (distinct person, counted once globally).

## Artifact columns

These are the minimum columns; more are allowed.

**`FUNNEL_BASELINE.csv`**
- `campaign`, `geography`, `source`, `query_arm`, `stage`, `stage_order`
- `input`, `pass`, `reject`, `unknown`, `duplicate`, `error`, `unexplained`
- `unit`, `rule_version`, `evidence_path`

**`LOSS_MATRIX.csv`**
- `campaign`, `geography`, `stage`, `reason_code`, `rejected_count`
- `labelled_n`, `false_negative_n`, `fn_rate`, `fn_rate_ci_low`, `fn_rate_ci_high`
- `good_jobs_lost_est`, `counterfactual_only_this_rule_qualified_delta`

**`QUERY_COUNT_MATRIX.csv`**
- `campaign`, `geography`, `provider_endpoint`, `source`, `arm`, `params_hash`, `params_redacted`
- `count`, `window_start`, `window_end`, `probed_at`, `requests_used`, `job_credits_used`

**`COST_BY_VERTICAL_GEOGRAPHY.csv`**
- `campaign`, `geography`, `arm`
- `fantastic_records_billed`, `fantastic_requests`
- `apollo_search_calls`, `apollo_match_credits`, `apollo_org_credits`
- `llm_input_tokens`, `llm_output_tokens`
- `usd_per_unit_source`
- `approved_jobs`, `approved_contacts`, `cost_per_approved_job`, `cost_per_approved_contact`
