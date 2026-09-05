# TGTC Production Deployment (Definitive)

This is the authoritative production configuration. It supersedes
`DEPLOY_RAILWAY_STEP_BY_STEP.md`, `PRODUCTION_HANDOFF_CHECKLIST.md`, and
`READY_V1_DEPLOY.md`.

Project: **tgtc-daily-pipeline** · Environment: **production** · Volume: **gtm-volume**

There is exactly **one acquisition spine** (`run_orchestrator.py`) and **one
Instantly worker** (`run_approved.py`). `run_daily.py` is retired from production.

## Service A — GTM (acquisition → Airtable Pending)

| Setting | Value |
|---|---|
| Start Command | `python -u run_orchestrator.py --mode live_acquisition_and_enrichment --lanes ats,jsearch,free_feeds --target 300 --airtable-write --global-budget 1500 --ats-lane-budget 1200 --reserved-non-ats 200 --board-budget 120 --provider-budget 300 --artifact-root /app/data/state/orchestrator_v2` |
| Source | **Railway service-level Start Command (set/edit in the Railway UI).** `railway.json` deliberately does NOT define `startCommand`, so this is paste-managed and can be changed without a Git commit/merge. If the field is left empty the image falls back to the safe `--preflight-only` CMD (never acquisition). |
| Restart policy | NEVER |
| Volume | `gtm-volume` at `/app/data/state` (state under `/app/data/state/orchestrator_v2`) |
| ATS boards | full `ats_board_registry.json` on the volume, health-aware deterministic scheduling. **No `--boards`, no `BOARDS_FINAL.json`.** |
| Instantly | never called by this service |

## Service B — GTM Approved Sync (Approved → Instantly)

| Setting | Value |
|---|---|
| Start Command | `python -u run_approved.py` (**service-level override — required**) |
| Cron | `*/5 * * * *` — **but not enabled until the historical Approved backlog is explicitly authorized** (see cutover sequence) |
| Restart policy | NEVER |
| Selection | Airtable `Status = Approved` only (never Pending/Enrolled/Error) |
| Behaviour | revalidate the approved record → enroll in Instantly → mark `Enrolled` only on confirmed success/idempotent duplicate. Cannot run acquisition. |

## Docker image default

`Dockerfile` CMD is `python -u run_orchestrator.py --preflight-only` — a safe,
zero-network readiness check. It is a fallback only; both services set explicit
Start Commands above. A service that loses its Start Command performs a harmless
preflight instead of silently running acquisition or the retired `run_daily.py`.

## Required Railway environment (production-intended, non-secret)

Set on **GTM**:
- `ATS_SCHEDULER_MODE=deterministic_partition` (health-aware scheduling; the
  orchestrator forces this for the registry path regardless, but set it so the
  effective config is explicit)
- `ATS_SCHEDULER_STATE_PATH=/app/data/state/orchestrator_v2/scheduler_state/ats_carried_overdue.json`
  (persists overdue carry-forward so no board starves)
- `ATS_REGISTRY_AUTO_SEED_HISTORY=1` (registry grows from job history)
- `PIPELINE_ARTIFACT_ROOT=/app/data/state/orchestrator_v2` (all state on the volume)
- `NUM_PAGES=1` — **required** with full target-title coverage. JSearch now queries
  all 118 target titles (units = queries × pages). 118 × 1 = 118 units ≤ the 150
  `JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN` budget; at the old `NUM_PAGES=3` it would be
  354 units and JSearch would refuse. First-pass depth is 1 page; adaptive
  deepening re-adds pages to the roles that actually yield, within budget.

**JSearch coverage / depth tradeoff (recommended vs alternative):**
- *Recommended (budget-neutral, full breadth):* 118 queries × `NUM_PAGES=1` = 118
  units ≤ 150. Every target title queried every run; ~32 units of headroom feed
  adaptive deepening. No quota increase vs the prior 50 × 3 = 150.
- *Alternative (full breadth + more first-pass depth):* raise
  `JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN` to 236 for `NUM_PAGES=2` (118 × 2), or 360
  for `NUM_PAGES=3` (118 × 3). This is +86 / +204 units per run (~1.6× / ~2.4× the
  prior JSearch quota burn) — choose only if the RapidAPI plan has monthly headroom.

Founding year is intentionally **neutral** for qualification in the definitive
ICP: it is enriched/persisted/shown when available but never rejects a company.
`ENFORCE_FOUNDED_BEFORE` / `FOUNDED_BEFORE_YEAR` are therefore inert (leave unset).

`APPROVED_SYNC_REVALIDATE_PROVIDERS` is **deprecated and not read at runtime.**
Approved Sync is delivery-only: it makes no Apollo, Hunter, Fantastic or
JobSourceResolver call, and `run_approved.run()` explicitly ignores the equivalent
argument. The variable is currently `true` on GTM Approved Sync; that value selects
nothing. Do not set it on a new environment, and do not read it as evidence that
provider revalidation is on — it is not, and re-enabling it is what caused the
2026-08-12 incident (627 Approved rows marked Error by a 24h validation-age gate
that ran before any provider call, so nothing enrolled).

