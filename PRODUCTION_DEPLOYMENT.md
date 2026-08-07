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
| Source | `railway.json` `startCommand` (authoritative; overrides Docker CMD) |
| Restart policy | NEVER |
| Volume | `gtm-volume` at `/app/data/state` (state under `/app/data/state/orchestrator_v2`) |
| ATS boards | full `ats_board_registry.json` on the volume, health-aware deterministic scheduling. **No `--boards`, no `BOARDS_FINAL.json`.** |
| Instantly | never called by this service |

## Service B — GTM Approved Sync (Approved → Instantly)

| Setting | Value |
|---|---|
| Start Command | `python -u run_approved.py` (**service-level override — required**) |
| Cron | `*/5 * * * *` |
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

Founding year is intentionally **neutral** for qualification in the definitive
ICP: it is enriched/persisted/shown when available but never rejects a company.
`ENFORCE_FOUNDED_BEFORE` / `FOUNDED_BEFORE_YEAR` are therefore inert (leave unset).

Set on **GTM Approved Sync** (unchanged intent): `APPROVED_SYNC_REVALIDATE_PROVIDERS=true`.

Secrets (names only, must be PRESENT): `RAPIDAPI_KEY`, `APOLLO_API_KEY`,
`HUNTER_API_KEY`, `AIRTABLE_TOKEN`, `AIRTABLE_BASE_ID`, `AIRTABLE_TABLE_NAME`,
`INSTANTLY_API_KEY` (Approved Sync), `VALIDATION_SIGNING_KEY`.

## Reading a run without the volume

Both services print a business summary to Railway logs at completion:
GTM prints `JOBS_ANALYZED / QUALIFIED / CONTACTS_FOUND / SENT_TO_AIRTABLE`, the
full funnel, the ATS coverage block, and the top-5 rejection reasons. Approved
Sync prints `APPROVED_FOUND / REVALIDATED / SENT_TO_INSTANTLY / DUPLICATES_SKIPPED
/ FAILED`. No volume mount required.

## Cutover and rollback

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
