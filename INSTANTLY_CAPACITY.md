# The Instantly lead limit: what it is, what it cost, and what now stops it

On 2026-09-24 the production run created 975 leads and then hit a wall. Every one of
the remaining 107 deliveries came back with

```
403 {"statusCode":403,"error":"Forbidden","message":"Lead limit reached. Remaining uploads: 0"}
```

Nothing upstream knew. The run kept buying: it finished with 3,900 of 4,000 Fantastic
credits and 1,539 of 1,600 Apollo credits spent, and 107 verified contacts with nowhere
to go. It also stopped believing it had covered the target, because it counted those
107 as credible pending deliveries.

The budget was never the ceiling that day. **The destination was.**

## What was measured (2026-09-24, read-only)

| | |
|---|---|
| Plan | `pid_hg_v1` — HyperGrowth (list price $97/month) |
| Plan allowance | 25,000 uploaded contacts; 125,000 emails/month on the pricing card, 100,000 in the comparison table on the same page |
| Contacts stored **now** | **25,000 across 52 campaigns** — exactly the allowance, which is why remaining uploads is 0 |
| Sending accounts | 252, all active, 5,040 emails/day in total |
| Sequence | 4 steps, delays 3 + 4 + 5 + 1 = **13 days**, stop-on-reply on, all nine campaigns identical |
| Emails actually sent | 9,720 in September so far; 8,556 in August; peak day 1,147 |

Where the 25,000 sit:

| group | campaigns | contacts | share |
|---|---|---|---|
| **Nine Challenger campaigns (live)** | 9 | 5,189 | 21% |
| Nine Control campaigns (8 completed, 1 active) | 9 | 3,811 | 15% |
| Legacy campaigns from before the rebuild | 34 | 16,000 | **64%** |

and by state:

| group | state | campaigns | contacts | finished the sequence |
|---|---|---|---|---|
| legacy | completed | 25 | 6,040 | 5,887 |
| legacy | paused | 5 | 5,025 | 3,307 |
| legacy | accounts unhealthy | 4 | 4,935 | 5 |
| control | completed | 8 | 3,551 | 3,451 |
| control | active | 1 | 260 | 252 |
| **Challenger** | **active** | **9** | **5,189** | 598 |

Of our own 5,189, this rebuild created 4,598 (all distinct people): 1,659 on 09-21,
881 on 09-22, 1,013 on 09-23, 975 on 09-24. The other ~591 pre-date it.

## What now runs: the destination is a gate, like Apollo

`tgtc_core/services/instantly_capacity.py`. A capacity refusal is recorded against the
provider exactly like an Apollo refusal, and it closes the gate in **front** of the paid
stages:

* **Before spending.** `DailyController.run` delivers first — free, owed anyway, and it
  is how we learn whether there is room — and then asks. While the destination is on
  record as full: no Fantastic purchase, no Apollo enrichment, no new work. The run
  stops with `blocked_instantly_capacity`, alerts once, and reports it.
* **Once, not once per row.** The first refusal records the block and ends the sweep.
  Afterwards exactly one caller per interval (`INSTANTLY_CAPACITY_RETRY_HOURS`, default
  1) may retry one contact to find out whether there is room again. No 403 loop.
* **A full destination never damages a contact.** The row stays `pending` with its
  identity, verified email and suppressions intact — not `failed`, so it never ages into
  `blocked: max_attempts` (at 8 attempts the 107 would have been permanently blocked
  within days), and never counted as created.
* **Room again drains the waiting first.** The probe is a delivery, so the contacts
  already paid for go in before anything new is bought, without repeating enrichment.
* **Only this 403.** A bad key, a revoked scope or a Cloudflare 403 keeps its old
  behaviour. Silently stopping acquisition over a credentials problem would hide it.

Tests: `tests_core/test_instantly_capacity_gate.py` (zero capacity, recovered capacity,
a persistent wall over ten days, the probe interval, a restarted process, a non-capacity
403, alert-once, and the two daily-run tests that assert nothing is bought). Each was
verified to fail with the gate neutralised.

## What it takes to sustain 1,000 a day

A lead occupies a slot from creation until its sequence ends: **13 days**. So the
working set at 1,000 new leads a day is about **13,000 contacts**, plus whatever has
finished and has not been removed. The allowance is a stock, not a monthly grant:

| horizon at 1,000/day | contacts needed |
|---|---|
| the 107 waiting now | 107 |
| 7 days | 7,000 |
| 30 days | 30,000 |
| 90 days | 90,000 |
| steady state (13-day sequence) | ~13,000 at any moment |

