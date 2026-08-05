# Replacement Orchestrator — self-contained package

Replaces the ATS acquisition/orchestration boundary that the Final Controlled
Live ATS Validation proved defective. Reuses the validated Phase 1B-2C
components unchanged; adds a production-reachable, mode-gated, checkpointed,
budgeted, reconcilable execution path.

## Prerequisite

This package applies **on top of** the Phase 1B-2C package. Reproduction is a
two-step apply on a clean tree at base commit
`9d456e5395fee1e6bcea078dad0d3325b1b2c2a6`.

## Apply

```bash
git worktree add -b feat/replacement-orchestrator ../tgtc_replacement 9d456e5395fee1e6bcea078dad0d3325b1b2c2a6
cd ../tgtc_replacement
git apply --whitespace=nowarn /path/to/phase1b2c.patch        # prerequisite
git apply --whitespace=nowarn /path/to/orchestrator.patch     # this package
```

## Files (18)

| Count | Note |
|---|---|
| 12 | `orchestrator/*.py` — the replacement package (incl. `adapters_real.py`) |
| 1  | `run_orchestrator.py` — CLI entry point (5 modes; production never default) |
| 5  | `tests/test_orchestrator_*.py` — 38 offline tests |

All 18 are **new** files. No Phase 1B-2C or base file is modified by this patch;
the validated components (including `ats_board_registry.fetch_board_jobs` and its
existing `_detail_request` listing/detail trace roles) are consumed, not edited.

## Verify integrity (EOL-agnostic)

```bash
while read -r sha bytes f; do
  actual=$(tr -d '\r' < "$f" | sha256sum | cut -d' ' -f1)
  [ "$actual" = "$sha" ] || echo "MISMATCH $f"
done < orchestrator.MANIFEST.sha256 ; echo done
```

## Test

```bash
export PYTEST_CURRENT_TEST=1
export PYTHONPATH="<Python 3.14 site-packages>"   # 3.12 borrows requests/dotenv
py -3.12 -m unittest discover -s tests
py -3.14 -m unittest discover -s tests
```

Expected on both interpreters: `Ran 1130 tests`, `FAILED (failures=1, skipped=1)`
— the single failure is the pre-existing aged-fixture
`AshbySourceTruthTests.test_hybrid_workplace_overrides_is_remote_true`; the skip
is the Windows symlink skip. `+38` tests vs the 1092 Phase 1B-2C baseline, all in
`tests/test_orchestrator_*.py`. `FAILED: lane failure: …` lines are expected log
output from failure-injection tests, not test failures.

## Safety

Offline only. Every offline mode makes zero network calls and writes nothing
outside its run root (both enforced structurally and asserted by tests). No real
Apollo/Hunter/Airtable/Instantly/Railway contact anywhere; delivery runs against
fake adapters. Production mode is refused unless explicitly acknowledged AND real
adapters are injected (not wired in this package).
