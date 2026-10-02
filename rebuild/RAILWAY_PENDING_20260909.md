# Railway pending configuration and acceptance follow-up — 2026-09-09

## Latest evidence (supersedes the earlier status below)

The user supplied the complete redacted JSON from 07:28:51 UTC: same patch ID,
STAGED, zero enumerated entries, empty scopes and comparisons. The original
inspector did not record container shape or request the non-decrypted patch;
the result cannot distinguish a literal `{}` from nested empty objects. The
connector still returns 308. Do not count those as 308 proven modifications.

The official CLI explicitly recognizes an empty placeholder returned by
`environmentStagedChanges`. Its database module also documents that current
configuration read without `decryptVariables: true` masks variable values as
null. These are plausible explanations to test, **not a verified cause for this
environment**. The revised inspector requests both patch views in one fixed
query, reports their counts and structural shapes, and never grants apply
permission from an empty result. Historical incident auditing remains open.

- [Empty staged placeholders](https://github.com/railwayapp/cli/blob/4536fd53b0f77cd9693321fd992dd8b582bab55e/src/controllers/template_apply.rs)
- [Non-decrypted configuration values](https://github.com/railwayapp/cli/blob/4536fd53b0f77cd9693321fd992dd8b582bab55e/src/commands/database/mod.rs)

Remote `feat/rebuild-core` is `07c715bc61ff75c3e0228d34ecfe7b38490bdca3`.
[CI 34324131422](https://github.com/TGTChq/GTM/actions/runs/34324131422) passed
core, legacy, Docker and the mandatory legacy consumer check. Windows reported
346 passed with zero skips. New changes in this follow-up still require their
own published CI.

For the **first infrastructure check only**, the CLI now implements
`TGTC_ACCEPTANCE_MODE=read_only`: only `describe` and `check-db` can dispatch.
Every other command is rejected before its handler, even with the spend flag.
Unknown mode values fail closed without printing their contents. This enforces
zero provider calls through that CLI mode; it does **not** solve the durable
budget required for subsequent paid trials or control the running legacy
services. Thirty-two focused offline regression checks passed locally.

Railway's current documentation says new services cannot opt into the deprecated
`railway.json`/`railway.toml` Config as Code mechanism. Therefore no new custom
config file is proposed. Set build/start configuration on the acceptance service
itself and verify the effective deployment metadata. Do not migrate or delete the
legacy configuration as part of this step. [Current Railway documentation](https://docs.railway.com/config-as-code).

The remainder records the preceding review. Read its temporal claims as history.

## Verified baseline

Remote `feat/rebuild-core` is still
`7ae353a7b4f11da6c0e5597c7c8949ff3a81d631`.
[CI 34321457988](https://github.com/TGTChq/GTM/actions/runs/34321457988)
finished successfully: core **330 passed, 1 skipped**, actual Docker build/smoke
passed; legacy **3,753 passed, 1,001 subtests passed**; integrity 35/35.
These results apply to that baseline, not automatically to this follow-up.

The core-only skip comes from the absent legacy `python-dotenv` dependency.
Previously the test caught every import exception, hiding potential runtime
defects too. This follow-up permits only the explicit missing legacy dependency
in the minimal environment; a required CI step installs legacy dependencies and
executes the compatibility test without that skip.

The command-line spend acknowledgement was checked after opening PostgreSQL;
`cycle` also applied the schema first. Three offline regressions reproduced this.
The follow-up refuses `cycle`, `work` and `deliver` before connection/migration
when the existing acknowledgement is absent. Acknowledged commands retain their
existing behaviour. **This is not an aggregate spending limit.**

## Railway evidence and limits

Project: `898f2e3a-1c1e-4b00-b9a6-686cf0432282` (`tgtc-daily-pipeline`).
Environment: `bae427bd-64a6-4f4e-8f56-fbd406985434` (`production`).

| Service | Latest deployment | Configured cron | Staged variable entries |
|---|---|---|---:|
| GTM | `f3849b3a-d37f-434b-aa9e-711dcf83806d`, SUCCESS | `0 3 * * *` | 240 |
| GTM Approved Sync | `74639bc7-6ae4-4e48-8986-008a8da4a4b2`, SUCCESS | `0 0 * * *` | 57 |
| Postgres Core | `ef13410b-66c9-41fc-9adc-ce677082dfdb`, SUCCESS | none | 5 |
| GTM Core Acceptance | no deployment/source/variables | none | 0 |

Both legacy deployments are still on
`5d87851c1907ee4ee07a0314c666272c5b573376`. The production volume is 20,000 MB at
`/app/data/state`; PostgreSQL has its own 50,000 MB capacity volume. Capacity is
not used storage.

The current connector report is **308**, patch
`2f6879e3-ccdb-40f1-87c8-719d79cd8dab`, `STAGED`. Service-level entries sum to
302. Their proposed configuration objects are empty and their variable values
are hidden. These facts do not identify the other six entries, prove actual
value changes, or explain why the user's dashboard has no banner. They also do
not prove the patch harmless or justify applying/discarding it.

The original patch `cb6cea14-bea8-4cc0-a581-3bef57d1bdc7` was committed during
the earlier PostgreSQL creation outside the authorized scope. Its historical
value audit remains unresolved. An unchanged deployment or an equal current
value does not prove equality with the pre-incident value.

The live log at **2026-09-09T03:11:12Z** records Apollo HTTP 422,
`BILLING.LIMIT.CREDITS_EXHAUSTED`, lead-credit balance zero. That is a historical
observation, not a fresh billing query. The legacy GTM command and cron still
enable acquisition. **A current general pause is not established.** This
follow-up made no Railway or provider mutations.

## A read-only route that does not require the missing banner

The official CLI source at commit
`4536fd53b0f77cd9693321fd992dd8b582bab55e` exposes `railway api`, reads
`environmentStagedChanges`, and defines `EnvironmentPatch.patch(decryptVariables:)`
and `Environment.config(decryptVariables:)` in its public GraphQL schema.

- [CLI API command source](https://github.com/railwayapp/cli/blob/4536fd53b0f77cd9693321fd992dd8b582bab55e/src/commands/api.rs)
- [Staged query](https://github.com/railwayapp/cli/blob/4536fd53b0f77cd9693321fd992dd8b582bab55e/src/gql/queries/strings/EnvironmentStagedChanges.graphql)
- [Schema](https://github.com/railwayapp/cli/blob/4536fd53b0f77cd9693321fd992dd8b582bab55e/src/gql/schema.json)
- [Full-environment commit warning in database operations](https://github.com/railwayapp/cli/blob/4536fd53b0f77cd9693321fd992dd8b582bab55e/src/controllers/template_apply.rs)

`inspect_railway_pending.py` uses the installed CLI's existing login and sends
one fixed query. It does not read token files, link a folder, upgrade the CLI,
stage/apply/discard a patch, or connect to a provider. It requests current and
proposed values for comparison **only in local process memory**; the saved JSON
contains paths, names and comparison categories, never those values. Masked,
sealed or omitted values remain unknown. It does not reconstruct a historical
snapshot or guarantee resolved variable-reference equality.

From the extracted diagnostic folder on the user's Windows machine:

```powershell
python .\diagnosticar_railway.py
```

Send back the generated `railway-pending-inspection-*.json`. No credential should
be pasted. If the CLI lacks `api`, the script stops with a specific code and
does not install, upgrade, or attempt another authentication route. Its logic
is verified against synthetic responses; the user's actual CLI/session has not
been tested here. The connected Railway tools cannot execute this raw query.

## Concrete next infrastructure check (prepared; not executed)

Only `GTM Core Acceptance` (`74ffcf60-dfbf-47cd-a623-b9aee69ad16d`):

| Setting | Proposed value |
|---|---|
| Source | Exact reviewed core commit with successful CI |
| Image recipe | `Dockerfile.core` |
| Start command | `python -m tgtc_core check-db` |
| Restart | NEVER |
| Variable | Literal `TGTC_DATABASE_URL=${{Postgres Core.DATABASE_URL}}` |
| Cron / public domain / provider keys | none |

The first success must be the authenticated private SELECT returning
`database_reachable`, `read_only=true`, a server version, and schema-ledger
presence. A missing ledger is valid connection evidence, not an installed core.
Schema initialization on the new database is a separate subsequent write.

Do not run the check using an operation that commits unrelated staged work.
Before a mutation, establish its service scope and authorization. The existing
creation permission does not authorize deploying the core or confirming the
environment-wide patch. No rollback is proposed without a verified diff.

## Remaining acceptance and business work

Still open: private core-to-PostgreSQL connectivity and schema; enforced durable
aggregate spend reservations (physical retries/uncertain outcomes included);
classification API credentials and independent labels; Apollo account
availability; isolated Airtable field/receipt acceptance; a controlled no-send
Instantly destination; suppression/history adoption and replay; cutover pause
verification; challenger routing; artifacts-versus-ledger coverage.

Historical duplicate events **5,218 / ATS 2,722 / 4,505** remain causally
unattributed. Trace source/segment/page ownership and insertion of `seen_ids`
using response-level IDs where available; do not infer their cause from this
rebuild. Preserve first appearances and real repeats in any offline reproduction.
**361 / 520 / 5,511** are window observations; **781 / 6,205** is not an established
Approved conversion. There is no demonstrated 111/day ceiling or 1,000 new
Approved/day capacity.

## Validation of this follow-up

20 focused checks passed with provider sockets blocked and credentials excluded:
six CLI acknowledgement paths, four portable database-check cases, the strict
legacy-consumer contract, and nine redaction/target/query/serialization checks
for the Railway inspector. Two existing PostgreSQL integration cases were
explicitly deselected locally; they were green in the baseline CI. The follow-up
full CI must be checked on its own commit after publication.
