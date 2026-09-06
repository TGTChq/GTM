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
| ~~B1~~ | ~~`git push` denied~~ **RESOLVED** — the denial was content-triggered, not standing | — | — |
| B2 | `startCommand` mutation denied (still) — NOT retried | none needed: routed around by design via `MAINTENANCE_ONLY`, a reviewed code path + an authorized variable | nothing |
| B3 | Apollo lead credits exhausted — `BILLING.LIMIT.CREDITS_EXHAUSTED`, `credit_balance 0`, `credit_type "lead credits"`, refuses rather than billing overage | five billing screens listed in `INCIDENT_2026-09-06_apollo_credits.md` | live enrichment, contact discovery, approval yield |
| ~~B4~~ | ~~volume unreachable~~ **RESOLVED** — `MAINTENANCE_ONLY` ran on the production volume 2026-09-06T10:20Z | — | — |

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
| W1 | Execute maintenance on production volume | **DONE** — `MAINTENANCE COMPLETE rc=0`, 2026-09-06T10:20Z |
| W2 | 09-06 recovery + reconciliation | **DONE** — net_new 226 / retained 226 / distinct 226 / unavailable [] / **agrees true** |
| W3 | Real-artifact vs ledger-only acceptance | **PASSED on production files** — `ACCEPTED: True` |
| W4 | Provider contract verified | **DONE** — durable cross-run offset exceeds the documented contract |
| W5 | Expired-unacquired loss (25% in fixture) — determine avoidable share | **in progress** |
| W6 | Source budget allocation vs measured yield | open |
| W7 | Capacity: company×function opportunities | **UNBLOCKED** — payloads retained (6,205 / 226); `capacity()` measures them offline; contacts still need B3 |
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


### Production maintenance pass — 2026-09-06T10:20Z (deployment `999f040e`)

`MAINTENANCE COMPLETE rc=0`. Evidence: `acceptance/evidence/20260906-maintenance/`.

* **09-06 recovery reconciled:** `net_new_jobs_captured 226`, `opportunities_retained
  226`, `identities_distinct 226`, `unavailable []`, **`agrees: true`**.
* **Reporting acceptance PASSED on production files:** `ACCEPTED: True` — artifacts+
  ledger and ledger-only produce identical text, values, statuses and units.
* **Census reconciles:** `jobs_captured 6431`, `jobs_reviewed 226`, `contacts_found
  1048`, `sent_to_airtable 781`, all `agrees=True`; `sent_to_instantly 769` measured.
* **Payloads are retained**: `postings.json` holds 6,205 (09-04) and 226 (09-06).

**Defect the run exposed, now fixed:** the pass constructed a `StateManager`, which
CREATES a run directory; the ledger backfill lifted that empty directory in as an
INTERRUPTED RUN, and the census counted it as an eligible run that failed to record
its metrics. That single phantom degraded every headline metric from `measured` to
`partial` and made Brett's report say "captured not measured" directly above a
census reading `jobs_captured census=6431 reported=6431 agrees=True`. Delegation now
happens before `RunContext`/`StateManager`; `--drop-empty-run` removes the one
already written, refusing anything carrying evidence.