Secrets (names only, must be PRESENT): `RAPIDAPI_KEY`, `APOLLO_API_KEY`,
`HUNTER_API_KEY`, `AIRTABLE_TOKEN`, `AIRTABLE_BASE_ID`, `AIRTABLE_TABLE_NAME`,
`INSTANTLY_API_KEY` (Approved Sync), `VALIDATION_SIGNING_KEY`.

## Reading a run without the volume

Both services print a business summary to Railway logs at completion:
GTM prints `JOBS_ANALYZED / QUALIFIED / CONTACTS_FOUND / SENT_TO_AIRTABLE`, the
full funnel, the ATS coverage block, and the top-5 rejection reasons. Approved
Sync prints `APPROVED_FOUND / REVALIDATED / SENT_TO_INSTANTLY / DUPLICATES_SKIPPED
/ FAILED`. No volume mount required.

## Maintenance: read the volume without running acquisition

The `gtm-volume` (run artifacts, file logs) is only reachable from a **running**
GTM container, and GTM's normal container exits when the run completes. Because
GTM's Start Command is now **service-managed** (not pinned in `railway.json`), you
can borrow the volume with a harmless idle command and no Git change:

1. **Record** the normal Start Command (copy it from the row above / keep this doc open).
2. In the Railway UI, set **GTM → Start Command** to: `sh -c "sleep infinity"`.
3. **Redeploy GTM.** The container starts, mounts `gtm-volume` at `/app/data/state`,
   and idles — it runs **no** acquisition and makes **no** external calls.
4. Read read-only, e.g.:
   - `railway ssh -s GTM "ls -la /app/data/state/orchestrator_v2/run_artifacts"`
   - `railway ssh -s GTM "tar -czf - -C /app/data/state/orchestrator_v2/run_artifacts <run_id>" > run_artifacts.tgz`
   - or `railway volume files --volume gtm-volume download /orchestrator_v2/run_artifacts/<run_id> ./out`
     (works now because a GTM instance is running to serve the session).
5. **Restore:** set **GTM → Start Command** back to the normal acquisition command
   (row above) — or clear it to leave the safe `--preflight-only` fallback — and
   redeploy. No Git commit/merge is involved in any of these steps.

`sleep infinity` is a **runtime UI value only**; it is intentionally NOT committed
to `railway.json` or any config-as-code.

## Final production-readiness hardening (this PR)

**Airtable dedup is now FUNCTION-aware.** Review/delivery dedup keys on
company + `role_bucket`, so a company already in Airtable for one function (e.g.
Marketing) no longer suppresses a *different* function (e.g. Sales). Two explicit,
independent flags replace the old bucket-blind `AIRTABLE_SUPPRESS_EXISTING_COMPANY`:
- `AIRTABLE_SUPPRESS_EXISTING_COMPANY_FUNCTION` (default **True**) — same
  company+function dedup (intended production behaviour).
- `AIRTABLE_SUPPRESS_ACCOUNT_LEVEL` (default **False**) — optional company-wide
  CRM/active-pipeline exclusion; enable only for a deliberate one-account-at-a-time
  policy. It is checked first and is strictly stronger than the function dedup.
- `AIRTABLE_SUPPRESS_EXISTING_COMPANY` is **deprecated**. If it is still set in the
  Railway env it acts as a back-compat alias that turns the account-level tier on;
  to get function-aware behaviour, **unset it** (or set
  `AIRTABLE_SUPPRESS_ACCOUNT_LEVEL=0`).

**Domain-resolution observability.** The dominant HM-failure cause in the first run
was `no_search_domain` (78%), which is a source-quality problem (aggregator-only
postings with the employer stripped), not a resolver gap. Each run now emits
`domain_resolution_summary.json` and a `---- Domain Resolution ----` log block
classifying every company as `direct_employer` / `intermediary_unresolved` /
`known_employer_unresolved_domain` / `unresolved_no_evidence`. A deterministic,
denylist-safe recovery chain (direct `apply_options` host + curated
`COMPANY_DOMAIN_ALIASES`) is in place; populate `COMPANY_DOMAIN_ALIASES_JSON` with
known acronym employers (e.g. `{"johnson & johnson":"jnj.com","hp":"hp.com"}`) to
unlock their searches. It never turns a staffing/board host into an employer domain.

**Approved Sync is legacy-safe.** `select_eligible_approved()` enrolls ONLY a
Status=Approved row that also carries the current authorization: an actionable
`Final Decision`, an EXACT `Validation Version` match, a valid `Validation
Fingerprint`, an `Email`, and a resolvable campaign. Legacy/invalid rows are
skipped *before* any revalidation or Instantly call and with **no Airtable write**
(a legacy row is never `mark_error`-ed just for being unauthorized). This is
unconditional (no longer gated on `FINAL_PASS_PIPELINE_ENABLED`), so a stale flag
can never release the backlog. The 42 legacy rows fail on missing/old `Final
Decision`/`Validation Version`/fingerprint → all classified legacy → never enrolled.

