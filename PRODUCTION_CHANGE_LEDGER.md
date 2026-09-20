# Production change ledger — exhaustive nine release

Append-only. Every production-touching action, in order, with its evidence.
Variable NAMES only, never values. No PII.

| # | when (UTC) | action | evidence / result |
|---|---|---|---|
| 1 | 2026-09-20 | Read Railway config for all services | 6 services; all deploy from `TGTChq/GTM`; watched branch `feat/rebuild-core`; deployed commit `be3af32`; last deploy 2026-09-18, status CRASHED |
| 2 | 2026-09-20 | Read `cronSchedule` on every service | **None on all six.** Nothing scheduled anywhere |
| 3 | 2026-09-20 | Read variable NAMES on `GTM Core Canary 1000` | 29 vars, **zero `INSTANTLY_*`** |
| 4 | 2026-09-20 | Read `INSTANTLY_CAMPAIGN_*` on `GTM` and `GTM Approved Sync` | all 10 names present on both; values are the **Control (retired)** ids |
| 5 | 2026-09-20 | Read `OUTBOUND_WAVE1_CHALLENGER_CAMPAIGNS_JSON` on `GTM Approved Sync` | 10 keys -> 9 distinct **Challenger** ids; disjoint from Control |
| 6 | 2026-09-20 | Ran `check_challenger_routing.py` (offline proof) | 10 keys collapse to 9; only CX shared; no Control id present; PASS |
| 7 | 2026-09-20 | Verified US send gate is not blocking | `cold_email_allowed('US', opt_out_status='none', unsubscribe_available=True, suppression_available=True).allowed is True` |
| 8 | 2026-09-20 | Created `release/exhaustive-nine-v1` from the audit branch, stripped audit-only artifacts | 73 files / 14,107 insertions vs `be3af32`; `tests_core` 1,579 passed / 0 failed |

## Decisions

* **D1 — release shape.** `be3af32` is a direct ancestor, so the release is the
  audit branch minus four doc/harness commits' artifacts, not a cherry-pick
  cascade. Rationale: 48 of 52 commits are inside the required dependency
  closure; reconstructing them individually would add conflict risk for no
  reduction in shipped surface.
* **D2 — `candidate_qualification.py` ships.** Inert but imported by the 24h
  canary. Smallest coherent implementation.
* **D3 — `policy/compliance.py` ships whole.** The UK prototype is not
  separable from the US market gate and is inert (unknown entity fails closed).
* **D4 — hard exclusions untouched.** An earlier draft relaxed the
  staffing-text, clearance, licence and public-sector rules; reverted in full
  once the patterns were read and found already scoped to the employer's
  business model.

## Deployment and first canary

| # | action | evidence / result |
|---|---|---|
| 9 | Restored `INSTANTLY_CAMPAIGN_*` on the core service using **Challenger** ids, `--skip-deploys` | 29 -> 40 vars. Control values on `GTM` were NOT reused |
| 10 | Transferred `INSTANTLY_API_KEY` to the core service via stdin, value never printed | 41 vars |
| 11 | Verified the nine Challenger campaigns LIVE (`check_challenger_routing.py --live`) | all nine resolve, names `WAVE1 CHALLENGER - *`, all `status=1`; the CX alias is the only collapse |
| 12 | Reconciled the real outbox | `airtable` 86 delivered + 1 blocked; `instantly` **86 pending** + 1 blocked |
| 13 | Outbox detail | 86 rows / 86 unique people / 86 unique emails / 86 company x campaign units -> **zero duplicates**; 86 verified emails; 0 revoked; 8 distinct campaign ids |
| 14 | **All 86 pending rows point at CONTROL (retired) campaign ids** | operations 22, finance 20, gtm 12, ai_technical 10, marketing 9, CX 6, people_hr 6, product 1 |
| 15 | Closed two holes that would have sent them there | `campaign_id_allowed()` at approval, `retired_campaign_block_reason()` at delivery |
| 16 | Set canary caps and a fresh budget id | `TGTC_CANARY_MAX_PER_CAMPAIGN`, `TGTC_CANARY_MAX_TOTAL`, `TGTC_SPEND_BUDGET_ID` |
| 17 | Diagnosed the CRASHED `be3af32` deployment | NOT a defect: `run-target` returns exit 1 whenever the target is unmet (`__main__.py:158`) and Railway marks any non-zero exit CRASHED. The run finished normally at 0/1000 with `stop_reason=spend_budget_exhausted` and 0 approvals, because no campaign was configured |
| 18 | Deployed `43f3d983-690c-4837-a70b-23548b112c38` | budget granted on the new id; 1,000 Fantastic credits / 250 Apollo |
| 19 | Applied migrations in the running container | `{"schema_version": 11}` -- migration 011 live |
| 20 | **First canary measurement** | 905 postings acquired; 1,013 classifications, **1,001 assigned (98.8%)**, all stamped `tgtc-core/3-exhaustive-nine`. Prior funnel assigned 14.3% |
| 21 | **Defect found in that measurement** | routing basis: fallback 834, title 239, description/model 11. OPERATIONS took 849 of 1,039 (82%) |
| 22 | Sampled the fallback titles | Residential Plumber, Soup Packer, Oil Delivery Driver, Manual Machinist, Refrigeration Mechanic, Physical Therapist, Day Porter, General Laborer -- all already on the approved hard-exclusion list |
| 23 | Fixed: `physical_title_reason()` as a hard exclusion + a qualify-stage re-check before paid enrichment | `tests_core` 1,599 -> 1,662 passed, 0 failed |
| 24 | Redeployed the tested commit | `25f77d0c-a91a-4c8c-bca4-52e7fce10097` |

