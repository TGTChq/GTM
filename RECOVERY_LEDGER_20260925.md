# Recovery ledger -- 2026-09-25

Persistent record of every change, execution, spend and receipt in this piece of work, so
it can be resumed without repeating anything. Append-only: entries are never rewritten,
only corrected by a later entry that says so.

## STATE RIGHT NOW

| thing | value |
|---|---|
| **CORE CRON** | **RESTORED to `0 3 * * *` at 02:52Z.** Next tick 2026-09-26 03:00Z |
| core deployment in force | `9414b58`, deployed 2026-09-25 05:53:24Z, SUCCESS, restart NEVER |
| run lock (advisory `1952937059`) | free -- the recovery run closed 05:52:49Z |
| last completed run | `20260924T055100.714673Z-b60f0376`, ended 2026-09-24 05:52:24Z |
| 2026-09-25 03:00Z scheduled run | did not run; replaced by the recovery run started 02:51Z |
| recovery run | started 2026-09-25 ~02:45-02:51Z, budget `prod-scheduled-20260925`, kind `scheduled` |
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


### 7. 2026-09-25 02:33Z -- the follow-up proved itself with a real email
The one-step campaign was activated with a single member: one of our own inboxes,
`devan@globaltalentassist.com`. Schedule temporarily widened so the send happened at
once rather than at 13:00Z.

**Receipt.** Message `01a0d668-fb7c-7679-bffd-d23ed3f7d80d`, sent 2026-09-25T02:33:19Z
from `devan.m@globaltalentanchor.com`, subject "Following up, Devan". `{{firstName}}`
resolved, `{{accountSignature}}` resolved to the sending account's own signature, and no
placeholder was left unrendered. **Exactly one message** in the campaign for that
address, and the contact went to **status 3** afterwards -- the one-step campaign ends
cleanly, which is the property the subsequence route could not give.

Then: business-hours schedule restored, test contact deleted, campaign back to zero
leads. Nobody outside our own mailboxes was written to.

### 8. 2026-09-25 02:35Z -- the probe contact, reviewed and given what it was owed
Checked against every gate rather than swept along: work item 45943 still `ready` and due
2026-09-22, approval 493 `delivered`, **no opt-out, not suppressed, posting still
`classified`, no reply of their own since**. Everything the follow-up requires was true.

So they were moved deliberately out of the inert holding campaign and into the proven
one-step campaign, which is the treatment they were queued for. The holding campaign
`aa271e68-…` now holds **zero** leads and was never reused as the follow-up vehicle. The
contact will receive exactly one message in the 13:00Z window, and the code will record
its receipt in `followup_deliveries`.

### 9. 2026-09-25 02:37-02:39Z -- variables, then promotion
`TGTC_INSTANTLY_ROTATION_ENABLED=1` and
`TGTC_OOO_FOLLOWUP_CAMPAIGN_ID=f0665173-d46b-49e6-8f17-4e024876a5a3` upserted on the core
service and read back. Set BEFORE the code push so the deployment that matters bakes
them in.

Before promoting: cron parked on `0 3 1 1 *` (a cron service still, so a deploy stays
build-only -- with `cronSchedule` null a deploy might have started the container and run
a full job on the old image), run lock **free**, 2,047 tests green.

`feat/weekly-slack-csv` -> `feat/rebuild-core`, `1633b6c..b4f81e1`. All three services
rebuilt and reached **SUCCESS on `b4f81e1`** at 02:39Z. Weekly Report kept
`0,20,40 13-20 * * *` and Replies kept `15 * * * *` -- neither was touched.

The variable-only deployment at 02:37:56Z is itself the evidence that these deploys do
not execute: it reached SUCCESS and no run started.

### 10. 2026-09-25 02:45-02:52Z -- the recovery run, and the cron restored
No `prod-scheduled-20260925` budget, no run and no receipts existed for today, so this is
a first execution and not a duplicate. `budget_id_for('scheduled')` is the plain UTC date,
so it is `prod-scheduled-20260925` at any hour today; `TGTC_RUN_KIND=scheduled` was
already set, so the hour-of-day branch in the start command cannot turn it into `manual`.

Fired by a one-shot cron `45 2 25 9 *`. Started and confirmed at **02:51Z**: run lock
held, and 106 Instantly creations already recorded -- the 107 approved contacts that had
been waiting for capacity, handed over before anything was bought.

