# Recovery ledger -- 2026-09-25

Persistent record of every change, execution, spend and receipt in this piece of work, so
it can be resumed without repeating anything. Append-only: entries are never rewritten,
only corrected by a later entry that says so.

## STATE RIGHT NOW

| thing | value |
|---|---|
| **CORE CRON** | **PAUSED (`null`) since 2026-09-25 02:14Z -- MUST BE RESTORED TO `0 3 * * *`** |
| core deployment in force | `56df036e-4d08-4ced-b296-ae00b66757ab`, commit `1633b6ce6998`, restart NEVER |
| run lock (advisory `1952937059`) | free |
| last completed run | `20260924T055100.714673Z-b60f0376`, ended 2026-09-24 05:52:24Z |
| 2026-09-25 03:00Z scheduled run | **did not run -- deliberately paused before the tick** |
| Weekly Report cron | untouched (`0,20,40 13-20 * * *`) |
| Replies cron | untouched (`15 * * * *`) |

## Entries

### 1. 2026-09-25 02:10Z -- preflight, before touching anything
Read: time 02:10:34Z, run lock **0**, no `daily/start` after 02:55Z of the prior day, last
`daily/end` 2026-09-24 05:52:24Z. Effective core deployment `56df036e-4d08-4ced-b296-ae00b66757ab`
(SUCCESS, commit `1633b6ce6998`, `cronSchedule: 0 3 * * *`, `restartPolicyType: NEVER`).
The 03:00Z run had **not** started, so the pause path applies rather than the watch path.

Full start command and manifest saved to the session scratchpad as
`core_deploy_before_pause.json`. The start command, reproduced here so it can be restored
from this file alone:

```
sh -ec 'KIND=${TGTC_RUN_KIND:-}; if [ -z "$KIND" ]; then H=$(date -u +%H); if [ "$H" -ge 3 ] && [ "$H" -le 5 ]; then KIND=scheduled; else KIND=manual; fi; fi; BID=$(python -m tgtc_core budget-id --kind $KIND); echo "run kind=$KIND budget=$BID"; python -u -m tgtc_core migrate && python -u -m tgtc_core budget --budget-id $BID --budget-kind $KIND --expires-hours 24 --fantastic-requests 60 --fantastic-credits 4000 --apollo-requests 10000 --apollo-credits 1600 --anthropic-requests 1500 --anthropic-input-tokens 14000000 --anthropic-output-tokens 1600000 && python -u -m tgtc_core run-daily --budget-id $BID --budget-kind $KIND --target 1000 --block-pages 2 --max-rounds 300 --max-items 10000 --i-understand-spend'
```

`TGTC_RUN_KIND` is already `scheduled` on the service, so the hour-of-day branch above is
dead in production and a run started at any hour is still `scheduled`.

### 2. 2026-09-25 02:14Z -- core cron paused
`serviceInstanceUpdate(serviceId: f83cd97a…, environmentId: bae427bd…, input: {cronSchedule: null})`
returned `true`. **`true` is not proof**, so it was read back:
`serviceInstance.cronSchedule` is now `None`, `numReplicas` 1, `restartPolicyType` NEVER,
and the start command is preserved (713 chars).

The *deployment* manifest still prints `cron= 0 3 * * *`, because that manifest is a
snapshot taken when the deployment was created, not live config. The service instance is
what the scheduler reads -- `PRODUCTION_CHANGE_LEDGER.md` row 2 established that when it
read `cronSchedule: None` on all six services and nothing was in fact scheduled anywhere.
No redeploy was forced to make the two agree: with `cronSchedule` null a redeploy might
not be `buildOnly`, which could start the container and run a full production job on old
code. Not worth it 45 minutes before a tick. The pause is confirmed instead by the service
instance reading plus watching 03:00Z pass with no `daily/start`.

Only GTM Core Canary 1000 was touched. Weekly Report and Replies were not.

**Rollback for this entry:** same mutation with `cronSchedule: "0 3 * * *"`.

### 3. 2026-09-25 02:20Z -- branch published
`feat/weekly-slack-csv` pushed to origin at `185809b`. It carries the three commits that
until now existed only on this laptop -- `7a60472` (the one-step follow-up), `fb57cee`
(rotation with a durable backup), `a7e90b0` (the subsequence probe and move confirmation)
-- plus this ledger. Nothing is deployed by this: no service builds from that branch.

