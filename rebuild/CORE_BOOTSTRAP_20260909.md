# Isolated core startup — 2026-09-09

This change prepares the next infrastructure check. It does not deploy the core,
apply the schema, call providers, or change any Railway configuration.

## What this adds

- `Dockerfile.core` installs `requirements-core.txt` and copies only the core and
  its two pure legacy helpers. `Dockerfile.core.dockerignore` excludes historical
  state, credentials, tests, and the simulated-provider package from the context.
- The default command is `python -m tgtc_core describe`. It reports configuration;
  it is not an acquisition command or a readiness guarantee.
- `python -m tgtc_core check-db` makes an authenticated database connection with
  a read-only transaction and SELECT queries. It reports server version and the
  migration ledger if present. A new empty database is a valid connection result.
  It does not install the schema or claim application/integration readiness.
- Connection errors return error class and optional SQLSTATE, not the DSN, host,
  user, password, provider credentials, or exception message. Connect timeout is
  10 seconds per connection attempt; statement timeout is 5 seconds.
- CI builds the actual image, exercises `describe` with Docker networking disabled,
  and checks that SQL schema/migration files are present in the image. The core
  suite includes real-PostgreSQL tests for the database check.

## Exact intended next deployment (not performed by this change)

Project: `tgtc-daily-pipeline` / `898f2e3a-1c1e-4b00-b9a6-686cf0432282`.
Environment: `production` / `bae427bd-64a6-4f4e-8f56-fbd406985434`.
Only service: `GTM Core Acceptance` / `74ffcf60-dfbf-47cd-a623-b9aee69ad16d`.

| Setting | Intended value |
|---|---|
| Source | Reviewed `feat/rebuild-core` head containing this change, after its CI passes |
| Dockerfile | `Dockerfile.core` |
| Start command | `python -m tgtc_core check-db` |
| Restart policy | `NEVER` |
| Cron / public networking | None |
| Only configured application variable | `TGTC_DATABASE_URL=${{Postgres Core.DATABASE_URL}}` |
| Provider and outbound keys | Not attached to this infrastructure-only check |

The reference above is literal Railway syntax; no credential belongs in Git.
Database `Postgres Core` (`f3e843e3-16f9-495e-a63b-574a653f3016`) is already
running privately. This check must run inside that private network, not by opening
a public database port for convenience.

Expected first result: `status=database_reachable`, `read_only=true`, PostgreSQL
version reported, migration ledger likely absent. Absence is reported, not repaired.
Success means a one-shot process exits zero, not that a permanent web worker stays up.

## Gates and ownership

Codex owns image/connection validation, schema initialization on the new database
after authorization, migration checks, and the later bounded acceptance executor.
The user must supply the production inference credential through secret management
and resolve Apollo account availability; neither is implied by a developer chat plan.
No new provider spending is authorized by a database connectivity check.

The earlier Railway operation confirmed the unrelated staged patch despite its
explicit scope restriction. Its variable-value audit is still unresolved because
OAuth hides the values. A subsequent action must target only the acceptance service;
never use a bulk environment commit or an autonomous deploy operation that could
apply unrelated changes. Deployment IDs and visible settings alone do not prove
unchanged secret values. No rollback is included here.

## Local verification limits

The portable test subset runs without network or credentials. This workspace has
no Docker daemon and cannot start embedded PostgreSQL with its root-only user
mapping. The new image build and real-PostgreSQL tests are therefore CI gates, not
claimed local successes. The previously green CI at `6202a63` does not verify this
new code. Results for the new exact head must be read before deployment.

## Follow-up verification

The image declares a complete `CMD`, with no `ENTRYPOINT`. Passing `describe` as
the Docker run command would replace that entire CMD and attempt to execute a
nonexistent standalone executable. The CI smoke command now runs the image with
no command override, exercising its actual default startup.

The four portable `test_database_check.py` cases passed with `ci_no_network`;
the two real-PostgreSQL cases and the Docker build remain pending on the new head.
The remote feature branch was directly checked at `6202a63`.

Railway's follow-up read reports a different staged patch,
`2f6879e3-ccdb-40f1-87c8-719d79cd8dab`, containing 308 pending changes. Service
reads associate 240 variable entries with GTM, 57 with Approved Sync, and 5 with
Postgres Core; they do not explain all 308 entries or reveal their values. The
acceptance service has no staged changes, source, variables or deployment.
The old deployment IDs remain unchanged. No attribution of this new patch's
author or actual value differences is possible from these responses. Do not
commit, discard, or conflate it with the earlier confirmed patch.

The acquisition pause is still not independently established: the old GTM cron
and its acquisition start command remain configured, and OAuth withholds the
control-variable values. This follow-up performs no Railway writes or provider
requests. The original incident's variable-value comparison remains open.
