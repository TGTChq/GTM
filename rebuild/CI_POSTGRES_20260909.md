# Linux CI follow-up

The user's Windows run at `58c26845d762400b797021980611ebaa674257e2`
completed with **311 passed, 1 skipped**. After publication, GitHub Actions run
[34317429517](https://github.com/TGTChq/GTM/actions/runs/34317429517) produced
**181 passed, 1 skipped, 130 setup errors** in the core job.

All 130 errors came from the same fixture guard before the test body:
`refusing to reset a schema on non-local host '/tmp/tgtc_pgtest_4acs7up7'`.
The installed pgserver 0.1.4 starts PostgreSQL over a Unix socket on Linux and
loopback TCP on Windows. `reset_schema` recognized only TCP host strings.

The test-only reset helper now recognizes absolute Unix socket paths with no
network host address. It also checks the effective `hostaddr` for TCP: a remote
address cannot be hidden behind `host=localhost` or an empty host. Remote host
names and addresses are still refused before any SQL.

The locality regressions use a recording connection and open no database:

- Before correction: **4 failed, 9 passed**. Two cases reproduce the Linux path
  rejection; two expose the previously unchecked remote hostaddr override.
- After correction: **13 passed**. Combined with the existing independent core
  review tests: **50 passed**, with provider network blocked.

Only test reset locality and its regression coverage change. No acquisition,
qualification, approval, delivery, production schema or deployment setting changes.
The follow-up Linux integrated CI run is the remaining validation for this fix.
