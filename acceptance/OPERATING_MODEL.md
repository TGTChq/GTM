# Operating the pipeline permanently

The objective is **at least 1,000 distinct, new, genuinely Approved Airtable contacts
per daily run**, continuing past that while eligible work and authorized resources
remain. Postings, contacts found, Pending rows, duplicates and previously delivered
contacts are not that number.

## Effective configuration, read live

`railway api` returns variable VALUES on this path, so effective state is directly
verifiable rather than inferred from a container printout. Read 2026-09-07, after the
source change below:

| variable | GTM | meaning |
|---|---|---|
| `MAINTENANCE_ONLY` | `1` | the pipeline loop cannot run |
| `FANTASTIC_JOBS_ENABLED` | `0` | paid acquisition paused |
| `ACQUISITION_EXTRA_LANES` | `ats` | **set now** — the 145 direct boards, zero provider credits |
| `ATS_DIRECT_ACQUISITION_ENABLED` | `1` | registry loads 145 cleanly |
| `APOLLO_RECOVERY_BUDGET_ENABLED` | `true` | the durable ceiling is in force |
| `APOLLO_RECOVERY_BUDGET_ID` | `luis-20260907-newjobs-2045-stage1` | **SPENT**, 200 of 200 |
| `APOLLO_RECOVERY_BUDGET_CALLS` | `0` | nothing authorized |
| `APOLLO_CACHE_ENABLED` | `1` | verified-match reuse live |
| `NET_NEW_SEND_SAFE_TARGET` | `1000` | enables the per-run target by default |
| `AIRTABLE_WRITE_SEND_SAFE_ONLY` | `1` | only send-safe rows are written |
| `VERIFY_WITH_HUNTER` | `0` | no second email opinion |

Unset variables take the repository default; the 2026-09-07T07:02Z container printout
confirmed those resolve to pending-work on, per-run target 1000 with continue-after,
window slicing on, send-safe auto-approve on and company × function suppression on.
Approved Sync carries `VERIFY_WITH_HUNTER=0`, `ENROLLMENT_PERSON_EMPLOYER_UNIQUENESS=1`
and `OUTBOUND_WAVE1_ENABLED=1`. Crons unchanged: `0 3 * * *` and `0 0 * * *`.

## Apollo is serving again — measured, 2026-09-07T19:23:17Z

`acceptance/apollo_readiness.py`, run through `railway run --no-local --service GTM`
under a one-call authorization opened for the purpose:

```
HTTP 200   x-request-id c4cf22ce-a5ee-4067-aaef-20e546b83ae8
READY: Apollo served a credit-consuming call.
```

**What that establishes:** at 19:23:17Z the account served a chargeable
`organizations/enrich`, so lead credits exist. That call consumed one.

**What it does NOT establish:** how many. A 200 carries no balance; only a refusal
does, in its `credit_balance` field. The historical 2,045 is not reused and no
credit-to-call rate is assumed — one reservation is at most one lead credit, which
bounds spend from above and says nothing about what a call actually costs.

The probe's authorization has been returned to `APOLLO_RECOVERY_BUDGET_CALLS=0`.

## Continuous mode: no manual grant after a top-up

`APOLLO_CONTINUOUS_MODE` replaces "did someone issue a grant today" with "is Apollo
serving", which is the question that can actually be answered automatically.

**Learning the answer is free.** A credit refusal is returned before any work is
done, so the run's own first chargeable call already IS the availability check. There
is no separate probe and no credit spent finding out that there are none.

**The refusal is durable and throttled.** `orchestrator/apollo_availability.py`
records when the provider last refused, on the mounted volume, so a restart does not
forget. A refusing provider is retried once `APOLLO_AVAILABILITY_RETRY_HOURS` (6) have
passed — on a schedule, never once per company and never in a loop.

**Recovery is a side effect of the next run succeeding.** The moment a chargeable call
goes through, the record flips to serving and paid acquisition resumes. No new
authorization id, no variable edit, no person.

**What it does not relax.** The durable ledger still records every call, so spend
stays auditable with no cap set. A configured aggregate still refuses exactly as
before. Quality gates, contact and email validation, dedupe, suppression, custody and
the Fantastic governor are all untouched. `SERVING` is a memory of the last answer,
never a prediction: the next refusal flips it straight back.

Verified offline before arming — exhaustion, restart and automatic recovery
(`tests/test_continuous_apollo_operation.py`, 14 tests), including that an unknown or
corrupt record reads as "attempt" because a failed attempt is free while a wrongly
withheld one costs a day.

## No credits: what happens, and how it comes back