### Approved Sync production activation (the only remaining manual step)
1. With auto-deploy still disabled, run the zero-write preflight to confirm the
   eligible count: `python -u run_approved.py --preflight-only` (reads Airtable,
   writes nothing, calls no Instantly). Expect `eligible = 0` for the legacy
   backlog. The hardened `select_eligible_approved()` logs
   `seen / eligible / skipped_legacy / skipped_invalid`.
2. Only if `eligible = 0` (or every eligible row is deliberately authorized), set
   **GTM Approved Sync** Start Command `python -u run_approved.py`, Restart NEVER,
   Cron `*/5 * * * *`, and (optionally) re-create its GitHub deploy trigger.
   Railway crons run the command on schedule, not on deploy, so re-enabling the
   trigger does not itself enroll anyone.
3. If any row is eligible that you did not intend to enroll, STOP and inspect it —
   do not enable the cron.

## Employer / domain resolution (source ≠ employer)

The dominant HM-failure cause is `no_search_domain` (353/452 in the first run; 77%
Himalayas, ~20% JSearch aggregator, only 5 ATS). Root cause is **source quality**:
the aggregator feeds carry no first-party employer domain. This pass adds a
deterministic, evidence-only layer (`domain_resolution.py`) that never guesses:

- **Preserve early.** Free feeds forced `is_direct=False` even when the
  `applicationLink` was the employer's own host; now `_canonical_job` marks it
  `is_direct=True` **only** when the host clears the intermediary denylist AND its
  registrable domain is name-consistent with the employer, so the standard
  employer-domain path can use it. ATS/board hosts (`boards.greenhouse.io`,
  `jobs.lever.co`, `*.myworkdayjobs.com`, …) are denylisted and never accepted.
- **Resolution chain (first-party first):** enrichment-resolved → direct
  `apply_options` host → **name-consistent first-party host** (from any apply/source
  URL) → curated `COMPANY_DOMAIN_ALIASES` → unresolved. Every step is denylist-safe
  and evidence-backed; a job-board host that does not match the employer name is
  rejected (no misattribution).
- **Explicit classification** (source ≠ employer): `direct_employer`,
  `ats_employer_known` (first-party name known, domain unresolved — searchable by
  name), `aggregator_employer_unresolved`, `intermediary_unknown_client`
  (staffing/hidden client — not a technical failure; Apollo is not wasted on the
  wrong org), `known_employer_unresolved_domain`, `unresolved_no_evidence`.
- **Observability:** each run emits `employer_resolution_summary.json` +
  `domain_resolution_summary.json` and a `---- Employer / Domain Resolution ----`
  log block (postings_evaluated, direct/aggregator/intermediary/staffing,
  employer_resolved/unresolved, hm_searches_unlocked_by_recovery, by_source,
  by_unresolved_reason). Units are company×role_bucket "postings evaluated".

**Curated aliases.** `reports/domain_alias_candidates_<run>.csv` lists recognizable
employers in the unresolved set. Domains are proposed ONLY when name-consistent and
evidence-backed (0 auto-safe in the first run); 5 employers (J&J, Philip Morris,
HII, VTG, HP) carry an observed first-party careers host but an acronym/brand
mismatch, so they are flagged **manual_review** — verify before adding to
`COMPANY_DOMAIN_ALIASES_JSON`. Never add a domain guessed from a company name.

Honest impact: **observed historical recovery ≈ 0** (the artifact is evidence-barren);
**expected future recovery** is the subset of future free-feed/JSearch postings whose
apply/source URL is a name-consistent employer host (bounded, source-dependent) plus
any curated aliases. ~331/353 Himalayas/aggregator failures are structurally
unresolvable from what those sources expose and need a different source.

## Cutover sequence (staged — do not release the historical backlog automatically)

1. Merge the final PR.
2. Configure **GTM** (Start Command + env above).
3. Configure **GTM Approved Sync** Start Command `python -u run_approved.py`,
   Restart NEVER — **but do NOT enable the `*/5` cron yet.** The 42 existing
   Approved rows are legacy manual approvals with no `Final Decision`/
   `Validation Version`, so `get_approved_leads()` already blocks all 42 (fail-
   closed); still, leave the cron unset until authorized.
4. Run and validate **GTM** production acquisition; inspect the new funnel logs.
5. Separately authorize the 42 existing Approved records (or decide how to handle
   them — they must be re-run through the pipeline to get a validated `Final
   Decision` before they can enroll).
6. Enable the normal `*/5 * * * *` Approved Sync cron.

## Rollback

See the final readiness report for the exact, ordered cutover steps.

**Rollback point.** If a rollback is required, revert **GTM/main** to commit
`456a384` and restore **only GTM's** prior Start Command.

**GTM Approved Sync must NOT be rolled back.** Its previous configuration was the
confirmed production defect (it ran the acquisition orchestrator instead of the
worker). Under **no** rollback scenario may it be restored to that command. It
must remain, in every scenario:

- Start Command: `python -u run_approved.py`
- Cron: `*/5 * * * *`
- Restart: NEVER

`run_approved.py` exists and behaves correctly at `456a384` as well (it was
merged in PR #34), so this configuration is valid both before and after a GTM
code rollback.