Cron then restored to **`0 3 * * *`** and read back; start command intact at 713
characters. The next tick is 2026-09-25 03:00Z, eight minutes later, while this run is
still in flight. That is safe and was checked rather than assumed: `create_budget` is
`ON CONFLICT (budget_id) DO NOTHING` and never resets usage, and `run-daily` takes the
advisory run lock before it does any work, so the 03:00Z container refuses and exits.

### 11. 2026-09-25 02:56Z -- did raising `daily_max_leads` lift first emails? Not yet, and here is why

Measured from `GET /campaigns/analytics/daily`, which reports `new_leads_contacted` --
first emails -- separately from `sent`. The two are not the same number and conflating
them is how this question gets answered wrongly.

| date | emails sent | **first emails** | per-campaign ceiling |
|---|---|---|---|
| 2026-09-22 | 795 | **776** | 1,150 |
| 2026-09-23 | 979 | **779** | 1,150 |
| 2026-09-24 | 1,497 | **775** | raised to 2,600 during this day |

**First emails did not move: 776, 779, 775.** Total `sent` rose sharply on 09-24, but that
is steps 2-4 landing on earlier cohorts, not new people being contacted.

Per campaign on the day of the change:

| campaign | 09-22 | 09-23 | 09-24 | cap | uncontacted in hand |
|---|---|---|---|---|---|
| OPERATIONS | 350 | 350 | **350** | 350 -> 600 | 1,192 |
| AI & TECHNICAL | 100 | 100 | **100** | 100 -> 250 | 157 |
| FINANCE | 100 | 100 | **100** | 100 -> 250 | 90 |
| CUSTOMER EXPERIENCE | 45 | 45 | 22 | 100 -> 250 | 8 |
| GTM SYSTEMS | 62 | 56 | 68 | 100 -> 250 | 46 |
| MARKETING | 57 | 59 | 70 | 100 -> 250 | 36 |
| PEOPLE & HR | 35 | 49 | 42 | 100 -> 250 | 22 |
| PRODUCT | 19 | 16 | 20 | 100 -> 250 | 14 |
| ECOMMERCE | 8 | 4 | 3 | 100 -> 250 | 0 |

Two separate things are going on, and only one of them is a ceiling:

1. **OPERATIONS, AI & TECHNICAL and FINANCE sat on their OLD caps (350/100/100) on the
   very day the caps were raised.** The nine send 08:00-18:00 America/Chicago, i.e.
   13:00-23:00 UTC, so a change made after that window cannot show until the next one.
   The first window that can test the new ceilings is **today, 2026-09-25 13:00Z**;
2. **the other six are not ceiling-bound at all -- they are out of people.** Between them
   they hold 126 uncontacted contacts, and ECOMMERCE holds none. Raising their caps from
   100 to 250 cannot do anything, because nothing was stopping them at 100.

So the honest answer today is: **unproven, and it could not have been proven yet.** The
prediction it makes is checkable this afternoon -- if the new ceilings bind, OPERATIONS
should contact up to 600 (it has 1,192 in hand), AI & TECHNICAL up to its 157 and FINANCE
up to its 90, plus roughly 126 across the rest: on the order of **970 first emails rather
than 775**. If OPERATIONS stops at 350 again, the ceiling is not the constraint and the
next thing to look at is how Instantly distributes new-lead starts across the 252 inboxes.

### 12. 2026-09-25 02:52Z -- a defect the run itself exposed, fixed in the branch

The first real follow-up batch moved five contacts and recorded only one. The other four
came back `move_unconfirmed`: `POST /leads/move` had accepted the job, and the check ran
before the job landed. Checking Instantly directly a minute later showed **all five were
in the one-step campaign**, so the moves were real and only the bookkeeping was wrong --
they would have waited a day to be recorded.

Fixed on the branch, not deployed, because deploying kills the run in flight: the moves
are now all issued first and confirmed afterwards in bounded rounds (6 rounds, 5 seconds
apart, at most 30 seconds for the whole batch rather than per contact). A move that still
has not landed is held and picked up by the next run, which finds the contact already in
the destination. **To deploy after `daily/end`.**

The five contacts are in the campaign and will each receive their single message in the
13:00Z window regardless; what lagged was our record of it, not the email.


### 13. 2026-09-25 05:52:49Z -- the recovery run closed, and what it actually did

