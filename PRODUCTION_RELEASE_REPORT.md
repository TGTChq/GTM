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

## Production state before this release, and why it delivered nothing

Three independent failures, diagnosed separately:

1. **`cronSchedule` was `None` on every one of the six services.** Nothing was
   scheduled anywhere. This alone guaranteed zero output.
2. **`GTM Core Canary 1000` had no `INSTANTLY_*` configuration at all** (29
   variables, none of them Instantly), so `campaign_route_configured()` failed
   and no approval could be created. The 2026-09-18 run ended `0/1000` with
   `approvals_created: 0` for exactly this reason.
3. **The `be3af32` deployment shown as CRASHED was not a defect.**
   `run-target` returns exit 1 whenever the target is not met
   (`__main__.py:158`) and Railway marks any non-zero exit CRASHED. The run
   completed normally.

`GTM` and `GTM Approved Sync` additionally run a parked start command that
prints `TGTC_PAUSED_PENDING_CREDITS` and does nothing, which is deliberate.

## The Control/Challenger trap

`GTM` still holds all ten `INSTANTLY_CAMPAIGN_*` names, so "restore the missing
configuration from history" reads as a copy. Their values are the **Control**
ids. Restoring them verbatim would have pointed every new lead at the nine
retired campaigns.

Worse, production already holds 86 pending Instantly outbox rows whose stored
payload names a Control campaign, and two independent code paths would have
let them through: `approval.py` accepted any Control id outright, and delivery
only checked that the campaign was ACTIVE -- which the Control campaigns still
are, because they were retired by convention, not deleted. Both are now closed.

## First canary: what it measured, and the defect it exposed

| | prior funnel | first canary |
|---|---|---|
| unique jobs classified | 5,285 | 1,013 |
| assigned to a campaign | 757 (14.3%) | **1,001 (98.8%)** |
| undecided | 3,399 (64%) | 0 |

The scope change works. But the routing basis showed how:

| basis | n |
|---|---|
| OPERATIONS fallback | 834 |
| title route | 239 |
| description or model | 11 |

82% of assignments landed in OPERATIONS. Sampling those titles gave Residential
Plumber, Soup Packer, Oil Delivery Driver, Manual Machinist, Refrigeration
Mechanic, Physical Therapist, Day Porter, General Laborer -- every one already
on the approved hard-exclusion list. They reached a campaign because `facts.py`
reads the DESCRIPTION for physical duties and these postings never state it in
the patterns it matches, and the fallback then presented them as corporate
Operations.

That is the failure mode the policy names explicitly: "Do not use Operations to
hide malformed or incomprehensible records." Fixed by making the TITLE
affirmative evidence of physical duties, checked before any routing and again
at the qualify stage before any paid enrichment.

Nothing was written to Instantly during this measurement: approvals 0, new
outbox rows 0, `delivery_receipts` unchanged at 172.

## Bounded production canary — PASSED

| check | result |
|---|---|
| contacts written to Instantly | **71** across 8 Challenger campaigns |
| genuinely new enrolments acknowledged | **70 `created`** |
| reconciled (already in the target campaign) | 1 `existing` — correctly NOT counted against the cap |
| every destination a Challenger id | **yes**; zero Control ids |
| inserted exactly once | **yes** — delivered = distinct people = distinct emails, per campaign |
| per-campaign ceiling (10 newly created) | held; `gtm_systems` shows 11 delivered = 10 created + 1 reconciled |
| campaigns under ten | marketing 4, product 7, people_hr 9 — the inventory held fewer valid contacts, and the cap is a ceiling, never a quota |

Delivered by campaign: gtm_systems 11, customer_experience 10, finance 10,
ai_technical 10, operations 10, people_hr 9, product 7, marketing_creative 4.
Ecommerce received none this run.

### The 86 legacy rows were stopped, not sent

All 86 pending Instantly rows pointed at retired Control campaigns. They are now
blocked as `compliance:outreach_eligibility_unknown` — they predate migration
011, so `outreach_eligible` is NULL and the compliance gate fails closed before
the retired-campaign guard is even reached. Two independent guards would have
caught them; the earlier one fired.

### Blocked reasons observed, all correct

| reason | n |
|---|---|
| `compliance:unknown_jurisdiction:absent` | 100 |
| `compliance:outreach_eligibility_unknown` (the legacy 86) | 86 |
| `compliance:uk:not_a_verified_corporate_subscriber` | 4 |
| `employer_attribution_conflict` | 1 |
| `compliance:cold_email_not_permitted:DE` | 1 |
| `not_delivered:instantly_existing_other_campaign` | 1 |

### Provider consumption

Fantastic 10 requests / 1,000 credits (the granted ceiling, exhausted).
Anthropic 279 requests, 1.73M input and 199k output tokens.
**Apollo 0 credits** — every contact came from an already-paid stored record,
which is what "prefer valid cached contact records, never re-enrich the same
person" asks for.

## The largest remaining recoverable loss

`compliance:unknown_jurisdiction:absent` blocks 100 otherwise-eligible
contacts — the single biggest bucket. The contact's country is absent on those
records, and the gate fails closed by design. Every one of them sits behind a
US job at a US employer. Populating `contact_country` from the employer's
country when the person's own country is absent, as a recorded inference rather
than a silent default, is the next yield lever and is worth more than any other
change measured here.

## Not done, and why

The daily schedule is NOT restored. `railway.core.json` is written, committed
and correct — it adds `migrate` before the run, sets
`restartPolicyType: NEVER` so an unmet target cannot restart and spend again,
and sets `cronSchedule: 0 3 * * *`. Activating it is one variable
(`RAILWAY_CONFIG_FILE`), and that write is refused by the permission classifier,
as is raising the canary ceiling from 90 to production levels.
