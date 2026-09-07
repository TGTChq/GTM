# Company identity and recovery review — 2026-09-07

Base: `ecfff2053babe074e0787336192590f885d93c53` (PR110), still deployed.
This follow-up is LOCAL: the connected GitHub create-tree API again returned
403 `Resource not accessible by integration`. No remote commit/PR/release exists.
User authorization is present; this is an integration access blocker.

## Findings established from the original execution

Original calibration: `20260906T202534Z-0395cf0a`.
Retained file: `enrichment/enrichment/jobs_enriched_2026-09-06.json`, 1,869,595 bytes,
SHA256 `a3469ca6b1cc6509d4b7bffb59179cae25ffec4330e5a11e53326a1e73e2aa28`.
The full JSON was reconstructed from 19 successful supported Railway file reads,
offsets 0..1,800,000, each at most 100,000 characters. No guessing from fragments.
Tool provenance and selected original runtime logs are in
`evidence/company_identity_access_20260907.json`. The private original is in
`TGTC-calibration-original-evidence.zip`; do not commit its contact data to GitHub.

### 1. Distinct ATS tenants were merged as one employer

Rows 0–7 name eight different employers on isolvedhire.com tenant subdomains but
carry canonical employer Hyundai MOBIS. Rows 15–19 name five different employers
on applicantpro.com but carry canonical employer Cutrale. The stage's five processed
company keys include both shared platform roots. Domain normalization removed tenant
identity, and the intermediary denylist omitted both providers, even though
`job_signal.ATS_DOMAINS` already recognized them as ATS platforms.

This is a demonstrated internal acquisition-to-enrichment loss and identity error.
The 13 surviving lead representatives mapped to TWO enrichment groups; the corrected
production helper maps them to THIRTEEN employer-name keys, retaining their real
names and falling back to the existing name-based resolver. No domain is guessed.
The lost source opportunities cannot be reconstructed from these representatives;
no 504-posting or daily output improvement is invented.

Correction: one `source_domains.ATS_DOMAINS` registry drives both source recognition
and employer-domain rejection, including callers with an empty local denylist.
ATS listing URLs remain usable job evidence. Genuine company career subdomains
still group normally. False collapsed domains can no longer drive cross-employer
company/function suppression, account enrichment or contact-domain decisions.

Official provider confirmation: https://www.applicantpro.com/ and
https://www.isolvedhcm.com/talent-acquisition/applicant-tracking-system .
The source-family registry was already in deployed job_signal; it is not a new
speculative provider catalog.

### 2. The two verified contacts hit a false display-identity hold

Original indices 23 (operations) and 24 (people_hr) are distinct contacts at Resource
Management Concepts, Inc. Both original records have job/role/account/contact/email
PASS and display NEEDS_CHECK. Their actual display evidence is
`linkedin_slug_domain_disagreement` for `resource-management-concepts-inc-` versus
`rmcweb.com`, with no role hold.

First-party review: https://rmcweb.com/ names Resource Management Concepts (RMC);
https://www.linkedin.com/company/resource-management-concepts-inc-/ links to that
same domain. The existing reviewed-alias mechanism now includes that exact pair
with source provenance. It does not generalize acronym matches or change ICP.

`acceptance/replay_retained_company_identity.py` replayed BOTH original rows:
retained company hold -> FINAL_PASS -> mapped Status Approved -> send_safe=True.
It preserved the other five recorded gate decisions and used the actual decision
engine, mapper and send-safe guard. Network blocked, local test signing key,
no provider calls or production writes. This is retrospective offline eligibility,
NOT a live approval, revalidation of old contact facts or original signature proof.
The result and exact source hash are in `evidence/company_identity_replay_20260907.json`.

The old proposed branch was not missing: deployed resolver and
`fix/company-anchor-conflict` shared blob `48b86b382688cdce06ab5ddf14d8bfc691c6a249`.
Bridge commit `2a067ec` predates calibration build `241572c8`. Re-merging that old
branch would have changed nothing for these rows.

### 3. Cache reuse crossed the employer identity boundary