Two consequences:

1. **No plan sustains it by itself.** 30,000 a month against any fixed stock means the
   stock has to turn over. Rotation is not an optimisation, it is the mechanism.
2. **With rotation, the plan we already pay for is enough.** 13,000 in flight fits
   inside 25,000 with room to spare — *if* the nine Challenger campaigns are what
   occupies it. Today they are 21% of it.

Emails are the second ceiling and are not binding yet: 4 steps × 1,000 leads/day is
~4,000 emails/day (~120,000/month) against 5,040/day of sending accounts and a plan
allowance stated as 100,000–125,000/month. Worth confirming in the account before the
sequence backlog matures.

## Rotation, designed and not executed

Nothing has been deleted. This is the classification the counts above support.

**Safe to archive — 6,040 contacts, 25 legacy campaigns, `completed`.** 5,887 of them
finished the whole sequence; these people were last emailed in a previous era of the
account, the campaigns are done, and no scheduled email depends on them.

**Safe to archive — 3,551 contacts, 8 Control campaigns, `completed`.** 3,451 finished.
The A/B record lives in our own database (approvals, receipts, replies), not in
Instantly, so removing the contacts does not touch the experiment's evidence. It does
reset those campaigns' analytics pages in the Instantly UI.

**Uncertain, needs a decision — 5,025 contacts, 5 legacy campaigns, `paused`.** Only
3,307 finished: 1,718 people are mid-sequence and would never receive the rest.

**Uncertain, needs a decision — 4,935 contacts, 4 legacy campaigns, `accounts
unhealthy`.** Almost none were contacted (5 finished of 4,935): these are the worst
occupancy in the workspace, and also the least "already spent".

**Never — 5,189 contacts in the nine Challenger campaigns, and the 260 in the one
active Control campaign.** Live sequences. Our own contacts become rotatable only after
their 13 days, and that is the steady-state supply: ~1,000 a day, removed 13+ days after
creation, which is exactly the rate we add.

## The options, corrected and measured

Two things in the earlier version of this file were wrong, and the account settled both:

* **deleting a contact does return a slot.** Proved on 2026-09-24: one finished legacy
  contact removed at 07:07:14Z, the workspace counter went 25,000 -> 24,999 at 07:12:41Z.
  About five and a half minutes, matching Instantly's documented 5-10. Deleting a CRM
  list entry does not do this; deleting the lead does.
* **an Email Outreach add-on exists**: $87/month list price for 25,000 further uploaded
  contacts, and it is not the Instantly Credits add-on. The earlier claim that no
  contact add-on existed came from the public pricing page and was wrong.

| | what it does | monthly cost | contacts | days of 1,000/day before the same wall |
|---|---|---|---|---|
| **A. Rotate finished contacts** | remove contacts whose sequence ended and who never replied | **$0** | 25,000 | 9 from the legacy pool alone; **indefinite** once our own finished contacts rotate too |
| B. Email Outreach add-on | +25,000 stored contacts | $87 list, to be confirmed at checkout | 50,000 | ~34, then the same wall unless rotation runs anyway |
| C. Light Speed | +75,000 stored contacts | $358 (+$261) | 100,000 | ~95, then the same wall |

Cost per delivered lead at 30,000/month: **$0 on A**, $0.0029 on B, $0.0119 on C.

**Chosen: A, and B not purchased.** Rotation restores capacity before acquisition
resumes, which is what the authorisation made the add-on conditional on, and it is the
only option that is sustainable rather than a postponement. B stays available and
pre-priced if the rotation policy is ever declined.

### What was rotated on 2026-09-24

Every candidate was backed up in full and judged one at a time before anything was
removed: 9,591 contacts exported from 33 finished campaigns (18 MB, sha256 per file,
`C:/TGTC/instantly_rotation_private/backup`), of which **9,079 were individually
removable** and 512 were protected -- 261 had replied, 253 had not finished the sequence
(bounced), and **one was a contact still awaiting delivery for us**.

Nothing was removed by campaign. The nine Challenger campaigns and the active Control
campaign are refused by id in the tool itself, and every removal is written to a ledger
BEFORE the call, so an interrupted session resumes without repeating one.

## What it takes to sustain 1,000 a day: the sending side

Slots are only half of it. Measured on the same day: **252 inboxes, every one at 20 a
day, sending Monday to Friday 08:00-18:00 America/Chicago** -- 25,200 emails a week. A
four-step sequence at 1,000 leads a day needs 7,000 x 4 = **28,000 a week**.

