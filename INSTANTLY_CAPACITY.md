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

## The options, with what is known and what is not

| | what it does | monthly cost | contacts | days of 1,000/day before the same wall |
|---|---|---|---|---|
| **A. Recover the space we already own** | archive the 9,591 finished legacy + Control contacts, then rotate our own after 13 days | **$0 extra** (stay on HyperGrowth, $97) | 25,000 | indefinite while rotation runs; 9.6 days of headroom from the first sweep alone, 19.8 if the uncertain groups go too |
| **B. A lead add-on** | — | — | — | Instantly publishes no contact-only add-on; its credits add-on is for Lead Finder and verification, not slots. To be confirmed in Billing |
| **C. Light Speed** | bigger stock | $358 list (+$261) | 100,000 | ~75 days as things stand, ~95 after a sweep — then the same wall, unless rotation runs anyway |

Cost per delivered lead at 30,000/month: **$0.0032** on A, **$0.0119** on C.

**Recommendation: A.** The workspace is not short of space, it is holding 19,811
contacts that no longer receive anything. C pays $261 a month to postpone a problem that
rotation has to solve regardless.

### The one thing that is not verified

That **deleting a contact actually returns a slot** is not proven — only that the stored
total equals the allowance exactly. Instantly's help centre sits behind a bot check and
the API exposes no usage counter. The minimal test is one contact: remove a single
contact from a legacy `completed` campaign, then retry one of the 107 deliveries.

* it succeeds → capacity is recoverable, option A is real, and the sweep can proceed;
* it still fails → the allowance is not a stock and only a plan change can help, which
  makes the decision C instead.

That deletion needs an explicit authorisation. It is one legacy contact whose sequence
finished, and no live campaign is touched.

## Rollback

The gate is a provider row and a code path. To disable it without a deploy, raise
`INSTANTLY_CAPACITY_RETRY_HOURS` to 0 (every caller probes, i.e. the old behaviour minus
the outbox damage). To revert it entirely, revert the commit: the delivery path returns
to failing rows with a backoff, and the `provider_state` row for `instantly` is inert
data that nothing else reads.