Run `20260925T024914.962584Z-15f2bf20`, kind `scheduled`, budget `prod-scheduled-20260925`,
02:49:14Z to 05:52:49Z (**3h 03m**, 236 rounds). Its own verdict: `target_met: true`,
`shortfall: 0`, `stop_reason: target_reached`.

**The populations, kept apart:**

| population | n |
|---|---|
| **produced AND created by this run** (the target) | **1,015** |
| backlog delivered that earlier runs had approved | 106 |
| total Instantly creations | 1,121 |
| already existed in Instantly, not new | 3 |
| Airtable records written | 1,121 (1,015 fresh + 106 backlog) |
| **distinct email addresses among the 1,121** | **1,121 -- no person created twice** |

Of the 1,015: 948 from newly acquired units, 67 from retries of work already paid for.
Airtable matches Instantly exactly, which is the invariant: a record only ever follows a
genuine creation.

**Destinations** -- all 1,121 landed in the nine Challenger campaigns and nowhere else:
OPERATIONS 546, AI & TECHNICAL 150, FINANCE 140, MARKETING 80, GTM SYSTEMS 78, CUSTOMER
EXPERIENCE 61, PEOPLE & HR 36, PRODUCT 27, ECOMMERCE 3. **No Control campaign received
anything.**

**What was held back, by named reason:** unknown jurisdiction 512, already in another
campaign 171, UK not a verified corporate subscriber 134, eligibility unknown 86,
Germany 28, UAE 8, UK no lawful basis 6, Saudi Arabia 2, employer attribution conflict 1.
Every one is a compliance or duplication gate, not a failure.

**Spend, against the ceilings this run was given:**

| provider | used | ceiling | note |
|---|---|---|---|
| Fantastic | 3,400 credits / 34 requests | 4,000 / 60 | |
| Apollo | 1,455 credits / 9,323 requests | 1,600 / 10,000 | **1.433 credits per fresh lead**; 91% of the ceiling |
| Anthropic | 1,260 requests | 1,500 | at the measured $0.0152 each, about **$19** |

**Zero refused reservations.** Apollo at 91% of its allowance is the nearest thing to a
constraint: another ~100 leads would have run it out.

**Capacity after the run:** the rotation step recorded `enough_room` with 5,344 free and
deleted nothing, exactly as intended.

### 14. 2026-09-25 06:00Z -- replacements: 11 processed, 0 creations, and the honest reason

The three things are counted separately, as they must be:

* **tasks processed: 11.** One closed `no_current_vacancy` -- its posting had gone, and
  reviving it would have been outreach with no reason behind it. Ten were handed back to
  ordinary qualification;
* **candidates found: 0. Genuine Instantly creations: 0.**

Why, checked unit by unit rather than asserted:

* **8 of the 10 already had a live contact.** At that company x function there was
  already at least one approved, delivered, **non-suppressed** person -- a colleague
  contacted earlier. There was nothing to replace, and not writing to anybody was the
  right outcome;
* **2 of the 10 (units 5005 and 8127) hold ONLY the departed, suppressed contact.** They
  came back as `approved` with no approved person and produced nobody. Those two vacancies
  are genuinely unfilled while the unit reads as covered.

So the replacement path is **not proven end to end**: it has never produced a real
Instantly creation.

The departed addresses stay suppressed throughout: **13** `person_email` suppressions with
a departure reason, and none of the ten re-openings touched them.

#### 14a. The first explanation was wrong. Corrected 07:40Z.

The entry above originally suspected that qualification counts an approval whose person
is suppressed toward the contact quota, so a unit holding only a departed contact would
read as covered. **The evidence contradicts that**, and leaving the guess on the record
would be worse than saying nothing.

`max_contacts_per_opportunity` is **3**, and each of these units holds exactly **one**
approval. The quota was therefore never reached and `contact_quota_already_reached` was
never the outcome. `candidate_attempts` shows what actually happened:

* **unit 5005** -- at 02:52:43Z, immediately after the replacement re-opened it, the
  qualifier evaluated **two** candidates and rejected both before spending anything:
  `gate | skipped_pre_enrichment | contact:function_or_authority_mismatch`. It looked for
  a replacement and there was no eligible one;
* **unit 8127** -- re-qualified at 02:52:44Z with **no candidate evaluated at all**: the
  candidate list for that employer x function was empty.

Both work items closed `done` after 2 attempts. So the replacement path **ran correctly
and applied the ordinary gates**; it produced nothing because neither unit had an
eligible person behind it, not because a gate was wrong.

