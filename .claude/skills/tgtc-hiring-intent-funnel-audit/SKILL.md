---
name: tgtc-hiring-intent-funnel-audit
description: Use when auditing, replaying, measuring or changing any stage of the TGTC hiring-intent pipeline - Fantastic.jobs acquisition or query arms, job qualification or campaign mapping, employer identity or dedupe, Apollo contact discovery, approval, capacity, cost or lead-volume claims - and before reporting any jobs, companies, contacts or leads-per-day number.
---

# TGTC hiring-intent funnel audit

## Overview

A number is reportable only if three things hold:
- every record behind it is accounted for at every stage;
- its unit is named;
- every quality claim is measured against frozen, independent labels.

Violating the letter of these rules violates their spirit.

Reference files in this directory; read the one your task needs:
- `funnel-accounting.md`: stages, record schema, reason codes, artifact columns.
- `provider-contracts.md`: Fantastic and Apollo contract-check procedures, plus known facts with their status.
- `experiments-and-evaluation.md`: campaign × geography arms, golden set and holdout, metrics, change loop, cost.

## Fixed definitions (Luis; do not change to hit a number)

**Campaigns (9, authoritative 2026-09-19).** They equal production's `tgtc_core/policy/campaigns.py`:

| Campaign | function keys | Campaign | function keys |
|---|---|---|---|
| Product | `product` | Customer Experience | `customer_success`, `customer_support` |
| Operations | `operations` | Marketing & Creative | `marketing` |
| Finance | `finance` | GTM Systems & Revenue Automation | `gtm_revenue` |
| People & HR | `people_hr` | AI & Technical Automation | `engineering` |
| Ecommerce | `ecommerce` | | |

- Product and Operations are separate campaigns and never count for each other.
- A job that fits two campaigns gets one `primary_campaign` and one `secondary_campaign`. The job, its company and its contacts count once, under the primary campaign.
- The old "four groups" (AI/Eng, GTM/RevOps, Marketing, CS) are NOT the scope. A metric built on them is void.

**Geographies:** US, UK, DE, AE, SA. That makes 45 campaign × geography cells, each measured on its own.

**Company:** 25–1,000 employees. Keep the exclusions for CRM, staffing/RPO, true intermediaries and approved industries.

**Role rules:**
- The work must be deliverable remotely. Remote, hybrid and onsite labels are data, not gates.
- Never require an exact title.
- Never reject on Senior, Lead, Director, Head or VP alone.
- Unknown or ambiguous records are never approved.

**Approved exclusions** (re-verify against labels): required federal clearance; government employers; quota-carrying sales (unless RevOps, Sales Ops or GTM-systems work is primary); essential field travel; third-party reposts.

**An approved contact must be:**
- a unique person;
- currently at the true hiring company;
- functionally relevant to the opening;
- reachable at a verified work email (Apollo `verified` only);
- absent from CRM, Instantly and suppression history;
- linked to an active qualified vacancy;
- legally eligible for outreach in its geography. Without a documented legal basis the record is `unknown`.

Target 2–3 role-diverse contacts per company × campaign: the functional owner, the executive leader, and a TA/People leader when appropriate. Equivalent people do not diversify.

## Units: never add across rows

1. provider records returned / billed
2. unique jobs
3. qualified jobs
4. unique hiring companies
5. company × campaign units
6. contacts found
7. contacts with a verified work email
8. historically net-new contacts
9. final approved contact leads

Contacts never become jobs, companies or opportunities. A person counts **once globally**: not once per job, per campaign, per company × campaign unit or per day. A person relevant to two campaigns at one company is still one lead.

## Verification gates (all must hold before a claim)

1. **Reconciliation.** Every stage satisfies `input = pass + reject + unknown + duplicate + error`. A residual is reported as `unexplained`, never absorbed.
2. **Provenance.** A number cites its evidence file, the command that produced it and `rule_version`.
3. **Precision.** Precision and recall come from the frozen holdout. Calibration, spot checks or self-review don't count.
4. **Recall.** Recall is estimated against a broad control arm, not only against the rows the filter already kept.
5. **Measurement.** A capacity claim is measured over several days. A one-day extrapolation is labelled a projection, as are projected contacts.
6. **Spend.** Paid calls, deploys, Airtable/Instantly writes, credential changes and frozen-rule changes all pause for Luis. The pause states the experiment, its expected consumption and the decision it resolves.

## Prior conclusions are hypotheses, not facts

- Memory entries, earlier REPORT.md files and canary verdicts are a **hypothesis inventory**.
- A prior figure may be cited only after it has been re-derived in the current audit, with its evidence path and `rule_version`.
- Every four-group figure is void. That includes "C7+D+B", "C7+D", "250 four-group qualified", "3rd contact 35%", "80% of units exhausted after the first visit", "~911–974/day", "400–500/day", and the verdicts MORE_PROVIDER_BUDGET_REQUIRED and CONTACT_TARGET_NOT_SUSTAINABLE. Do not reuse them, even as "data we already have".

**Do not conclude that the market, budget or plan is insufficient until all of these hold:**
1. every Fantastic contract is verified;
2. the full funnel reconciles;
3. false negatives are measured;
4. each of the 9 campaigns has its own optimized query;
5. every one of the 5 geographies has a free count matrix;
6. broad recall controls are evaluated;
7. identity and dedupe defects are fixed;
8. multi-contact economics are measured;
9. a multi-day shadow has measured net-new supply.

Before that point, state what is missing, not a verdict.

## Reward hacking: forbidden, whatever the deadline

Each of these is forbidden:
- counting unknowns, duplicates, projections or historical rows as approved or new;
- counting one person twice (across jobs, campaigns or days);
- turning contacts into jobs;
- merging distinct ATS requisitions;
- editing labels, the golden set, the holdout, metric denominators, thresholds, the approval definition, the geography allowlist, exclusions, the dedupe window or the contact maximum;
- adding a campaign or geography;
- raising contact depth silently.

| Rationalization | Reality |
|---|---|
| "The spot-check says most unknowns are good" | That sample is unlabelled and not frozen. Report unknowns separately. |
| "Those holdout labels are obviously wrong" | Send them to a blind re-review by the evaluator. You never relabel your own holdout. |
| "The unit test proves the filter is sent" | Sending ≠ applying. First check that the parameter NAME exists in the live docs: unknown names can be silently ignored. Then run the canaries in `provider-contracts.md`. |
| "The count narrowed when I added the filter, so it's applied" | Narrowing shows some effect, not the documented semantics. Also run an impossible value (must return 0), the opposite value, and a null-field probe (does it drop rows with missing data?). |
| "This rule rejects the most, so fix it first" | Rank by `good_jobs_lost = rejected × measured FN rate`, per campaign. |
| "Scope was four groups last session" | Void. Re-run the metric on nine campaigns. |
| "We already know the 3rd contact is 35% correct / inventory isn't the constraint / it needs more budget" | These are void four-group numbers or premature verdicts. Re-measure per campaign, or list what is still unmeasured. |

## Red flags: stop and re-check

- writing "verified", "matches the docs" or "applied" for a check not executed in this session. Write "not yet checked" instead;
- a residual between input and outputs;
- `n/a` read as 0;
- a figure with no unit or no evidence path;
- one config applied to all nine campaigns;
- US hidden inside a multi-country total;
- a fix bundled with another fix;
- a fourth change in the same layer without an architecture review.
