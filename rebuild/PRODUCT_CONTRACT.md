# TGTC core rebuild — product contract

Status: **implementation contract, not a deployment.** Written 2026-09-08 from
`TGTC_REBUILD_BLUEPRINT.md` (Codex, 2026-09-09 draft) reconciled against
`TGTChq/GTM` at `main = 5d87851`. Where the blueprint and the repository disagree
the resolution is stated here explicitly; nothing below is a legacy default
silently promoted to business truth.

The new package is `tgtc_core/`. It has its own entry point (`python -m tgtc_core`),
its own PostgreSQL schema (`tgtc_core/db/schema.sql`) and its own tests
(`tests_core/`). It never imports the old orchestrator, `run_orchestrator.py`,
`run_approved.py`, `hiring_manager.py`, `config.py` or any gate module. The few
utilities it reuses are listed in `INTEGRATION_MAP.md §7`.

---

## 1. Purpose

Continuously detect hiring opportunities compatible with the TGTC offer and the
nine live Instantly campaigns; identify the real employer and the buyer who owns
that team; obtain a verifiable professional email; and deliver **new, distinct,
complete** leads to Airtable and to the matching Instantly campaign with no manual
review step. Keep traceability from posting to reply/meeting/contract when those
outcomes are available.

The design objective of 1,000 new, distinct Approved leads per day is retained as a
**design target**. It is not a demonstrated rate and nothing in this branch claims
one. Throughput and buyer availability are measured separately (see
`ACCEPTANCE.md §4`).

## 2. Commercial unit

The commercial unit is the **opportunity = employer × function**, carrying one or
more postings. Rules:

* Several postings of the same function at the same employer are one opportunity.
* Two functions at one employer are two opportunities and may reach two different
  buyers. They never reach the **same person twice**: a person–employer pair is
  approved at most once (`approvals` has a unique index on the person while the
  approval is not revoked), so the second function's discovery must find a
  different buyer or close.
* Account-level "one contact per company" suppression is **off** in production
  today (`AIRTABLE_SUPPRESS_ACCOUNT_LEVEL=false`, container-verified 2026-09-07).
  It is carried as an explicit, default-off policy switch, not silently dropped.

## 3. Changes the user explicitly requested (and how each is honoured)

| Request | Contract |
|---|---|
| Admission depends on function, responsibilities and campaign compatibility; an unknown, generic or missing title is not grounds for exclusion. | `tgtc_core/domain/classification.py` decides from description + structured fields. The title is stored as data and displayed in copy; it is never a required input and never a gate. Test `test_title_independence.py` asserts identical decisions when the title is replaced by a generic one or removed. |
| Routine queries do not depend on title lists. | Acquisition (`tgtc_core/providers/fantastic.py`) sends **no** `title_advanced` and no description keyword expression. Coverage/cost of the title-free feed is reported, not hidden. |
| Every lead meeting the contract is delivered as **Approved**. No `Needs Check`, `Pending Review` or confidence tiers. | Airtable rows are written with `Status = Approved` only. Internal states describe *work* (`ready`, `running`, `waiting`, `retry`, `closed`, `done`), never lead confidence. A lead that cannot prove a fact is **closed with a reason** and stays reopenable; it is never written as a review row. |
| Work incomplete for technical reasons is retried automatically, and is neither rejected nor approved by default. | Provider timeouts, 429/5xx and lease expiry put the `work_items` row into `retry` with backoff; the item keeps its identity and attempt count. Only business facts close it. |
| Fresh inventory has protected capacity against backlog. | Two lanes (`fresh`, `backfill`) with an 80/20 claim share that borrows in both directions when one lane is empty (`tgtc_core/services/scheduler.py`). |
| Reuse accounts, credentials, campaign IDs, schemas and assets; do not buy tools or change copy. | Same Airtable base/table and field names, same nine Instantly campaign IDs (resolved from the same env names), same Control-A payload shape, same custom-variable names. No copy is rendered or altered by this package. |

## 4. Campaign registry (the nine routes)

Source of truth for the mapping: `outbound_wave1/campaigns.py` at `5d87851`
(verified) and `config.CAMPAIGN_ENV_BY_BUCKET`. The new registry
`tgtc_core/policy/campaigns.py` reproduces it and a compatibility test
(`tests_core/test_campaign_registry.py`) asserts the two agree on names, function
keys and env names so they cannot drift apart.