A cached Acme/acme.com decision was reused with the same slug and globex.com,
including manual entries; a manual entry marked identity_safe=False also passed.
Both failures were reproduced before correction. Reuse now requires all provided
identities to belong to the cached reviewed set, identity_safe=True and an accepted
confidence. A changed anchor falls back to fresh resolution; manual review remains
sticky for its actual identity set. Existing legitimate overrides remain supported.

### 4. Custody copies inflated pending inventory and consumed import limits

The production maintenance snapshot reports 5,595 stored rows in three run files,
contradicting the earlier unqualified 3,595 count. The importer checked only custody
owned by the artifact's own run, so recovered postings could be imported again under
a recovery run. The reader already deduplicated globally; the summary did not.

Local reproduction: four stored records containing three identities were reported
as four pending postings; an import limit of one bought no progress past an already
held prefix. The correction checks all current custody identities before slicing
artifact adoption and reports pending unique identities separately from stored rows,
duplicate copies and unidentifiable records. Existing copies are preserved; no
production state reset or cleanup was performed. Our first pass already reported
5,595 and imported zero postings; these extra copies predate this turn.
The current production UNIQUE
count needs the corrected summary executed; do not relabel 5,595 as unique inventory
or assume the overlap from subtraction alone.

## Maintenance execution and restored state

Two bounded production passes ran on the unchanged base code:
- `7ed3bf76-d40a-44dc-848a-1b45ba616294`, runtime 01:36–01:38 UTC.
- `0d36f95d-9d68-406a-abfa-d9bb297be155`, runtime 01:44–01:46 UTC.
Both: MAINTENANCE COMPLETE rc=0 and artifacts-versus-ledger A/B ACCEPTED True.
September 6 recovery still reconciles 226 captured / 226 retained / 226 distinct,
unavailable=[] and recovery_agrees=true. Backups were preserved before maintenance.

The restored GTM deployment is `adc36f86-0212-4ea6-aaa1-4bb0b0ec69be`, SUCCESS on
ecfff20, cron `0 3 * * *`. Approved Sync is unchanged on ecfff20, deployment
`90bef032-4113-46b4-b56c-6d795a0a6191`, cron `0 0 * * *`.
MAINTENANCE_ONLY=1, FANTASTIC_JOBS_ENABLED=0, recovery budget enabled and calls=0 were
explicitly enforced. Probe variables cleared. Start command unchanged. Supported
agent subtools performed file reads only; no new paid acquisition/enrichment,
Airtable/Instantly delivery, message sending, credit purchases or budget increase.
Maintenance's Instantly collector is read-only. Trailing weekly report exited
because Monday is not Friday; the exact log says nothing written.

Brett's current partial-window report prints 6,431 captured, 1,050 contacts found,
769 sent to Instantly; missing full-period review/qualification metrics remain
missing. A/B compares the same window using a ledger with nine entries, including only
the runs eligible for that window. This is
not the completed Aug28–Sep04 weekly retrospective and was not sent.

## Acceptance limits and next steps

The changes repair reproduced internal failures. No 1,000-new-approved-per-run
production outcome exists. Prior 99.1% first-party readiness and five-company
conversion interpretations are withdrawn: those helpers accepted shared ATS roots.
Provider count windows remain observations, not a stable capacity ceiling.

Publish this reviewed patch through existing supported repository write access,
run normal CI/release gates, and verify both deployments without activating paid
processing. Then bounded maintenance can run the new custody summary and replay
retained source cohorts with corrected company/function identities. Reuse retained
contact work through the established guarded recovery path; do not force-approve
historical rows or buy another calibration to explain a deterministic hold.
A new paid run still requires an explicit credit grant and reconciled real credit
costs; the exhausted 50-counter grant remains zero. Continue demonstrated internal
repairs without weakening qualification, resetting dedupe, inventing a daily ceiling
or counting offline mapped rows as delivered production leads.

## Final local gates

3,399 tests and 1,001 subtests passed with ci_no_network blocking sockets/DNS,
empty environment, in 74.94 seconds. Earlier failing regressions demonstrated the
shared ATS collapse, unsafe cache reuse and cross-run custody overcount before
correction. Focused recovery integration: 133 passed. Integrity: 27 checked,
zero mismatches/absent files. Undefined-name gate and git diff --check passed.
The original-row replay also reran after final review with the same source hash
and the same two send-safe Approved mappings; original source bytes unchanged.