So the intake the current setting can carry is **900 a day**, and at 1,000 the unsent
queue grows by **2,800 emails a week**. It is not a forecast: 2,234 of our 5,189 stored
contacts had not received a first email on 2026-09-24.

**But the configured limit is not what binds.** Actual sends in the nine campaigns were
**1,141 on 09-21, 795 on 09-22 and 979 on 09-23** -- about **20% of the 5,040
configured**. All 252 inboxes are attached across the nine (33 each, 80 on OPERATIONS,
21 on PEOPLE & HR) and the campaign ceilings total 6,000 a day, so neither limit is the
constraint. Raising 20 -> 23 would raise a ceiling nobody is touching, so it was tested
on one inbox (`PATCH /accounts/{email}` answers 200 and reads back 23) and **reverted**:
all 252 are back at 20.

What actually holds first emails at ~800 a day is not visible through the v2 API. That
is the open question behind the 2,234 contacts stored with no first email, and it needs
the Instantly UI -- campaign sending health, the four `accounts unhealthy` campaigns, or
per-account ramp. Until it is answered, **first-email throughput, not lead slots, is the
binding constraint on the outreach behind 1,000 creations a day.**

`scripts/instantly_occupancy_forecast.py` simulates both ceilings per calendar day from
the live account, including weekend steps waiting for Monday.

## A third ceiling, found while measuring: semantic classification was dead

The day's budget carried **1,500 Anthropic reservations, every one refused, none
served**, with 3,710 deterministic classifications and no semantic ones. One call from
the core's own environment gave the reason:

```
400 {"type":"invalid_request_error","message":"Your credit balance is too low to access the Anthropic API."}
```

Every classify call had been failing all day. Because a 400 is a configuration failure,
the work item correctly waited -- and tried again next cycle, 1,500 times, each attempt
consuming one of the day's 1,500 request slots and classifying nothing.

The provider is now gated exactly like Apollo and Instantly: one refusal puts it on
record, one caller per `TGTC_INFERENCE_RETRY_HOURS` (default 1) asks again, and a served
call lifts it. Postings still wait rather than close -- a billing problem must never
lose a job.

**This does not fix the cause.** The Anthropic account needs credit, which is outside
what this work was authorised to buy. Until then, classification is deterministic only,
which is the measured reason a run needs ~3,900 Fantastic records to produce ~1,000
leads instead of fewer.

## Two guards that are not about capacity, and shipped with it

**A deploy no longer kills a run.** A push to `feat/rebuild-core` replaces the core
container; on 2026-09-24 one at 03:40:11Z ended that day's run 30 seconds later with 73
approvals produced and nothing delivered. `.githooks/pre-push` refuses a push to that
branch while the run lock is held (`git config core.hooksPath .githooks`, override with
`TGTC_ALLOW_DEPLOY_DURING_RUN=1`), and `python -m tgtc_core run-lock` answers the same
question anywhere, exiting 3 when a run is in flight.

**The kind of a run is declared, never read off the clock.** The start command decided
`scheduled` vs `manual` from the UTC hour, so the same recovery resumed the day's
allowance at 04:47 and would have opened a second one at 06:00. `scheduled` needs no
declaration -- the service has one cron and that is what it is; every other kind must be
named through `TGTC_RUN_KIND`, and both `budget` and `run-daily` refuse before touching
the database or a provider when it is not.

The start command still contains the old hour test. It is now inert in the safe
direction (an hour-inferred `manual` run is refused rather than silently given a new
allowance), and replacing its text needs one manual step, because
`serviceInstanceUpdate` is blocked for me:

```
railway api 'mutation { serviceInstanceUpdate(serviceId: "f83cd97a-135d-48e3-8e12-d517a51edfff", environmentId: "bae427bd-64a6-4f4e-8f56-fbd406985434", input: { startCommand: "..." }) }'
```

with `KIND=${TGTC_RUN_KIND:-scheduled}` and `BID=${TGTC_BUDGET_ID:-$(python -m tgtc_core budget-id --kind $KIND)}` in place of the hour test. Setting
`TGTC_RUN_KIND=scheduled` as a service variable has the same effect today.

## Rollback

The gate is a provider row and a code path. To disable it without a deploy, raise
`INSTANTLY_CAPACITY_RETRY_HOURS` to 0 (every caller probes, i.e. the old behaviour minus
the outbox damage). To revert it entirely, revert the commit: the delivery path returns
to failing rows with a backoff, and the `provider_state` row for `instantly` is inert
data that nothing else reads.
