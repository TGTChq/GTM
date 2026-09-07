# Why the calibration's two verified contacts were withheld

Run `20260906T202534Z-0395cf0a` produced two verified contacts and created **zero**
Airtable rows. Delivery recorded only `send_safe_withheld: 2` — an aggregate with no
reason — so the cause was unknown for a day. A larger Apollo budget could never have
explained it, because the withholding happens after the contact is found.

Answered 2026-09-07T00:35Z by rebuilding each retained lead's Airtable fields with the
production `_job_to_fields` and re-asking `send_safe_facts`, which is deterministic,
offline and fail-closed and returns the **first** failing fact. No provider was
contacted; nothing was written or repaired.

## The answer

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
with the employer, Apollo verified the address, and the row carried a current, valid
signed fingerprint. It was withheld because the **outbound COMPANY display could not
be resolved at high or medium confidence**, so the row is held for human review.

`send_safe_facts` names which side of the hold fired — company, role, both, or a stale
flag with no current condition. Here it is the company side alone.

## What this rules out

* **Not the Apollo budget.** The withholding is downstream of contact discovery.
  Spending more would produce more contacts and withhold them the same way.
* **Not email verification.** `Apollo Email Status: verified`, `Email Validation: PASS`.
  This is further evidence against a second verification provider being the lever.
* **Not a fingerprint or version problem.** Both are current and valid.
* **Not the ICP.** All five companies reached were ICP-eligible; zero were rejected.

## What it points at

The company-display resolver. This is a **known blocker class**: on 2026-08-28 the
entire Approved backlog was found unenrollable with 133 of 153 company holds caused by
`linkedin_slug_domain_disagreement` — branded domains disagreeing with the LinkedIn
slug. A narrower fix exists on `fix/company-anchor-conflict` (recovers 14 rows across
12 companies with zero name-text changes) and **has never been deployed**; a blanket
recompute was assessed as unsafe because it rewrites two rows to a different company.

So the sample is small — two rows — but it lands on an already-documented,
already-quantified constraint rather than a new one.

## The other 24

`missing_email`, matching the run's own `no_contact 24`. Those leads had no address at
all, which is hiring-manager coverage and a different problem from the two above. Both
populations are real; they have different owners and different remedies, and the
aggregate `send_safe_withheld: 2` hid one of them entirely.

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