**Exhaustion is a pause, not a loss, and not a relabelling.** Both our own
`BudgetExhausted` and the provider's `ApolloCreditsExhaustedError` are in
`GLOBAL_FATAL_ERRORS`. That is deliberate and load-bearing: the enrichment loop's
other handler catches `Exception` per company, marks it UNVERIFIED and continues, so a
credit error raised there would repeat on every remaining company and relabel the whole
cohort as UNVERIFIED while spending nothing. Being globally fatal stops the loop,
preserves completed work, and leaves the company that tripped it unprocessed.

**No retry storm, and a controlled retry.** Because the error is globally fatal, a run
attempts once and stops — there is no in-run retry loop against a refusing provider.
The retry cadence is the schedule itself: the next `0 3 * * *` run tries again. No
additional backoff mechanism was added, because the daily cron already is one.

**Unfinished work survives.** A withheld FINAL_PASS and a NEEDS_CHECK are both excluded
from `terminal_posting_ids`, so their postings stay in `pending_work` and a later run
resumes them. Custody currently holds 3,524 postings across 4 runs.

**The ceiling is durable and covers every potentially paid request.** Reservations are
taken BEFORE each call, inside the retry loop (`before_attempt`), so a retry is
reserved too. The ledger lives on the mounted volume, so a deploy does not reset it.
The availability probe is now inside the ceiling as well — see below.

### Provider balance and internal authorization are different things

Topping up Apollo does **not** renew a grant that is zero or spent, and this is
enforced rather than documented:

* an unset or spent grant refuses, and an unset grant is **zero, never unlimited**;
* re-using a spent `APOLLO_RECOVERY_BUDGET_ID` with a larger number **resumes** that
  grant's consumed counter — it does not open a new one. Only a NEW id resets;
* `acceptance/apollo_readiness.py` used to issue its probe with a raw client, outside
  the budget. The one call made precisely when nobody knows what the account will do
  was the one call the ceiling could not see. It now reserves first and exits **3 —
  NOT AUTHORIZED HERE** when the internal grant refuses, saying explicitly that Apollo
  credit will not change that.

Reservations count **request attempts by kind**, not provider credits. What Apollo
charges for a request depends on the response and the plan, so the ceiling bounds
requests and says so.

## Activation order

Budgets and sources first, maintenance last. **Never publish during a run** — the
window is 03:00Z plus the run's duration.

1. **Sources** — `ACQUISITION_EXTRA_LANES=ats`. Free inventory, no provider credits.
2. **Ceiling in force** — `APOLLO_RECOVERY_BUDGET_ENABLED=true`.
3. **Per-grant cost ceiling** — `APOLLO_RECOVERY_BUDGET_CALLS`. Currently `0`.
4. **Acquisition armed** — `FANTASTIC_JOBS_ENABLED=1`. **BLOCKED HERE** — refused by
   the permission layer. Safe to leave on permanently *because* paid lanes stand down
   without an enrichment authorization.
5. **Maintenance cleared** — `MAINTENANCE_ONLY=0`. **BLOCKED HERE** — same refusal.
   Last step.

Steps 1 and 2 are done. **Steps 3–5 need a hand**, and until 4 and 5 are applied the
service is not running on a schedule at all — no top-up of Apollo will change that.

```bash
railway api 'mutation { variableUpsert(input: {projectId: "898f2e3a-1c1e-4b00-b9a6-686cf0432282", environmentId: "bae427bd-64a6-4f4e-8f56-fbd406985434", serviceId: "3a41d0d7-cd66-4f53-baa6-886266ddbbed", name: "FANTASTIC_JOBS_ENABLED", value: "1"}) }'
```

```bash
railway api 'mutation { variableUpsert(input: {projectId: "898f2e3a-1c1e-4b00-b9a6-686cf0432282", environmentId: "bae427bd-64a6-4f4e-8f56-fbd406985434", serviceId: "3a41d0d7-cd66-4f53-baa6-886266ddbbed", name: "MAINTENANCE_ONLY", value: "0"}) }'
```

Apply them outside the 03:00Z window. The service is then ARMED and the `0 3 * * *`
cron runs every night on its own, standing paid lanes down until a grant exists.

## The three states, and what each night does

**No authorization** (`APOLLO_RECOVERY_BUDGET_ID` unset or spent). The run starts,
resolves the authorization once before any lane, and **stands paid lanes down** —
recorded as `paid_acquisition_stood_down`, not a failure. Free lanes still run,
custody still drains as far as it can without a chargeable call, delivery still
writes what completed, and unfinished postings stay in custody. **Nothing is bought.**
This is the recoverable wait: it costs nothing and repeats nightly until authorized.

Buying ahead of the grant would not be saving. A posting is worth its price only once
a contact can be found for it; bought early it becomes backlog, and the next
authorization is spent on inventory acquired at a worse moment.

