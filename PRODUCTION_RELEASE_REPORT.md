# Production release — exhaustive nine campaigns

Branch `release/exhaustive-nine-v1`, built on the deployed commit **`be3af32`**,
which is a direct ancestor of the audit branch. 52 commits sat above it.

## Commit selection and dependency closure

48 of the 52 carry production logic, tests or migrations inside the required
closure. They are included whole and in order. Four carry nothing else and are
excluded: `003004d` (offline audit harness), `54c006c`, `ce5c44d`, `08f6534`
(implementer reports). One further commit, `5be8c0e`, is included for its
`services/metrics.py` change; its edit to the audit harness disappears with the
harness.

Closure, and what satisfies it:

| requirement | commits |
|---|---|
| exhaustive nine-campaign qualification | `9fb0e4c` |
| exhaustive acquisition universe | `ebce796` |
| corrected Fantastic v1/24h acquisition and pagination | `ca972e1` (`services/daily_24h_canary.py`), `6ebe0a2`, `247895b` |
| stable job deduplication | inherited from `be3af32`; `c474393` corrects the evidence rows |
| current Challenger routing | `9fb0e4c` (`KNOWN_CHALLENGER_CAMPAIGN_IDS`), `check_challenger_routing.py` |
| contact title normalisation and ranking | `95cf2a1`, `f338dbc`, `5a3e023`, `4318498`, `424a6af`, `a7d4485` |
| verified email and current employer | inherited; `fe09d2d`, `8070e45` |
| historical suppression | inherited from `be3af32` |
| outbox idempotency | inherited; `8070e45` (never pay twice for a stored answer) |
| migrations | `da41b08` (size band), `c474393`, `5f33c88` (dedupe before indexing), `6453537` (011, jurisdiction) |
| qualification recovery (fail-open) | `56c4a1d`, `e4e7fec`, `bfed634`, `8334dc5`, `296c42a`, `8206a1c`, `302ed40`, `f75d8f6`, `20f7498`, `680bb7c` |
| company size three-state | `600ac63`, `af0d7b1`, `db79e4f`, `0804a93`, `c528e98`, `9f2e900` |
| campaign scope correctness | `95db150`, `f8e92b4`, `7260466`, `01e1154`, `dbc309c` |
| market gate / compliance | `3898e7b`, `6453537`, `ebfbe54`, `5be8c0e` |
| excluded-industry correction | `94d487f`, `1020520` |
| observability and reason codes | `d47c16e`, `247895b` |

### Kept deliberately, against the "exclude abandoned implementations" rule

* `domain/candidate_qualification.py` — carries the void four-group structure
  and is inert (`TGTC_CANDIDATE_QUALIFICATION` is unset in production), but
  `services/daily_24h_canary.py` imports `approved_industry_query_labels` from
  it. Extracting one function from 1,052 lines is a larger and riskier change
  than shipping an inert module.
* `policy/compliance.py` — contains the UK corporate-subscriber prototype,
  which is not separable from the module that enforces the US market gate. The
  UK path is inert: an unknown entity type fails closed.

## Policy

`tgtc-core/3-exhaustive-nine`, gated by `TGTC_EXHAUSTIVE_NINE_CAMPAIGNS=1`.
Flag off returns every decision to `tgtc-core/2`. `CachedInference` falls back
v3 -> v2 -> v1, so the version bump costs no paid re-calls.

Three states: explicit evidence of an approved hard exclusion rejects; missing
or unknown information passes; passing the gates without an allowlist match
assigns the closest of the nine. There is no NEEDS_CHECK bucket.

A verified US send is gated on `opt_out_status != opted_out` plus unsubscribe
and suppression, which this core provides structurally
(`services/opportunity.py`), so the market gate does not block US delivery.

## Tests

| suite | result |
|---|---|
| `tests_core` (release branch) | **1,579 passed, 0 failed** |
| `tests_core` (audit branch, incl. removed harness test) | 1,592 passed, 0 failed |
| `tests` legacy | 3,751 passed, 1 skipped, 1,001 subtests, 3 failed |

The three legacy failures are `tests/test_offline_network_guard.py`. They were
reproduced against `HEAD~2`, before any commit in this release, and fail
identically: this shell permits outbound network access, which is exactly what
the guard refuses to allow. They are a baseline/environment failure, not a
regression, and the guard was not weakened or deleted.