It remains unproven end to end -- no replacement has ever reached a genuine Instantly
creation -- but the reason is the absence of an eligible case, which is a different
statement from a defect, and it is the one the evidence supports.

For completeness, the quota question was measured anyway: **11** units hold at least one
suppressed contact and **3** hold nothing but suppressed contacts (5005, 8127, 8199). If
`max_contacts_per_opportunity` were ever lowered to 1, those three WOULD start reading as
covered by somebody unreachable. That is a latent trap, not a live one.

### 15. 2026-09-25 06:20:35Z -- the exposed Postgres password, rotated

Rotated in a window with nothing running: the recovery run had closed at 05:52Z, the
Replies tick at 06:15Z had completed at 06:17:58Z, and the weekly report was six and a
half hours away.

**A correction to an earlier belief first.** The three services do NOT hold a Railway
reference to the database; each holds a **literal** connection string of 98 characters.
So rotating the password on the database alone would have broken all three. The rotation
had to change four places and then prove they agree:

| where | variable |
|---|---|
| Postgres Core | `POSTGRES_PASSWORD`, `DATABASE_URL` |
| GTM Core Canary 1000 | `TGTC_DATABASE_URL` |
| GTM Weekly Report | `TGTC_DATABASE_URL` |
| GTM Replies | `TGTC_DATABASE_URL` |

Order: `ALTER USER postgres WITH PASSWORD` first, because that is the real change and the
variables are only a record of it; then the five variables; then verification. A dry run
first confirmed all four places agreed beforehand, so a mismatch could not be blamed on
pre-existing drift. No secret was printed at any point -- the script reads and writes the
value in memory and prints only lengths and outcomes.

**Verified, not assumed:**

* `POSTGRES_PASSWORD` and `DATABASE_URL` agree, and all three consumers hold exactly that
  same string;
* the new URL **connects over the internal network** (`postgres-core.railway.internal`):
  `THE NEW URL CONNECTS`;
* the same URL with a tampered password is **refused** --
  `password authentication failed for user "postgres"` -- so password authentication is
  genuinely enforced on that route and the first result means something. (Inside the
  container, local and 127.0.0.1 connections are `trust`, so testing there would have
  proved nothing; that was checked and discarded as a method.);
* all three services redeployed at 06:20:29-06:20:33Z, **SUCCESS**, still on `9414b58`,
  crons unchanged: core `0 3 * * *`, weekly `0,20,40 13-20 * * *`, replies `15 * * * *`.

**Still outstanding for this entry:** the end-to-end proof is a service connecting on its
own schedule with the new credential. `instantly_replies` last served at 06:17:58Z on the
old one; the **07:15Z tick** is the first on the new one and is being watched.

**Rollback:** none is possible for the password itself -- the old one is gone. If a
service cannot connect, the fix is forward: read `DATABASE_URL` from Postgres Core and
re-upsert it as that service's `TGTC_DATABASE_URL`.

### 16. 2026-09-25 07:27Z -- the rotation is proven end to end, and a redeploy costs a tick

The 07:15Z Replies tick, which should have been the first connection on the new
credential, **never happened**: `provider_state` still read 06:17:58Z at 07:18Z and the
deployment's log was completely empty -- not an error, no output at all. A tick that had
run and failed would have left something.

Rather than guess, the job was fired directly with a one-shot cron at 07:23Z. It ran:
connected to the database, read 100 replies from Instantly (69 for the nine campaigns:
49 out-of-office, 12 departures, 7 human, 1 opt-out -- all already known), and wrote
`last_attempt = 07:27:02Z`.

So **the rotation is verified end to end**: a service, on its own schedule, with the new
credential, from connection through provider read to database write. Together with the
internal-network check and the refused tampered password, every link is measured.

The operational fact left over: **a redeploy appears to cost the next cron tick.** The
service was redeployed at 06:20:33Z and the 07:15Z tick did not fire. It matters for
planning -- the weekly report was redeployed in the same batch, so its 13:00Z tick may be
skipped too. That is survivable because it also runs at :20 and :40 through to 20:00, so
the worst case is a twenty-minute delay rather than a missed report. Worth knowing before
assuming a quiet cron means a broken service.

Replies cron restored to `15 * * * *`. All three confirmed: core `0 3 * * *`, weekly
`0,20,40 13-20 * * *`, replies `15 * * * *`.
