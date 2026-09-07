# Why the calibration's two verified contacts were withheld

Run `20260906T202534Z-0395cf0a` produced two verified contacts and created **zero**
Airtable rows. Delivery recorded only `send_safe_withheld: 2` — an aggregate with no
reason — so the cause was unknown for a day. A larger Apollo budget could never have
explained it, because the withholding happens after the contact is found.

Answered 2026-09-07T00:35Z by rebuilding each retained lead's Airtable fields with the
production `_job_to_fields` and re-asking `send_safe_facts`, which is deterministic,
offline and fail-closed and returns the **first** failing fact. No provider was
contacted; nothing was written or repaired.

## Correction after deployment/source verification (2026-09-07)

The previously proposed next step, deploying `fix/company-anchor-conflict`, would
change no resolver code: the file is byte-identical to deployed main, blob
`48b86b382688cdce06ab5ddf14d8bfc691c6a249`. Commit `2a067ec` already introduced the
bridge before calibration commit `241572c8`. The old branch being unmerged does not
mean its code was never deployed. The 14 historical recoveries belong to another
cohort and do not establish that these two contacts can be released.

Also, `_job_to_fields` generates a NEW fingerprint, and can supply missing version
and timestamp values from current configuration. The replay establishes the current
first failing gate on retained lead facts; it does not verify a historical Airtable
fingerprint. The logs did not export `_outbound_company_evidence` for either row,
so at that stage their specific company-identity failure remained undetermined. See
`COMPANY_HOLD_REVIEW_2026-09-07.md` for the corrected evidence and follow-up.

## Original per-lead answer recovered (2026-09-07T01:44Z)

Both are Resource Management Concepts, Inc., original jobs indices 23 (operations)
and 24 (people_hr), with distinct addresses. Their retained resolver evidence says
`linkedin_slug_domain_disagreement` for LinkedIn `resource-management-concepts-inc-`
and domain `rmcweb.com`. The company website identifies Resource Management Concepts,
and the exact LinkedIn page links back to rmcweb.com. An exact reviewed alias clears
this display ambiguity, without inferring arbitrary acronym matches.

Offline replay of the ORIGINAL rows: both become FINAL_PASS, mapped Status Approved,
and pass send_safe_facts. All five other gate decisions remain the recorded ones.
The mapper uses a local test signature; no historical signature or live approval
claim follows. Production has not received this local patch (integration API403).

A separate cache defect let an approval for one slug/domain pair survive a domain
change. The local fix requires every provided identity to belong to the cached
reviewed set, including manual entries, and never promotes identity_safe=False.

## Original category result

    examined 26   send_safe 0   withheld 26

    missing_email                      24
    outbound_company_held_for_review    2   <- the two verified contacts

Both verified contacts, field by field:

| fact | value |
|---|---|
| Final Decision | `NEEDS_CHECK` |
| Email Validation | **PASS** |
| Contact Alignment | **PASS** |
| Apollo Email Status | **verified** |
| Validation Version | `tgtc-ready-v1.4.7-role-display-2` (current) |
| Validation Fingerprint | present |
| **Outbound Hold** | **true — company side** |

**Nothing was wrong with the contact.** The email passed the gate, the contact aligned
with the employer, Apollo verified the address, and the row carried a newly rebuilt
signed fingerprint. It was withheld because the **outbound COMPANY display could not
be resolved at high or medium confidence**, so the row is held for human review.

`send_safe_facts` names which side of the hold fired — company, role, both, or a stale
flag with no current condition. Here it is the company side alone.

## What this rules out

* **Not the Apollo budget.** The withholding is downstream of contact discovery.
  Spending more would produce more contacts and withhold them the same way.
* **Not email verification.** `Apollo Email Status: verified`, `Email Validation: PASS`.
  This is further evidence against a second verification provider being the lever.
* **Not a fingerprint or version problem.** The rebuilt fields pass current signature/version checks; the original request fingerprint was not inspected.
* **For these two contacts**, the retained account gate was PASS. The five-group aggregate is not five distinct employers: see the ATS identity correction below.

## What it points at

The company-display resolver. This is a **known blocker class**: on 2026-08-28 the
entire Approved backlog was found unenrollable with 133 of 153 company holds caused by
`linkedin_slug_domain_disagreement` — branded domains disagreeing with the LinkedIn
slug. A narrower fix exists on `fix/company-anchor-conflict` (recovers 14 rows across
12 companies with zero name-text changes) and **was already present in the calibration build**; a blanket
recompute was assessed as unsafe because it rewrites two rows to a different company.

So the sample is small — two rows — but it lands on an already-documented,
already-quantified constraint rather than a new one.

## The other 24

`missing_email`, matching the run's own `no_contact 24`. Those leads had no address, but this is not proof
of natural hiring-manager coverage. Thirteen retained rows name distinct employers
whose ATS hosting domains were mistakenly used as employer identities. Two shared
platform groups inherited unrelated canonical accounts before contact search.
That internal preparation error is corrected locally by one shared ATS registry;
provider coverage remains unmeasured for the repaired employer groups.

## Evidence preserved

The per-lead files that answered this are now copied out of the pruned tree to
`maintenance_backups/20260907T003521Z/per_lead/20260906T202534Z-0395cf0a/`, nothing
skipped:

    enrichment/enrichment/jobs_enriched_2026-09-06.json            1,869,595
    enrichment/enrichment/enrichment_progress.json                 1,328,571
    enrichment/enrichment/hiring_manager_failures.csv                  7,478
    enrichment/enrichment/hiring_manager_summary.json                    823
    enrichment/qualification/jobs_contact_eligible_...json         26,285,427
    enrichment/qualification/jobs_precontact_nonpass_...json        2,285,444

Retention prunes under `run_artifacts`, so without this the evidence explaining the
decision would have disappeared while the decision's summary survived it.