**Authorized.** Acquisition runs, enrichment spends against the durable ceiling,
delivery writes send-safe rows, and the per-run objective is 1,000 distinct NEW
Approved with `CONTINUE_AFTER_TARGET` so a good night is not truncated at the goal.

**Authorization spent mid-run.** The charge is globally fatal by design: the loop
stops, completed work is preserved, the rest stays in custody, and the approvals
already delivered in the final batch are still counted. The next night stands paid
lanes down until a new grant exists.

## Resuming after a top-up: ONE action

**Apollo's balance and this authorization are different things, and topping up the
provider changes nothing here.** With `APOLLO_RECOVERY_BUDGET_CALLS` pre-set, the
whole of resumption is one variable:

```bash
railway api 'mutation { variableUpsert(input: {projectId: "898f2e3a-1c1e-4b00-b9a6-686cf0432282", environmentId: "bae427bd-64a6-4f4e-8f56-fbd406985434", serviceId: "3a41d0d7-cd66-4f53-baa6-886266ddbbed", name: "APOLLO_RECOVERY_BUDGET_ID", value: "<NEW-ID>"}) }'
```

`<NEW-ID>` must be one never used before — a date and purpose is enough, e.g.
`luis-20260910-operating-1`. A NEW id is what resets the consumed counter; re-using a
spent id **resumes** it and grants nothing, which is enforced and tested rather than
merely documented. The next `0 3 * * *` run picks it up. Nothing else changes: no
redeploy is needed beyond the one the variable change itself triggers, and no counter
is reset by deploying.

To change the appetite rather than renew it, set `APOLLO_RECOVERY_BUDGET_CALLS`
instead — that is the cost dial, and it takes effect **on the next new grant**. An
authorization that is already open keeps the size it was opened with; the ceiling can
always be *lowered*, never raised in place.

> **Why that rule exists.** On 2026-09-07 I set the ceiling to 2,000 on the live
> service while the SPENT 200-call id was still in place. That handed a closed
> authorization 1,800 calls of fresh room — a grant nobody issued, and one that would
> have been indistinguishable in the ledger from a real one. Maintenance was on and
> the cron had not fired, so nothing was spent; only the schedule prevented it. The
> ceiling was reverted to 0 within four minutes, and the module now refuses the
> manoeuvre outright rather than relying on the operator not to make it.

That is also what makes pre-arming safe: `APOLLO_RECOVERY_BUDGET_CALLS` can sit at
its operating value beside a spent id and grant nothing at all, so resumption is one
variable with no window in which the old authorization is quietly enlarged.

### Why the balance cannot size this, and why that is survivable

The lead-credit balance is not readable from here at zero cost: `auth/health` returns
200 with or without credits, and the only true signal is a credit-consuming call,
which now correctly requires a grant. **There is no automatic detection of a top-up**,
and adding credit alone will not restart anything.

What makes that survivable is the unit. **One reservation is at most one lead
credit** — a reservation is taken per potentially paid request attempt, and a refused
attempt costs a reservation while costing Apollo nothing. So a ceiling of N calls
bounds spend at **no more than N lead credits**, without knowing the balance. And a
ceiling above the balance is not overconsumption: credits that do not exist cannot be
spent, so Apollo refuses first and the run pauses cleanly.

A ceiling of **2,000** is the suggested starting value: an explicit cap of at most
2,000 potentially paid requests per grant, an order of magnitude above the only grant
ever exercised (200, on 2026-09-07), and bounded whatever the balance turns out to be.
It is a cap, not a forecast — nothing predicts how many Approved rows 2,000 requests
produce, and the 2026-09-07 run is not a rate.

It is **not set on the service**: it was set and then reverted to `0` (see the box
above), and setting it now would be inert anyway while the spent id stands. Set it in
the same session as the new id.

## What is proved, and what is not

Proved offline, through the real orchestrator with faked provider and Airtable
boundaries: 1,500 distinct new Approved in one run; 3,524 owed drained against a
2,000-row batch, so a batch size is not a daily ceiling; zero approvals when rows are
created but not approved, and when every write fails; a budget interruption still
counts the batch it delivered and leaves the rest in custody; a re-delivered lead adds
nothing.

**Not proved:** any of this in production. No run has executed on this code. Simulated
approvals are not Approved Airtable receipts, and 1,000 per day remains the objective
rather than a demonstrated result.

**Still evidence gaps, unchanged:** the 27 withheld contacts individually (needs a
maintenance pass; the cron mutation for it is refused in this session), the
`hiring_manager_not_found` 169 against `hm_not_found` 147 disagreement, and the
response-level attribution of the 172 duplicates and the historical 5,218 / 2,722.
