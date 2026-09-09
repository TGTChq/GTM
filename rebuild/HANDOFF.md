# TGTC core rebuild — handoff for independent review

Date: 2026-09-08. Author of the implementation: Claude (Fable 5.1), implementation owner
per `TGTC_REBUILD_BLUEPRINT.md §12`. Reviewer: Codex (independent QA).

**Evidence class of everything in this branch: implementation + offline validation.**
No live provider was called, nothing was deployed, no live configuration changed, no
credit was purchased, no message was sent. Where a claim below rests on simulated
providers it says so.

## 1. Where the work is

| Item | Value |
|---|---|
| Repository | `TGTChq/GTM` |
| Base | `main = 5d87851` (the blueprint's inspected commit) |
| Branch / worktree | `feat/rebuild-core` at `C:/TGTC/tgtc_rebuild` |
| Commit | `__COMMIT__` |
| Diff summary | `git diff --stat 5d87851..feat/rebuild-core` — new package `tgtc_core/`, new suite `tests_core/`, `rebuild/*.md`, `requirements-core*.txt`, one added CI job. **No legacy file was modified except `.github/workflows/ci.yml` (job appended).** |
| Preserved | the session's original worktree (`feat/row2-diagnostic-instrumentation`), every other worktree's uncommitted changes, all legacy modules, the integrity manifest |

## 2. What was implemented (paths)

| Blueprint component | Path | Notes |
|---|---|---|
| Contracts | `rebuild/PRODUCT_CONTRACT.md`, `rebuild/INTEGRATION_MAP.md`, `rebuild/ACCEPTANCE.md` | reconciled with the repository; every legacy default is labelled `pending confirmation` |
| Policy registry | `tgtc_core/policy/campaigns.py`, `tgtc_core/policy/requirements.py` | nine campaigns / ten functions, buyer hierarchies, 21 rules with provenance, `POLICY_VERSION = tgtc-core/1` |
| Storage | `tgtc_core/db/schema.sql`, `db/migrate.py`, `db/connection.py`, `db/work_queue.py` | 21 tables (all blueprint identities), `FOR UPDATE SKIP LOCKED` claims, token+version leases |
| Acquisition | `tgtc_core/providers/fantastic.py`, `services/acquisition.py` | fresh/backfill partitions, intent-before-call, rows+receipt+cursor in one transaction, duplicate-page hold, quota reserve, PII stripping, no title filters |
| Identity | `tgtc_core/domain/identity.py`, `services/identity_service.py` | domain/slug/name anchors, alias corroboration, cross-source duplicate linking |
| Facts + classification | `tgtc_core/domain/facts.py`, `domain/classification.py`, `domain/inference.py`, `services/classification_service.py` | deterministic facts → lexicon evidence → semantic port; title never scores; grounding re-validated in code |
| Semantic adapter | `tgtc_core/domain/inference.py::AnthropicAdapter` | official SDK, `output_config.format` JSON schema, prompt-cached system, refusal → unavailable; **live call unverified** |
| Discovery + enrichment | `tgtc_core/providers/apollo.py`, `services/opportunity.py`, `services/provider_state.py` | 0-credit search → ordered candidates → paid match per untried candidate → gates; durable refusal state; controlled retry; recovery on a served chargeable call |
| Gates + approval | `tgtc_core/domain/gates.py`, `domain/approval.py` | contact/email gates (Apollo-only verification), deterministic approval, refusal reasons, Airtable/Instantly payload builders |
| Outbox + delivery | `tgtc_core/providers/airtable.py`, `providers/instantly.py`, `services/delivery.py` | approval + outbox in one transaction; reconcile by `Lead Key` / `search-by-contact`; truthful Instantly membership; campaign status defers |
| Suppressions + outcomes | `tgtc_core/services/suppression.py` | legacy-compatible keys, idempotent imports, outcome events applied once |
| Scheduler + runner + CLI | `tgtc_core/services/scheduler.py`, `runner.py`, `__main__.py` | 80/20 fair share with borrowing; `python -m tgtc_core {migrate,describe,cycle,work,deliver,ledger,import-airtable,demo}` |
| Metrics | `tgtc_core/services/metrics.py` | ledger from tables; unknown ≠ zero |
| Simulated providers, corpus, scenario, load | `tgtc_core/testing/*` | labelled SIMULATED; shared by tests and `demo` |

## 3. Verification commands and results

Run from `C:/TGTC/tgtc_rebuild` (Python 3.12; `pip install -r requirements-core-dev.txt`).

```bash
python -m pytest tests_core -q
```
Result: `195 passed in 23.25s` (2026-09-08). Backed by an embedded PostgreSQL 16.2 (`pgserver`), simulated
providers. What it proves: the storage contract, the gates, the failure/recovery
scenarios in `ACCEPTANCE.md §1`, and that all nine campaign routes deliver. What it does
not prove: provider behaviour, conversion, capacity.

```bash
python -m tgtc_core demo
```
Result: 10 postings → 10 employers → 10 opportunities (nine campaigns) → 10 Approved →
10 Airtable rows + 10 Instantly leads, 30 simulated paid matches (3 per opportunity: the
wrong-company and unverified candidates fail the real gates first), 0 organization
enrichments (Fantastic supplied the facts). `evidence_class: SIMULATED_PROVIDERS`.

```bash
python -m tgtc_core.testing.load --events 50000 --burst 10000 --workers 4
```
Result: `__LOAD__`. Zero provider latency, so this is a software envelope only.

```bash
python -m pyflakes tgtc_core tests_core   # undefined names: none (unused imports only)
python ci_check_integrity.py               # legacy manifest unchanged
python -m pytest tests -q -p ci_no_network  # legacy suite: 3752 passed, 1 skipped, 1001 subtests passed (146.78s); manifest checked=35 mismatch=0
```

Defects the tests caught in this branch before it was handed over (kept as evidence
that the assertions bite): a duplicate page advanced the cursor past uninspected rows
(fixed: cursor holds, page re-requested, loop stalls); a provider-refused match counted
as an attempt against the candidate (fixed); a served 0-credit search flipped a refusing
provider to serving (fixed); the provider guard re-blocked the pass it had just
permitted (fixed); dropped search results were silent (fixed: recorded as attempts);
work items and outbox rows stamped with the DB clock were unclaimable under an injected
clock (fixed: every enqueue carries the run clock).

## 4. What remains unverified or undecided (exact)

| # | Dependency / decision | Owner | Why it matters |
|---|---|---|---|
| 1 | **PostgreSQL in production** — none exists; the Railway project is volume-backed JSON. Options: Railway Postgres plugin (paid) or external. | Luis (cost) | The core will not start without `TGTC_DATABASE_URL`. |
| 2 | **Inference credential and model/cost decision** — no `ANTHROPIC_API_KEY` available. Default model `claude-opus-5` (SDK guidance); `TGTC_INFERENCE_MODEL` overrides. Cost per description unmeasured. | Luis | Without it the deterministic layer decides what it can and closes the rest as `insufficient_evidence` (never approves). Coverage loss is measurable in `postings_close_reasons`. |
| 3 | **Independent classifier corpus** (≥95 % precision / ≥90 % recall targets) | Codex | The synthetic corpus gates regressions only. |
| 4 | **Apollo balance, plan pricing per operation** | Luis | `credit_events` carries estimates; confirmed usage must come from the invoice/usage page. |
| 5 | **Fantastic plan / quota headers for this account, and the coverage effect of `location=United States` and of dropping `title_advanced`** | Luis + one 0-credit `active-jb-count` call | The new feed is title-free; volume vs quota is unmeasured. |
| 6 | **Airtable field types** on the production table for the subset written (`Posted At`, `Employees`, `Job Age Days`) | first acceptance write to an isolated table | A 422 blocks the item with the field error; it never blocks other leads. |
| 7 | **Instantly Control campaign states** (ECOMMERCE was `completed` 2026-09-03) and sending capacity | Luis | Non-active campaigns defer delivery hourly; they never invalidate a lead. |
| 8 | **Policy confirmations** for every `legacy_default_pending_confirmation` rule in `policy/requirements.py` (size band 25–1000, excluded industries, seniority exclusions, US market, LinkedIn requirement, account-level suppression OFF) | Luis | Imported as starting policy; not business truth. |
| 9 | **Wave 1 challenger routing** inside the new consumer (hook exists, default no-op; Control-A payloads only) | later | Wave 1 stays where it runs today (Approved Sync); the new consumer does not double-deliver. |
| 10 | **Direct ATS scrapers (145 boards)** — deliberately not incorporated | — | blueprint §5 |

## 5. Handoff to Codex (independent review)

Review the exact commit above. Suggested order, invariants first:

1. **Conservation**: `tests_core/test_acquisition_pages.py` — try to construct a page sequence (overlap, repeat, failure, timeout, crash) that loses or skips a row; assert on `page_receipts.page_offset` and `postings.provider_job_id`, not counts.
2. **Delivery**: `tests_core/test_delivery_outbox.py` — try to produce a second Airtable row or a second Instantly enrollment for one approval under lost responses and restarts.
3. **Approval-only output**: `tgtc_core/domain/approval.py` — try to make `airtable_fields` emit anything but `Status=Approved`, or make `build_approved_lead` approve with an unverified email, a generic mailbox, a non-employer domain, or missing responsibility evidence.
4. **Title independence**: `tests_core/test_title_independence.py` — add postings whose decision flips when only the title changes; any such case is a defect.
5. **Identity**: `tests_core/test_identity_edge_cases.py` — try to merge two companies by name similarity, or split one company across a careers subdomain.
6. **Spend**: `request_attempts` / `credit_events` — confirm every chargeable path writes intent before the call (`test_intent_is_committed_before_the_provider_is_called`) and that nothing equates a request with a credit.
7. **Legacy exclusion**: `tests_core/test_legacy_consumer_exclusion.py` — confirm the legacy Approved Sync classifies a core row as `legacy` and writes nothing.
8. **Semantic layer**: `tgtc_core/domain/inference.py` — the model cannot approve, cannot remove an exclusion, and every responsibility excerpt is verified verbatim by code. Build the independent labelled corpus (dependency 3).

Each objection should carry a reproducible scenario (a test in `tests_core/`) or a concrete
difference from `PRODUCT_CONTRACT.md`.

## 6. Deployment / migration / rollback plan (NOT executed)

1. **Provision PostgreSQL** (decision + cost). Set `TGTC_DATABASE_URL`, `TGTC_CORE_SIGNING_KEY`
   (new, distinct from `VALIDATION_SIGNING_KEY`), the existing provider keys and the ten
   `INSTANTLY_CAMPAIGN_*` names on a **new Railway service** (`GTM Core`), image from the
   same repository with `requirements-core.txt`. `python -m tgtc_core migrate` is idempotent.
2. **Import history with provenance**: `python -m tgtc_core import-airtable` (reads the
   production table once; writes only `suppressions`), plus an Instantly workspace email
   import. Re-running is a no-op (`test_migration_replay.py`).
3. **Acceptance in isolation**: point `AIRTABLE_TABLE_NAME` at a new acceptance table in
   the same base and run `python -m tgtc_core cycle --i-understand-spend` with a bounded
   Fantastic window and a bounded Apollo grant. Read `ledger`. This is the first live
   evidence; nothing before it is.
4. **Mutual exclusion at cutover**: pause the legacy GTM cron (`MAINTENANCE_ONLY=1` already
   holds it) and the legacy Approved Sync cron. Structurally, core rows carry
   `Validation Version = tgtc-core/1`, so a still-running Approved Sync skips them without
   a write; the core's importer treats legacy rows as history. Both directions are guarded.
5. **Schedule**: run `cycle` hourly (blueprint §5 proposal) or as three stage workers
   sharing the database; fresh/backfill share is `TGTC_FRESH_SHARE_PCT`.
6. **Rollback**: stop the `GTM Core` service; re-enable the legacy crons. Deliveries made by
   the core remain in Airtable/Instantly and in `delivery_receipts`; they are history the
   legacy suppression already recognises (same `Lead Key`, same company × function keys).
   Nothing in rollback deletes a delivered lead.
7. **Backup**: PostgreSQL logical backup before each policy-version change; `payload
   retention` prunes `page_receipts.rows_compressed` after `TGTC_PAYLOAD_RETENTION_DAYS`
   (prune command to be added before production; receipts and ids are never pruned).

Infrastructure change and cost: one PostgreSQL instance and one Railway service. No new
provider, no plan change, no copy change.