| Campaign (live name) | Function keys it serves | Env name(s) for the campaign id | Initial buyer hierarchy (from `role_mapping.BUCKET_DIRECT_TITLES` + `BUCKET_TITLES`) |
|---|---|---|---|
| PRODUCT | `product` | `INSTANTLY_CAMPAIGN_PRODUCT` | product/design directors → CPO/VP Product/Head of Product → CTO → founders* |
| OPERATIONS | `operations` | `INSTANTLY_CAMPAIGN_OPERATIONS` | ops directors/managers → COO/VP Ops/Head of Ops/Chief of Staff → founders* |
| FINANCE | `finance` | `INSTANTLY_CAMPAIGN_FINANCE` | finance/accounting directors → CFO/VP Finance/Controller → COO → founders* |
| PEOPLE & HR | `people_hr` | `INSTANTLY_CAMPAIGN_PEOPLE_HR` | HR/People Ops/TA directors → CHRO/CPO/VP People → founders* |
| ECOMMERCE | `ecommerce` | `INSTANTLY_CAMPAIGN_ECOMMERCE` | ecommerce directors → VP/Head of Ecommerce → CMO → COO → founders* |
| CUSTOMER EXPERIENCE | `customer_success`, `customer_support` | `INSTANTLY_CAMPAIGN_CUSTOMER_SUCCESS`, `INSTANTLY_CAMPAIGN_CUSTOMER_SUPPORT` | CS/Support directors → VP/Head of CS or Support → CCO → COO → founders* |
| MARKETING & CREATIVE | `marketing` | `INSTANTLY_CAMPAIGN_MARKETING` | marketing/growth directors → CMO/VP Marketing → founders* |
| GTM SYSTEMS & REVENUE AUTOMATION | `gtm_revenue` | `INSTANTLY_CAMPAIGN_GTM` | RevOps/Sales Ops directors → CRO/VP RevOps/Head of GTM → founders* |
| AI & TECHNICAL AUTOMATION | `engineering` | `INSTANTLY_CAMPAIGN_ENGINEERING` | engineering managers/directors → CTO/VP Eng/Head of AI → founders* |

\* Founders/CEO are searched **only** when the employer has ≤ 99 employees
(`FOUNDER_FALLBACK_MAX_EMPLOYEES=99`, legacy value carried as explicit policy).
They are never a universal fallback.

The legacy catalogue also routes `data` and `it` roles to the `engineering`
campaign and `partnerships` to `gtm_revenue`. The new classifier emits the ten
function keys above only; data/IT/partnerships work is classified into
`engineering` / `gtm_revenue` directly by its responsibilities.

## 5. Eligibility policy (explicit, versioned)

`tgtc_core/policy/requirements.py` carries every rule with its **provenance**. Two
provenance classes exist:

* `blueprint` — stated in the rebuild blueprint.
* `legacy_default_pending_confirmation` — the value production runs today, imported
  as a starting policy **pending Luis's confirmation**. These are decisions, not
  facts, and the manifest says so.

| Rule | Value | Provenance |
|---|---|---|
| Market | US market: explicit US scope, or provider `countries_derived` includes US and no foreign-only clause | legacy default pending confirmation |
| Employment | full-time, open-ended; part-time / contract / fixed-term / fractional / temporary / freelance / seasonal / internship / unpaid excluded | legacy default pending confirmation |
| Deliverability | work TGTC can staff remotely: field work, mandatory physical facility, ≥20% travel, security clearance, mandatory professional license excluded. Remote/hybrid/onsite **labels** are preserved as data, not used as a gate | blueprint + legacy |
| Job seniority | intern, director and above, VP, C-level, principal/staff/lead IC excluded; senior IC allowed; roles with people-management authority excluded | legacy default pending confirmation (`ROLE_ALLOW_SENIOR_IC=1`, `job_quality.has_people_authority`) |
| Company size | 25 ≤ employees ≤ 1000 when Apollo returns a count; unknown count does **not** reject (it closes as `insufficient_evidence:employee_count` only if the policy flag `REQUIRE_EMPLOYEE_COUNT` is on; default off, matching `REJECT_UNKNOWN_FIRMOGRAPHICS` being a legacy env value not a code truth) | legacy default pending confirmation |
| Excluded industries | Apollo taxonomy list from `config.APOLLO_EXCLUDED_INDUSTRY_KEYWORDS` (staffing, recruiting, government administration, healthcare family, outsourcing/offshoring, media family, nonprofit management, …) | legacy default pending confirmation |
| Agencies | staffing/RPO/outsourcing employers excluded by provider flag (`org_linkedin_recruitment_agency_derived`), industry and description evidence | blueprint + legacy |
| Employer identity | domain from the organization's own URL/derived domain; ATS hosts, aggregators, shorteners and free-mail hosts are never an identity; LinkedIn slug is a second anchor; aliases require corroboration and live in `employer_aliases` | blueprint |
| Contact | current employee of the resolved employer (Apollo current organization or employment history), title matches the function's buyer hierarchy, LinkedIn profile required, no foreign-only territory in the role title | legacy default pending confirmation (`REQUIRE_CURRENT_EMPLOYMENT_EVIDENCE=1`, `REQUIRE_CONTACT_LINKEDIN=1`) |
| Email | professional (not a generic mailbox, not free-mail), on the employer domain **or** on a corroborated alternate employer domain, and **Apollo `email_status == "verified"`**. Hunter never promotes. `unknown`, `accept_all`, `extrapolated`, `likely_to_engage` never pass | blueprint §8 keeps the existing Apollo-only authority until a second authority is accepted and tested |
| Suppressions | person email, person–employer pair, company × function with an active row, unsubscribed/replied/bounced, existing customer — re-checked at approval **and** immediately before each delivery | blueprint |

### What the contract removes

