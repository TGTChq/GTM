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
