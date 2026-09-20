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
