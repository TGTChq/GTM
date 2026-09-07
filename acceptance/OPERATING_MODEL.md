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

Budgets and sources first; maintenance last. **Do not publish during a run** — the
window is 03:00Z plus the run's duration.

1. ~~Sources~~ — done: `ACQUISITION_EXTRA_LANES=ats`. Free inventory, no credits.
2. ~~Budget enforcement~~ — confirmed: `APOLLO_RECOVERY_BUDGET_ENABLED=true`.
3. **Apollo authorization — BLOCKED, see below.**
4. `FANTASTIC_JOBS_ENABLED=1`.
5. `MAINTENANCE_ONLY=0`. Last.

### What blocks step 3, exactly

**The Apollo lead-credit balance cannot be read from here at zero cost.** `auth/health`
returns 200 whether or not credits remain, so the only signal is attempting a
credit-consuming call — and that call now correctly requires a grant. Sizing
`APOLLO_RECOVERY_BUDGET_CALLS` without the balance risks exactly the overconsumption
that is not authorized, so the number is not chosen here.

**The missing datum:** the workspace's lead-credit balance, from Apollo's Plan overview
/ Credits and activity screen, with the timestamp it was read.

Two commands, once that number exists. Both need a **new** id — the current one is
spent, and re-using it resumes its counter:

```bash
railway api 'mutation { variableUpsert(input: {projectId: "898f2e3a-1c1e-4b00-b9a6-686cf0432282", environmentId: "bae427bd-64a6-4f4e-8f56-fbd406985434", serviceId: "3a41d0d7-cd66-4f53-baa6-886266ddbbed", name: "APOLLO_RECOVERY_BUDGET_ID", value: "<NEW-ID>"}) }'
```

```bash
railway api 'mutation { variableUpsert(input: {projectId: "898f2e3a-1c1e-4b00-b9a6-686cf0432282", environmentId: "bae427bd-64a6-4f4e-8f56-fbd406985434", serviceId: "3a41d0d7-cd66-4f53-baa6-886266ddbbed", name: "APOLLO_RECOVERY_BUDGET_CALLS", value: "<N>"}) }'
```

**Sizing `<N>`.** One reservation is one potentially paid request attempt, not one
credit. The 2026-09-07 run consumed 200 reservations reaching 184 companies and
produced 56 contacts and 28 Airtable rows. Nothing establishes a rate from that — the
run was interrupted, and requests and credits are different units — so `<N>` should be
set from the balance and the appetite for one day's spend, not from a projected yield.

Then steps 4 and 5, same mutation shape with `FANTASTIC_JOBS_ENABLED=1` and
`MAINTENANCE_ONLY=0`.

### Why acquisition and maintenance were not flipped here

With the Apollo grant at zero, a run enables paid Fantastic acquisition and then stops
at its first chargeable Apollo call. It would buy postings it cannot enrich and produce
**zero** Approved contacts. Custody would preserve them, so nothing is lost — but it is
spend with no output, and the authorization is explicitly for operating within
available credits without overconsumption. Steps 4 and 5 belong after step 3.

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
