# Bounded closeout of loss-report correctness

Upstream base: `81d0dc3e65c9a717004e52c53e169e2edb5bbd56` (PR #118).
This patch changes loss interpretation and its tests, not acquisition, enrichment,
approval, suppression, custody, budgets or scheduling. It introduces no enable flag.

## What was reproduced and corrected

The new contract suite produced **22 failures and one passing zero-control** against
the unmodified upstream module. All 23 pass after the correction:

- Missing or invalid counters stay unknown; explicit measured zero stays zero.
- Agreeing aliases count once. Disagreeing aliases and overlapping populations do
  not become a guessed total. Categories cannot add different units.
- A missing contact is not evidence of an unsuccessful search. The HM summary's
  row-level flags are preserved as observations, not physical-request receipts.
- Writer rows do not substitute for distinct opportunities with outcomes. The
  run's stop reason does not assign a cause to each missing outcome.
- The unsupported 11-email-failures and four-interrupted-opportunities claims are
  withdrawn. Balanced duplicate counters do not prove correct duplicate decisions.

The current module is a reporting helper, not a production gate. These fixes do
not themselves increase lead output and do not claim to resolve the 27 writer holds.

## Verification

Python 3.12.13, no credentials, sockets and DNS blocked by `ci_no_network`:

- Focused reporting, identity and real-orchestrator capacity suite: **153 passed**.
- Full suite: **3,623 passed; 1,001 subtests passed**.
- Integrity: **33 checked / 0 mismatch / 0 absent**.
- Undefined names: **0**; `git diff --check` clean.

The existing capacity tests remain unchanged: 1,500 distinct Approved in a simulated
run, 3,524 pending postings drained across 2,000-row batches, no approval credit for
failed writes or unapproved rows, and pending work retained after budget interruption.
These prove behavior under simulated inputs, not commercial output of 1,000/day.

## Defaults versus effective production state

Code inspection at the pinned base confirms:

| setting | repository default |
|---|---|
| `RUN_APPROVED_TARGET` | `1000` |
| `RUN_APPROVED_CONTINUE_AFTER_TARGET` | `true` |
| `RUN_APPROVED_TARGET_ENABLED` | enabled when `NET_NEW_SEND_SAFE_TARGET > 0`, unless explicitly overridden |
| `PENDING_WORK_ENABLED` | `true` |
| `PENDING_WORK_RESUME_MAX_PER_RUN` | `2000`, applied per loop iteration |
| `FANTASTIC_JOBS_ENABLED` | `false` |
| `APOLLO_RECOVERY_BUDGET_ENABLED` | `false`; deployment must explicitly enable its durable ceiling |
| `APOLLO_RECOVERY_BUDGET_CALLS` | `0` |

No configuration value was changed. A repository default is not evidence of a
service's environment. The last effective-value printout available remains the
2026-09-07 07:02Z snapshot: maintenance active, paid acquisition paused and grant zero.
Read-only Railway verification found both latest deployments SUCCESS, created
09:10:44Z: GTM `c162323e-8f9b-4647-bfc5-dc5d19d2aacf`, Approved Sync
`d6a912b5-66be-4996-aa48-1d22957f2e60`. Their runtime log queries returned no entries,
so they provide no newer effective-value readback. Crons remain `0 3 * * *` and
`0 0 * * *`. No service mutation or paid request was performed.

## Publication handoff

This is a prepared patch, not a claim of deployment. The connected GitHub integration
previously rejected publication with `403 Resource not accessible by integration`;
no alternate write path was used to evade that restriction.

Apply the single patch on an isolated branch based on the upstream commit above
using authenticated repository access. Preserve any intervening commits; rerun the
repository CI gates, then publish through the normal PR process. Keep acquisition,
maintenance, billing and grant values unchanged. Do not reuse the spent grant.

Do not reopen a whole-system redesign to publish this report correction. Future
production changes should answer a reproduced loss of valid contacts, wasted spend,
lost pending work or failure to advance. Actual Approved receipts remain the output
acceptance; the historical 27 holds and duplicate attribution remain evidence gaps.
