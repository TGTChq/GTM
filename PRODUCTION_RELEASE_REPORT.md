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

## Production completion (2026-09-21)

### Exit semantics

`run-target` now exits 0 for every completed run, with
`result = target_reached | target_not_reached` in the run ledger
(`run_log`, stage `target`, event `end`). It exits 2 only for a broken system:
refused provider credentials, a delivery channel that achieved no successful
write, or a systemic provider error with nothing acquired. Exceptions and
migration failures still propagate non-zero. `restartPolicyType` stays `NEVER`,
so a failure never repeats paid acquisition by itself.

### Contact country: 58 of 100 recovered, from the person's own record

The 100 `compliance:unknown_jurisdiction:absent` contacts were NOT missing
data. 99 carried a `country` in their own stored Apollo evidence
(`people.facts_json.enriched`), 88 a `state`, 84 a `city`. The column
`people.contact_country` arrived with migration 011, nullable and unbackfilled,
and the contact-reuse path never recomputed it; approval read only the column.

| resolved from the person's own stored evidence | n | outcome |
|---|---|---|
| United States | **58** | full compliance re-evaluated, **eligible, returned to `pending`** |
| United Kingdom | 6 | reclassified `compliance:uk:not_a_verified_corporate_subscriber`, still blocked |
| Germany | 1 | reclassified `compliance:cold_email_not_permitted:DE`, still blocked |
| outside the matrix (India 8, Canada 3, Poland, Ukraine, Netherlands, Israel, France 2 each, ...) or absent | 35 | still unknown, still blocked |

Employer location was never read. A declared country the matrix does not
cover stays unknown and never falls through to a state guess — without that
rule a Canadian record with `state: CA` would have read as California; a test
caught it. Zero paid enrichment.

A recovered row returns to `pending`, not `delivered`: delivery's precheck
re-runs suppression, the retired-campaign guard, the ceiling and posting
validity before anything is sent.

### Legacy backlog

The 86 rows that reference retired Control campaigns remain blocked
(`compliance:outreach_eligibility_unknown`). None was remapped.

## Full production cycle — observed funnel (run `20260921T033253.666863Z-6c015841`)

Exact daily configuration, 80 minutes, deployment `598f3e86`, exit 0
(`result=target_not_reached`, `stop_reason=spend_budget_exhausted`,
`technical_failures=[]`).

| stage | observed |
|---|---|
| Fantastic requests | 33 (30 served, 3 uncertain) |
| records billed | 3,300 |
| new unique postings | 2,308 (1,071 employers) |
| classified in window (incl. backlog) | 2,896: 1,474 assigned, 1,422 excluded |
| top hard exclusions | part_time 558, physical_title 345, contract 128, professional_license 95, physical_facility 49, employment:other 48, security_clearance 42, internship 32, temporary 29, field_work 20 |
| assigned by function | operations 824, engineering 238, finance 128, gtm_revenue 95, marketing 80, customer_support 53, people_hr 49, customer_success 40, product 25, ecommerce 11 |
| qualify-stage closures | employer_too_large 314, employer_too_small 169, person_enrichment not permitted DE 7 / AE 4 / SA 1, excluded industry 4, physical_title 2 |
| approvals | 356 = 356 people = 356 emails; 208 employers; 235 company x campaign units |
| verified work email | 356 |
| outreach-eligible | 320 |
| Instantly, this run's approvals | 312 delivered, 27 unknown jurisdiction, 8 UK not verified corporate, 8 already in another campaign, 1 DE |
| Anthropic | 853 requests, 1.34M input / 249k output tokens |
| Apollo (this run) | 1,199 served requests, 485 credits |

Every work item ended in a terminal reason; the only non-terminal states are
`waiting` (a dependency with a scheduled retry) and `retry`.

## Drain run and the daily ceiling

After the three fixes, run `20260921T050148.931160Z-69eb70b2` processed only
already-bought inventory (no new Fantastic spend): 368 approvals, 319 eligible,
160 Instantly writes, stopped on the Apollo budget. OPERATIONS had 242 writes
already that day, so its 155 new contacts were **deferred by the per-day
ceiling** rather than written. This drain is a one-off recovery of stranded
inventory and is NOT part of the sustainable rate below.

## Instantly writes, UTC 2026-09-21

**610 new enrolments acknowledged** (`created`), 2 reconciled, across all nine
Challenger campaigns: operations 242, finance 109, ai_technical 76,
gtm_systems 56, marketing_creative 48, people_hr 28, customer_experience 27,
product 19, ecommerce 5. Zero Control campaigns. Zero duplicate emails. 155
deferred to the next day by the ceiling.

## The arm measurement, and why acquisition went back to priority

| arm | billed | assigned | employment-excluded | eligible | eligible / record |
|---|---|---|---|---|---|
| priority | 482 | 72% | 3% | 152 | **0.315** |
| discovery | 400 | 49% | 24% | 43 | 0.108 |
| exhaustive | 1,814 | 48% | 33% | 139 | **0.077** |

My widened arm was dominated by both others. Slots are now priority 4 :
discovery 1 (`6c79aa1`). The exhaustive scope's gain is in routing, not in
buying, and is unaffected.

## Does the evidence support 1,000 sustainable contacts a day? **No.**

The ~904/day below is DERIVED from one day's measured arm yields, weighted by
the new slot mix. It is not an observed day. The first observed day under the
new allocation is the 2026-09-22 03:00 UTC run.

| constraint | measured basis | supports per day |
|---|---|---|
| Fantastic, 3,300 records/day (100k/month plan) | 0.8 x 0.315 + 0.2 x 0.108 = 0.274 eligible / record | ~904 eligible |
| **Apollo, 1,000 credits/day grant** | 986 credits for 724 approvals; 639 eligible -> 1.54 credits / eligible | **~650 eligible** |
| Instantly, OPERATIONS campaign | 550 emails/day / 4 steps -> ceiling 150; OPERATIONS took 40% of today's writes | ~690 delivered |

**Best estimate for the deployed configuration: about 650 outreach-eligible
contacts a day, bound by Apollo credits, with the OPERATIONS sending ceiling
close behind.** Not observed; derived; one day of arm data.

### What 1,000 a day would take, none of it bought

| provider | needed | now | gap | cost |
|---|---|---|---|---|
| Apollo | ~1,540 credits/day | 1,000/day grant; **balance unknown** — Apollo exposes none without spending | +540 credits/day (~16,200/month) | **unknown** — no USD per Apollo credit exists in the evidence base |
| Instantly | OPERATIONS ~400 new/day x 4 steps = ~1,600 emails/day | daily_limit 550, 33 senders | raise that campaign's daily_limit (a campaign setting, not changed) and possibly senders | no purchase if the existing 252 accounts have headroom |
| Fantastic | 1,000 / 0.274 = ~3,650 records/day | 3,333/day on the plan | ~+320/day, ~9,700/month over 100k | ~$24/month at the $0.0025/record rate used before — plan-tier pricing unconfirmed |

Raising the Apollo daily grant is a one-line change, but only safe once the
Apollo balance is known: if it runs out, every daily run stops producing
contacts until a top-up (Apollo refuses; it never bills overage).