* `NEEDS_CHECK`, `UNVERIFIED`, `Pending`, `REROUTE` as *commercial* outputs.
* The role catalogue (`role_catalog.py`, 118 roles) as an eligibility gate.
* The `title_advanced` acquisition filter and the functional-discovery description
  expression.
* The 2,000-call recovery ceiling as a proxy for spend. Spend is `credit_events`:
  requests, estimated credits and provider-confirmed usage are three columns.

## 6. Classification contract

Input: description text, employment/location/structured provider fields, employer
fields. Title optional.

Output (`ClassificationResult`, validated schema): `compatible_functions`,
`campaign_keys`, `excluded` + `exclusion_reason`, `required_facts` (each with
value/status/excerpt), `responsibilities` (1–3 short phrases, each grounded in a
literal description excerpt verified by code), `contradictions`, `method`
(`deterministic` | `semantic`), `policy_version`, `model_version`.

Order of decision:

1. Deterministic facts (employment, market, deliverability, seniority, agency).
   A hard incompatibility closes the posting with a reason. No model call.
2. Deterministic function evidence: a responsibility lexicon per function scores
   evidence *fragments* from the description. A dominant function with enough
   independent evidence and no strong competitor decides deterministically.
3. Otherwise the semantic port is consulted (`tgtc_core/domain/inference.py`).
   The port has a real Anthropic Messages adapter (structured output via a forced
   tool schema) and a replay adapter for tests. Responses are cached by
   `(content_hash, policy_version, model_version)`. Model output is **data**: the
   code re-validates schema, grounding of every responsibility excerpt, and every
   hard exclusion. Instructions embedded in a description are text, never actions.
4. If the port is unavailable or the answer is ungrounded, the posting closes as
   `insufficient_evidence` (reopenable when a policy/model version or new data
   arrives). It is **never** approved and never parked in a human queue.

The model is never asked to approve. Approval is deterministic (`§8`).

## 7. Contact discovery and enrichment

1. Reuse a person already verified for this employer when the evidence is current
   (`people.email_verified_at` within `PERSON_EVIDENCE_TTL_DAYS`, default 45).
2. Use employer facts from Fantastic (headcount, industry) before paying Apollo for
   organization enrichment; enrich only missing facts.
3. Apollo People Search (documented 0 credits) by employer domain, falling back to
   `organization_ids[]` when the domain search returns nobody; buyer titles by
   function; candidates ordered direct manager → executive → founder (size-gated).
   Candidates already attempted for this opportunity, already approved anywhere,
   or suppressed are excluded **before** the broad search so they cannot block
   recovery.
4. Enrich (People Match, paid) the best untried candidate; on a negative gate
   outcome move to the next distinct candidate, up to
   `MAX_MATCH_ATTEMPTS_PER_OPPORTUNITY` (default 3, legacy value). Every attempt is
   a `candidate_attempts` row with reason and outcome. A negative result is never
   stored as a successful search.
5. `reveal_personal_emails=false`, `reveal_phone_number=false`. No waterfalls.
6. Every potentially chargeable call writes a `request_attempts` row **before** the
   HTTP request and a result after it. A timeout is `uncertain`, not free.

## 8. Approval (deterministic) and delivery

An Approved lead has: stable identities (posting, employer, opportunity, person);
obtained first and last name; employer with corroborated domain; buyer title in the
function's hierarchy; Apollo-verified professional email; an active compatible
posting with responsibility evidence; the exact campaign id; every field the
Control-A copy needs (`open_role`, `role_focus`, company display name, HM title);
sources and dates; the policy version; and a passing suppression check at approval
time.

The approval and its two outbox items (`airtable`, `instantly`) are written in
**one transaction**. Consumers are idempotent: before any send they re-check
suppressions, approval validity and campaign availability; on restart an
`in_flight` item is **reconciled by stable identity** (`Lead Key` in Airtable,
email + campaign via Instantly `search-by-contact`) before anything is re-sent. A
create receipt is not "emailed"; enrollment is not delivery; both are receipts.

Airtable rows are written with the legacy field names, `Status = Approved`, and
`Validation Version = tgtc-core/<policy>`. That version string is deliberately
**not** the legacy `config.VALIDATION_VERSION`, so the legacy Approved Sync worker
classifies these rows as *legacy* and skips them without a write
(`airtable_client.approved_row_eligibility`). This is the structural mutual
exclusion between the old and new delivery consumers; the operational one (stop
the old service) is in the deployment plan.

## 9. Metrics

Recorded separately, from tables, never by subtracting reasons: billed rows per
source; unique ids received; duplicates per stage; new postings; active compatible
postings; opportunities; candidates found; people enriched; valid emails; distinct
Approved; Airtable receipts; Instantly enrollments; and (when ingested) replies,
meetings, opportunities, contracts. Absence of evidence is reported as unknown,
not zero.

## 10. Out of scope for this task

Deploying, merging, changing live configuration, buying credits, creating a paid
PostgreSQL service, sending any message, running paid acquisition, rewriting copy,
adding campaigns, Wave 1 challenger routing inside the new consumer (hook exists,
default no-op), and the 145 direct ATS scrapers.
