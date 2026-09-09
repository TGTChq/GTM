# Core release: use the service configuration

On `feat/rebuild-core`, the old root `railway.json` is preserved byte-for-byte as
`railway.legacy.json`. Railway's automatic root config discovery previously
overrode `Dockerfile.core` with `/Dockerfile`, building the legacy image. Binding
`railway.core.json` through `railwayConfigFile` was rejected with INVALID_ARGUMENT
on 2026-09-09 (both absolute and relative paths).

Use the core service's native settings; do not opt this service into deprecated
Config as Code. `railway.core.json` remains the core build specification read by
CI, but is not bound to the Railway service.

- Project: `898f2e3a-1c1e-4b00-b9a6-686cf0432282`.
- Environment: `bae427bd-64a6-4f4e-8f56-fbd406985434` (production).
- Core service: `74ffcf60-dfbf-47cd-a623-b9aee69ad16d`.
- Source: TGTChq/GTM, `feat/rebuild-core`, root `/`.
- Builder DOCKERFILE, Dockerfile path `Dockerfile.core`, config file path empty.
- No cron, no pre-deploy commands, restart NEVER, one replica.
- `TGTC_ACCEPTANCE_MODE=read_only`; private PostgreSQL reference unchanged.
- Start: `sh -c 'test "$TGTC_ACCEPTANCE_MODE" = read_only && python -m tgtc_core describe && python -m tgtc_core check-db'`.

Publish the commit, require its own complete CI, then deploy that exact SHA.
Verify actual build and runtime logs: Dockerfile.core, settings presence report,
database_reachable, read_only true and migration ledger [1, 2, 3]. A SUCCESS
deployment status alone is insufficient. No schema reset or provider call is
needed to check the release.

The legacy services follow `main`; their code and production deployments are
unchanged by this feature-branch rename. Their current paused start commands
and lack of cron must remain intact. Before a future merge to main, retain native
legacy service build settings; do not bind either archived JSON automatically.

Configuration-file precedence and the deprecation policy are documented at
[Railway Config as Code](https://docs.railway.com/config-as-code). Activation with real providers remains
separate: missing inference credential, persistent spending bounds, isolated
acceptance and historical suppression adoption are still outstanding.
