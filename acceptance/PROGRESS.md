# TGTC completion — progress record

Living record. Survives compaction. Reopen a closed item only on contradictory
evidence.

**Deployed:** `origin/main = 6da817b`.
**Local unpushed:** `2d67782` (`MAINTENANCE_ONLY`).
**Acquisition:** PAUSED (`FANTASTIC_JOBS_ENABLED=False` on GTM). Billing untouched.

---

## Blockers (external — record the exact action, then work elsewhere)

| # | blocker | exact action needed | blocks |
|---|---|---|---|
| B1 | `git push` denied by auto-mode classifier (succeeded many times earlier this session) | user adds a Bash permission rule for `git push` | landing any further fix |
| B2 | `railway api` **mutation** denied (cron no-op succeeded moments earlier, so authorization not syntax); reads mentioning `startCommand` also denied | user adds a Bash permission rule for `railway api`, or performs the settings change themselves | executing maintenance on the production volume |
| B3 | Apollo lead credits exhausted — `BILLING.LIMIT.CREDITS_EXHAUSTED`, `credit_balance 0`, `credit_type "lead credits"`, refuses rather than billing overage | five billing screens listed in `INCIDENT_2026-09-06_apollo_credits.md` | live enrichment, contact discovery, approval yield |
| B4 | production volume unreachable between runs (`railway ssh` + `railway volume files` both need a live container) | resolved by B1+B2 via `MAINTENANCE_ONLY` on the existing 03:00Z cron | real-artifact reporting acceptance, 09-06 recovery |

---

## Closed (evidence in repo; do not reopen without contradictory evidence)

| item | outcome | evidence |
|---|---|---|
| Apollo stop is provider-side, not our misclassification | Apollo's own `error_details.code` agrees with our label | `INCIDENT_2026-09-06_apollo_credits.md` |
| Apollo evidence was being discarded | `error_details.code` + `context` now extracted and logged | PR #99 |
| Custody of paid-for work | `pending_work`, sibling of `run_artifacts`, prune cannot reach it | PR #100 |
| Custody ordering vs continuation | hook runs before `_save()`; a failed hook leaves the cursor put | PR #102 |
| Expiry is not completion | `expired/` archive + `_audit.jsonl` with three distinct outcomes | PR #102 |
| Duplicate semantics | duplicates are cross-run re-deliveries correctly suppressed; `seen_ids` is SEEDED | PR #104 |
| Enforced cardinality | Airtable = company×function; enrollment = person-employer PAIR; **no one-lead-per-employer rule** | PR #106 |
| Approval mechanism | automatic, fact-based, `FANTASTIC_AUTO_APPROVE_SEND_SAFE=True` both services | PR #105 |
| Delivery preflight honesty | hardcoded `(Pending) auto_approve=OFF` replaced with real config | PR #105 |
| Coverage assessed on every stop | diagnostics only; recovery is the drained-stop rewind | PR #105/#106 |
| Maintenance entry point | written, verified locally rc=0 / A/B ACCEPTED | PR #107 |

---

## Open work

| # | item | state |
|---|---|---|
| W1 | Execute maintenance on production volume | blocked B1+B2 |
| W2 | 09-06 recovery + reconciliation | blocked B4 |
| W3 | Real-artifact vs ledger-only acceptance | blocked B4 |
| W4 | Verify provider contract against current official docs | **in progress** |
| W5 | Expired-unacquired loss (25% in fixture) — determine avoidable share | **in progress** |
| W6 | Source budget allocation vs measured yield | open |
| W7 | Capacity: company/function + contact identities on comparable windows | blocked B3/B4 for contacts |
| W8 | Integrated review for omissions/contradictions | open |

---

## Log

### W4 — provider contract verified against current official documentation (2026-09-06)

Checked `developer.fantastic.jobs` and the vendor's own API pages. The documented
pagination contract is:

> "To retrieve all jobs for a time_frame, you might need to do multiple requests
> while increasing the offset, with a preferred limit between 100 and 1,000."
> "If the number of jobs returned is less than the limit, do another request while
> increasing offset by the limit, and keep making requests until the API returns
> less jobs than the limit."

**That describes draining a result set in ONE pass.** Nothing in the documentation
states that an offset remains meaningful across requests separated in time, and
nothing documents a stable sort order for the active-jobs endpoints.

**Consequence, and it is the root of the expired-unacquired loss:** our durable
cross-run `window_offsets` cursor is an EXTENSION BEYOND THE DOCUMENTED CONTRACT.
We resume an index into a result set the provider never promised would be stable,
and the 7-day frame floor guarantees it is not.

Also confirmed: the count endpoints use a different `time_frame` set (1m default,
no 7d option) -- our count probes pass explicit `date_created_gte/lt`, so they are
unaffected.

`date_created_gte` / `date_created_lt` ARE honoured (proven by our own count probes
returning distinct counts for distinct 24h ranges). A `date_created` boundary is
STABLE in a way an offset is not: a row's `date_created` never changes.