### 4. 2026-09-25 02:22Z -- rotation made automatic, and bounded by the occupancy
Rotation now decides for itself, and the decision is read BEFORE anything is spent:

* `GET /campaigns/analytics` gives every campaign's `leads_count` in **one** request, so
  occupancy is known without paging 25,000 contacts. Live now: **19,549 stored of 25,000,
  so 5,451 free** (the nine hold 5,188; the rest is legacy);
* `room_needed(free, target, reserve)` = `target + reserve - free`. With the default
  1,500 reserve and a 1,000 target that is `-2,951`, i.e. **zero**. Nothing is deleted
  today, and the expensive candidate scan never even runs;
* when it IS short, it rotates in bounded batches, then **re-reads the occupancy** and
  reports `freed_measured` -- a delete that answered 200 is not a slot until the
  workspace says so;
* if there are not enough safe candidates, it returns the exact `deficit` and the daily
  run stops before buying, with `acquisition_stop = instantly_slots_short_by:<n>`.
  Delivery still runs first, so contacts already paid for are still handed over.

Every guard from the manual rotation is unchanged and still tested: durable backup row
written before the delete, eligibility by campaign id, sequence finished, no reply, no
suppression or pending delivery of ours, the eighteen live ids refused by id, hard batch
ceiling, one ledger row per lead id, idempotent.

### 5. 2026-09-25 02:26Z -- the out-of-office follow-up, end to end
Migration **019** adds `followup_deliveries`, keyed on the Instantly lead id. It records
`moved_at` and `sent_at` as two separate facts, because they are two separate events.

The decision now re-checks, in this order: suppression, opt-out, **a reply of their own
since the auto-reply** (new -- a real answer supersedes an out-of-office), the approval,
the vacancy, our own creation receipt, and the contact's live state in Instantly. Then:

* a contact **still in sequence** is held, never moved -- moving them would stop emails
  already going out;
* the move is confirmed against Instantly's own record, since `POST /leads/move` answers
  200 with a job that is still `pending`;
* a confirmed move records `moved_to_followup` and then **waits** in
  `followup_awaiting_send`. It is NOT finished;
* it is finished only when a message with `ue_type: 1` exists for that contact in the
  one-step campaign. Instantly's message id is stored as the receipt. Their own
  out-of-office (`ue_type: 2`) is in the same feed and is never mistaken for ours;
* a contact already in the destination is never moved again, so a retry cannot cause a
  second email.

**Campaign created: `f0665173-d46b-49e6-8f17-4e024876a5a3`** -- "TGTC OOO FOLLOW-UP -
single step". One step, no delay; 252 senders (the same estate the nine campaigns use,
all status 1, warmup 1, 20/day); `stop_on_reply` and `stop_for_company` on; unsubscribe
header on; text only; `daily_limit` 50, `daily_max_leads` 50. Its final schedule is the
same business hours as the nine (Mon-Fri 08:00-18:00 America/Chicago).

The copy is one short message that claims nothing about the offer: it says we wrote
before, caught an out-of-office, that this is a single follow-up and not another
sequence, and that no reply is needed. The signature is `{{accountSignature}}`, the
sending account's own. It does **not** use `{{rendered_email_1_html}}` -- that is the
variable the nine campaigns render the original first email from, and reusing it would
have re-sent the very email the contact already had.

`TGTC_OOO_FOLLOWUP_CAMPAIGN_ID` is **not set yet**. No real contact can be moved until it
is, and it will not be set until the internal test has produced a receipt and the
schedule is back to business hours.

**The inert campaign from the probe is NOT reused.** `aa271e68-1081-4099-bf33-2ce96759f6cc`
still holds the single probe contact, still has no steps and no senders, and is a
different campaign from the follow-up one.

### 6. 2026-09-25 02:28Z -- internal test in flight
One of our own inboxes, `devan@globaltalentassist.com`, was added to the follow-up
campaign as lead `01a0d664-5fd3-7e29-b975-61e4907a4b55`, and the campaign activated with
that contact as its ONLY member. The schedule was temporarily widened to 00:00-23:59 all
days so the send happens now instead of at 13:00Z.

**Outstanding for this entry:** read the receipt, confirm exactly one message and that
`{{firstName}}` / `{{companyName}}` / `{{accountSignature}}` rendered, then restore the
business-hours schedule, remove the test contact, and only then set the env var.