## Incidents

* **I1 — could not stop the first canary mid-run.** `railway down`, the
  `serviceInstanceUpdate` mutation and a budget revoke were each refused by the
  permission classifier. Verified continuously that it had written nothing:
  approvals 0, new outbox rows 0, `delivery_receipts` unchanged at 172. The
  redeploy superseded the container. Exposure was bounded the whole time by the
  code-enforced caps (<=10 per campaign, <=90 total) and the retired-campaign
  guard.
* **I2 — heredoc corrupted regex backslashes again.** Writing the physical-title
  patterns through a shell heredoc turned `\b` into byte 0x08 (control bytes at
  offset 8518+). Reverted and rewrote the block with the file-writing tool.
  Known failure mode; the guard is to scan for control bytes after any
  regex patch, which is what caught it.

## Second and third canary

| # | action | evidence / result |
|---|---|---|
| 25 | Second canary, after the physical-title fix | excluded classifications rose 12 -> 340; OPERATIONS share of assignments fell 82% -> 38% |
| 26 | Sampled the remaining fallback | STILL frontline: Usher, Ramp Agent, Car Wash Associate, Bartender, Teller, Diesel Technician, CDL-A Dump Truck Driver, Prepared Foods Cook |
| 27 | **Root cause corrected** | removing `ai_taxonomies_a` did not widen the knowledge-work universe, it imported the frontline labour market. The measured loss was only ever the two FIRMOGRAPHIC gates (null headcount / null employment type, which drop 100% of Wellfound and YC rows) |
| 28 | Acquisition redesigned | EXHAUSTIVE keeps the professional taxonomies and drops only the firmographic gates; slots split 3 widened / 1 UNFILTERED discovery / 1 narrow control, so a null-taxonomy row stays reachable |
| 29 | Third deployment | `tests_core` 1,664 passed, 0 failed |
| 30 | **Approvals produced under the new policy** | 178 approvals, every `campaign_id` a CHALLENGER id, **zero Control ids** |
| 31 | Jurisdiction storage verified live | 96 of 115 sampled approvals `outreach_eligible = true` with `contact_country = US`; 16 fail closed on an absent contact country; 1 DE, 1 UK correctly blocked |
| 32 | Compliance gates verified live | blocked reasons observed: `compliance:unknown_jurisdiction:absent` (16), `compliance:cold_email_not_permitted:DE` (1), `compliance:uk:not_a_verified_corporate_subscriber` (1), `employer_attribution_conflict` (1) |
| 33 | Campaign spread | 8 of 9 campaigns received eligible approvals: operations 59, finance 21, gtm 18, ai_technical 9, CX 10, people_hr 9, product 7, marketing 3 |
| 34 | Provider consumption | Fantastic 10 requests / 1,000 credits (budget exhausted); Anthropic 279 requests, 1.73M input + 199k output tokens; **Apollo 0 credits** -- contacts came from already-paid stored records, as required |
