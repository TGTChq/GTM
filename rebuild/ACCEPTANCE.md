# TGTC core rebuild — acceptance criteria

Three evidence classes are kept apart in every statement below:

* **synthetic fixture** — deterministic fakes of provider behaviour (stateful,
  scripted, labelled `SIMULATED` in the test module docstring);
* **replay** — recorded provider responses replayed byte-for-byte;
* **live** — a real provider answered. Nothing in this branch is live evidence.

A test passing against a fake proves the *orchestration and storage contract*. It
does not prove provider behaviour, conversion, or capacity.

## 1. Mandatory QA from the blueprint (§11) → tests

| Blueprint row | Required result | Test (tests_core/) | Evidence class | Status |
|---|---|---|---|---|
| Generic, new or removed titles | equal eligibility for equal responsibilities; attractive title with incompatible content fails | `test_title_independence.py` | synthetic corpus | implemented |
| Semantic coverage | stratified corpus across nine campaigns with hard negatives; precision/recall per campaign | `test_classifier_corpus.py` (deterministic path; semantic path via replay adapter) | synthetic corpus, small | implemented for the deterministic layer; independent labelled corpus **pending** (§3) |
| Contact recovery | first candidate wrong company / no email / rejected → next valid candidate reaches Approved through the real gates | `test_contact_recovery.py` | synthetic Apollo | implemented |
| Multiple postings/people | same job from two sources → no duplicate; distinct jobs survive; one person not enrolled twice across functions | `test_identity_dedupe.py` | synthetic | implemented |
| Pagination and persistence | overlap, repeated page, limit, late response, crash before/after checkpoint, date change; no queue loss, no skipped incomplete partition | `test_acquisition_pages.py` | synthetic Fantastic | implemented |
| Classification and identity | public suffix, documented alias, parent/subsidiary, ATS host, employer mismatch, incomplete description, malicious instructions | `test_identity_edge_cases.py`, `test_prompt_injection.py` | synthetic | implemented |
| Credits and availability | zero balance, 422, 429, 401, timeout, restart, recovery are distinct; no storms; reservation never lifts a refusal | `test_apollo_availability.py` | synthetic Apollo | implemented |
| One source fails | received rows kept; own work and other sources continue | `test_acquisition_pages.py::test_source_failure_is_local` | synthetic | implemented |
| Huge backlog | fresh work keeps moving while a measurable share serves backfill; expired items do not return as new | `test_scheduler_fairness.py` | synthetic | implemented |
| Concurrency and expired lease | two workers compete; a stale worker cannot confirm the new one's result; real PostgreSQL unique constraints | `test_work_queue_concurrency.py` | real PostgreSQL (embedded) | implemented |
| Delivery with timeout | Airtable/Instantly accept, response lost; after restart reconcile without a second enrollment | `test_delivery_outbox.py` | synthetic Airtable/Instantly | implemented |
| Automatic quality | generic mailbox, wrong domain, insufficient evidence, unverified email are **not** approved and no review state exists | `test_approval_gate.py` | synthetic | implemented |
| Campaigns and outputs | nine routes covered, variables complete, copy preserved, destinations checked | `test_campaign_registry.py`, `test_nine_routes_end_to_end.py` | synthetic | implemented |
| Suppressions and outcomes | unsubscribe/reply during retry blocks the later send; duplicate events apply once | `test_suppression_races.py` | synthetic | implemented |
| Migration/rollback | importing twice keeps exclusions/receipts and duplicates no work; rollback keeps deliveries | `test_migration_replay.py` | synthetic | implemented |
| Load and resources | 50,000 posting events incl. repeats/modifications/closures, 10,000 burst, several workers | `tgtc_core/testing/load.py` (script; not part of the default suite) | synthetic | implemented; run locally, numbers in `HANDOFF.md §3`; the first run surfaced the employer-creation race now covered by `test_identity_race.py` |

Assertions name **identities and remaining work** (which posting, which
opportunity, which outbox item is left), not only totals or exit codes.

## 2. Structural criteria

| Criterion | How it is enforced |
|---|---|
| The new runner never invokes the old orchestrator | `test_no_legacy_imports.py` walks `tgtc_core/` imports; allowed legacy imports are exactly `domain_utils`, `source_domains` |
| Approved-only commercial output | `test_approval_gate.py::test_no_review_state_exists` asserts the Airtable payload builder can only emit `Status=Approved` and that no internal state name leaks into a written field |
| Approval and outbox are one transaction | `test_delivery_outbox.py::test_outbox_written_with_approval_atomically` (injected failure after approval insert rolls both back) |
| Intent before charge | `test_apollo_availability.py::test_request_attempt_written_before_call` |
| Legacy consumer cannot re-deliver new rows | `test_legacy_consumer_exclusion.py` runs `airtable_client.approved_row_eligibility` on a new-format row → `legacy` |
| Secrets never logged | providers log only status/error class; `test_no_secret_in_logs.py` |

## 3. Classifier acceptance (separate from orchestration)

Initial project thresholds, **not achieved precision**: precision ≥ 95 %, recall ≥
90 % per campaign on an independent stratified corpus with labels produced before
reading the classifier, plus 100 % of defined critical cases. Denominators and
intervals are to be published with the result. The legacy title-filtered corpus is
insufficient for measuring recall on excluded titles.

In this branch: `tests_core/corpus/` holds a small **synthetic** stratified corpus
(≥ 3 positives and ≥ 2 hard negatives per campaign) used to gate regressions of the
deterministic layer. It is not the acceptance corpus. Building the independent
corpus is a Codex/QA task (`HANDOFF.md §5`).

## 4. Capacity claims

None. The load script reports software throughput against fakes with zero
provider latency; it says nothing about inventory, buyer coverage or credits. The
budget identity `Approved = opportunities × buyer coverage × valid email × policy
pass` is computed by `tgtc_core/services/metrics.py` from linked cohorts only.

## 4b. Results recorded 2026-09-08 (offline; simulated providers)

| Check | Result |
|---|---|
| `python -m pytest tests_core -q` | 196 passed (embedded PostgreSQL 16.2) |
| `python -m tgtc_core demo` | 10 Approved across the nine campaigns; 10 Airtable rows, 10 Instantly leads; 30 simulated paid matches; 0 organization enrichments |
| `python -m tgtc_core.testing.load --events 50000 --burst 10000 --workers 4` | see `HANDOFF.md §3` (software envelope only) |
| Legacy `python ci_check_integrity.py` | `checked=35 mismatch=0 absent=0` |
| Legacy `python -m pytest tests -q -p ci_no_network` | 3752 passed, 1 skipped |
| Classifier on the synthetic corpus (`tests_core/test_classifier_corpus.py`) | precision 1.0 / recall 1.0 per function on 21 positives + 8 hard negatives — **synthetic, not the acceptance corpus** |

Defects found and fixed by these tests before handoff are listed in `HANDOFF.md §3`.

## 5. What "done" means for this task

* `python -m pytest tests_core -q` passes against the embedded PostgreSQL.
* `python -m tgtc_core demo --database-url <url>` runs the nine-route
  end-to-end path against the built-in fakes and prints the ledger.
* Contracts, integration map, acceptance and handoff exist and match the code.
* Remaining dependencies are listed exactly (`INTEGRATION_MAP.md §6`).
