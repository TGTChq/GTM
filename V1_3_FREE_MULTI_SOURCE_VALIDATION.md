# TGTC READY v1.3 — Free Multi-Source Acquisition

## Baseline

- Repository baseline: `214cf167e8f457ba34c6306f05663b3ec007c0a2`
- Production acquisition default: `free_multi_source`
- JSearch status: disabled by default; retained only as an explicit rollback mode
- Manual company/ATS registration: not required

## Implemented acquisition sources

### Free global discovery feeds

- Himalayas
- Jobicy
- We Work Remotely
- Remotive
- Remote OK

Each adapter maps provider payloads into the existing TGTC raw-job contract. Feed evidence remains subject to the unchanged integrity, source, account, role, contact, and email gates.

### Automatically discovered public ATS boards

- Greenhouse
- Lever, including EU Lever boards
- Ashby
- Recruitee
- Workable
- Personio

The pipeline detects ATS URLs from provider records, application links, landing pages, and existing raw/filter/enrichment artifacts. It persists the detected boards in `data/state/ats_board_registry.json` and refreshes due boards automatically. The registry is a cache maintained by the pipeline, not a manual input list.

## Processing changes

- Added cross-source deduplication by canonical employer identity and normalized job title.
- Official ATS records win over syndicated records while preserving all discovery sources and application URLs.
- Added per-source metrics for raw records, normalized jobs, selected jobs, role acceptance/review, and prefilter viability.
- Added bounded landing-page discovery to recover direct ATS and company-site links.
- Added fail-closed JSON/XML parsing, response-size limits, bounded redirects, DNS resolution checks, and private-network destination blocking.
- JSearch FINAL_PASS/reviewable top-up is automatically disabled outside `ACQUISITION_MODE=jsearch`.
- Corrected the observed `topup_filter_error`: a non-empty top-up batch with zero filter survivors now counts as zero downstream yield instead of a technical filter failure.

## Shadow validation command

```bash
python -u run_free_source_shadow.py
```

The shadow command:

- calls only the configured public free feeds and public ATS endpoints;
- uses a throwaway seen-jobs registry;
- writes raw/filter/report artifacts below `data/state/shadow/`;
- may update the automatically discovered ATS board registry;
- does not call JSearch, Apollo, Hunter, Airtable, or Instantly.

The report includes `source_metrics`, `source_outcomes`, `ats_metrics`, the filter funnel, and explicit zero counts for all paid/downstream services.

## Required Railway variables

```text
ACQUISITION_MODE=free_multi_source
FREE_JOB_SOURCES_JSON=["himalayas","jobicy","weworkremotely","remotive","remoteok"]
FREE_SOURCE_REQUEST_TIMEOUT_SECONDS=20
FREE_SOURCE_MAX_RESPONSE_CHARS=8000000
FREE_SOURCE_MAX_RECORDS_PER_SOURCE=1000
FREE_SOURCE_MIN_SUCCESSFUL_SOURCES=2
HIMALAYAS_PAGE_SIZE=20
HIMALAYAS_MAX_PAGES=25
FREE_SOURCE_LANDING_DISCOVERY_ENABLED=1
FREE_SOURCE_LANDING_DISCOVERY_MAX_REQUESTS=40
ATS_DIRECT_ACQUISITION_ENABLED=1
ATS_BOARD_REFRESH_INTERVAL_HOURS=20
ATS_MAX_BOARDS_PER_RUN=150
ATS_MAX_JOBS_PER_BOARD=250
ATS_REGISTRY_AUTO_SEED_HISTORY=1
ATS_REGISTRY_HISTORY_FILE_LIMIT=80
ATS_REGISTRY_MAX_HISTORY_FILE_BYTES=25000000
FINAL_PASS_TOPUP_ENABLED=0
VALIDATION_VERSION=tgtc-ready-v1.3-free-multi-source
```

`RAPIDAPI_KEY` is not required in free multi-source mode. Existing JSearch variables may remain present; they are ignored unless `ACQUISITION_MODE=jsearch` is selected.

## Validation performed

```text
Python compilation: passed
Focused v1.3 tests: 16 passed
Complete repository suite: 371 passed, 0 failed
```

The complete suite was executed with a temporary test-only validation signing key and `PRODUCTION=0`. The warning/error logs emitted by mocked failure-path tests are expected assertions, not live API calls.

No live calls were made to JSearch, Apollo, Hunter, Airtable, or Instantly. No live free-source benchmark was performed during patch construction; the Railway shadow run is the required first market-volume measurement.

## Production rollout gate

Do not run the full production pipeline immediately after deployment.

1. Deploy v1.3 with the variables above.
2. Run `python -u run_free_source_shadow.py` once from Railway SSH.
3. Review acquired jobs, source outcomes, ATS jobs, cross-source duplicates, and the filter funnel.
4. Confirm that employer identities and direct ATS records are clean.
5. Only then run one production `run_daily.py` execution.

A successful test suite proves integration correctness and regression safety. It does not prove that the free market inventory will produce 30 net Airtable rows per day; the shadow report is the evidence required for that decision.

## Rollback

Application-level rollback without reverting code:

```text
ACQUISITION_MODE=jsearch
FINAL_PASS_TOPUP_ENABLED=1
```

This rollback also requires a valid `RAPIDAPI_KEY`. Keep `VALIDATION_VERSION=tgtc-ready-v1.3-free-multi-source` so stale v1.2 checkpoints are not resumed.

Full rollback: revert the v1.3 commit and redeploy the prior `main` SHA.
