# Challenger copy incident — closeout

Two different problems ran together through this work and they are separated here on
purpose. **Part 1 is the copy incident**: it has a proven cause, a measured scope, a
deployed fix and verified repairs, and what is left of it is a set of decisions about
people who were already emailed. **Part 2 is capacity and production**: it was never
caused by the copy defect, it is not fixed, and it is the reason volume is zero.

Full working record, step by step: [COPY_INCIDENT_20261002.md](COPY_INCIDENT_20261002.md).

---

# Part 1 — the copy incident

## Cause, proven

The nine Challenger campaigns hold no literal copy. Every step body is
`{{rendered_email_N_html}}` plus `{{accountSignature}}`, and step 1's subject is
`{{rendered_subject}}`. Steps 2–4 have a deliberately empty subject because they are
same-thread follow-ups that inherit step 1's actual sent subject.

`instantly_payload` in the rebuilt core built only the 14 Control-shaped variables
and asserted them against an allow-list containing no `rendered_*` name. So the core
created Challenger leads with none of the five copy fields, and the provider rendered
an empty subject and a body holding nothing but the signature.

Database proof: **0 of 9,053 stored Instantly payloads carried `rendered_subject`.**

## Scope, measured

Every message Instantly actually sent between 2026-08-31 and 2026-10-02, pulled from
its `/emails` log and classified one by one: **24,300 messages**.

| | |
| --- | --- |
| broken messages | **16,875** |
| distinct recipients affected | **7,777** |
| sending mailboxes involved | all 252 |
| onset | **2026-09-21T13:15:59Z** |
| broken sends before 2026-09-21 | **0** |

Two independent indicators — an empty subject, and a body whose copy slot has no
visible text — agree on **100%** of the 24,300. It is a count, not an estimate.

It did **not** start on the 14th. The 1,214-message export from the 17th is a clean
baseline. Separately and coincidentally, the outbox holds exactly 1,214 `blocked`
rows; the matching number means nothing.

## Fix, deployed and live

Live on the Core at commit `8242de24`:

- the copy is built from each lead's own approved record, through the frozen
  `outbound_wave1` renderer
- a Challenger lead missing a subject or any of the four bodies **cannot be created**:
  `instantly_payload` raises, and `instantly_payload_with_copy_state` returns a named
  refusal that blocks the outbox row instead of killing the run
- the refusal blocks the **Airtable row too**, so no CRM record exists for a lead that
  was never created. Observed holding in production on the 10-02 run: the same 10 copy
  refusals on both channels, reason for reason
- the image now carries the renderer and the claims registry, with a guard test against
  the real defect that had kept them out

**Not yet live** — in [TGTChq/GTM#136](https://github.com/TGTChq/GTM/pull/136): the
same check run *before* paid enrichment, so an unrenderable posting costs nothing.

## Repairs, verified by read-back

| | |
| --- | --- |
| leads rebuilt from approved records | **5,653** — read back, 0 mismatch, 0 drift |
| re-rendered with a concrete job title | **363** |
| parked because no approved copy exists | **1,975** |
| of those, repaired and verified | **12** |
| final sweep: in-campaign leads sendable without copy | **0** |
| subjects that were a bare function noun | **0** |

No contact deleted, no sequence restarted, no mass resend, every update verified by
reading the record back.

## Verified for real

Two live emails to an internal address through the real sequence shape: step 1 arrived
with the subject `Documentation Manager` and a personalised body; a second confirmed
bodies 2, 3 and 4. After the brief reactivation window, **125 messages went out and
all 125 carried a real subject and real copy.**

## What is left of the copy incident — decisions, not engineering

Of the **7,777** people affected, by where they are now:

| cohort | count | why a technical fix cannot settle it |
| --- | --- | --- |
| still in a campaign, mid-sequence under a blank thread subject | **5,343** | Instantly builds a thread's subject from what it actually sent, which was nothing. The body is fixed; the thread subject cannot be changed. |
| moved to the hold list | **1,961** | Returning a lead to its campaign restarts its sequence — proved on an internal contact: moving out wipes the step history, moving back clears the last-contact date and the lead would get step 1 again. |
| terminal (338 bounced, 1 completed) | 339 | Nothing to decide. |
| replied | 80 | Excluded from any reactivation. |
| no longer in a campaign or on the hold list | 54 | — |

And the **1,975** parked leads, by whether they were ever emailed:

| | count |
| --- | --- |
| already received a broken email | **1,961** |
| never emailed, title recovered, repaired and verified | **12** |
| never emailed, no recoverable title | **2** |

Cutting the same 1,975 a different way — by whether a title can be recovered at all,
regardless of contact history — gives **1,490 recoverable** and **483** with no job
title in the source to recover without inventing one.

For the parked population, going back to the **original postings** recovered a
concrete title for **1,490 of 1,973** as a literal substring of the employer's own
posting text, validated against a role-noun vocabulary measured from the 6,027
displays that already pass every gate. 1,478 of those repairs are prepared and
deliberately **unapplied**, because applying them changes nothing until there is a
decision about re-contacting people who already received a blank email.

## Ready to switch on, awaiting authorisation

23 contacts are genuinely clean — never emailed, concrete job title, complete copy.
**5 are held back** because the recovered display reads as a department rather than a
person's job (`Promotional Review Operations` ×3, `AI and ML Engineering`,
`Student Health Services`). **18 are proposed**, each routed by its approved
`function_key` through the code's own map:

| campaign | contacts |
| --- | --- |
| OPERATIONS | 7 |
| PEOPLE_HR | 3 |
| AI_TECHNICAL | 2 |
| CUSTOMER_EXPERIENCE | 2 |
| FINANCE | 2 (listed for completeness; its pause is kept) |
| GTM_SYSTEMS | 1 |
| PRODUCT | 1 |

**0 of the 18** route to a campaign other than the one they already sat in. All 18 are
absent from the send record entirely.

**The missing authorisation is one decision:** enrol those contacts and let them
receive step 1. Everything technical is done and nothing has been executed — no
campaign created, no lead moved, nothing unpaused.

---

# Part 2 — capacity and production

Separate from the copy defect, and not fixed by it.

## Why volume is zero

The nightly run needs 2,500 free contact slots in Instantly and had about 2,240. It
created nothing from 2026-09-29 to 2026-10-02.

Three defects found while working on the copy incident, all unrelated to copy:

- the capacity check counted **campaign membership** instead of stored contacts, so it
  would have reported 3,931 free slots when there were 2,237
- rotation scored every **successful** deletion as a failure and gave up after three.
  That is why four consecutive nights produced nothing
- a **deployment** starts a cron service's command regardless of the schedule, so
  merging a fix could begin a run

All three are live at `8242de24`. Rotation then cleared **263** slots with 0 failures,
10 of 10 sampled confirmed gone by id, and 12 historical mis-records reconciled.

## The last run, in full: it produced nothing

Run `20261002T233322.605777Z-b7478fc9`, closed 2026-10-03T00:17:38Z.

| | |
| --- | --- |
| stop reason | `target_not_reached:apollo_request_allowance_insufficient` |
| target / shortfall | 1,000 / **1,000** |
| acquisition blocks attempted | **none** (`blocks: []`) |
| approvals | 42 |
| **confirmed Instantly creations** | **0** |
| Airtable rows written | **0** |
| Apollo requests | **7,407** of 10,000 |
| Apollo credits | **56** of 1,600 |
| Fantastic requests | 0 |

**Exact termination.** `tgtc_core/daily.py` compares the remaining request allowance
against the projected requests for the next acquisition block. With
`10,000 − 7,407 = 2,593` left, the projection exceeded it, so the controller **declined
to start a block at all** and ended with the full shortfall. A pre-spend refusal, not
a failure.

**Where the allowance went.** `people_search` is 99.3% of the requests and costs no
credits, so requests, not credits, are the binding ceiling.

| operation / status | HTTP | n | credits |
| --- | --- | --- | --- |
| `people_search` served | 200 | 6,000 | 0 |
| `people_search` **refused** | **429** | **1,351** | 0 |
| `person_match` served | 200 | 55 | 55 |
| `organization_enrich` served | 200 | 1 | 1 |

All **1,351** refusals are genuine HTTP **429** with `error_class = rate_limited`, a
single distinct receipt shape, and `retry_after: 900.0` on every one.
**Local budget refusals: 0. Other rejections: 0.** Nothing was mislabelled.

Two defects behind that, both fixed in #136 and neither live yet:

1. `_handle_global` raised for the opportunity in hand on `RATE_LIMITED` but recorded
   nothing, so the next opportunity searched and hit the same limit again — 1,351
   times, **18% of the day's allowance** spent on nothing.
   `Outcome.RATE_LIMITED.global_stop` already existed and was read nowhere.
2. the wait was being cut short in two places — `min(retry_after, 900)` in the service
   and `min(parsed_retry, 900.0)` in the adapter. Every one of the 1,351 refusals was
   recorded at exactly 900.0, the ceiling, so Apollo was asking for **at least** that
   and we came back early.

## The 25 pending deliveries — RECOVERED on 2026-10-03

Resolved under authorisation, by `recover-deliveries --withhold 18122 --withhold 18150`
run as a deployment command inside Railway, holding the production run lock.

| | Instantly | Airtable |
| --- | --- | --- |
| delivered | **22** | **22** |
| blocked | **20** | **20** |
| total for the run | **42** | **42** |

* **2 withheld as instructed** — outbox 18122 (`customer support role`) and 18150
  (`operations role`), both recorded as `recovery_hold:withheld_by_operator`.
* **22 created and every one verified by reading the lead back by its provider id**:
  all exist, all in the campaign expected, all five copy fields present, 0 unresolved
  tokens, 0 generic subjects, all `active`, and **0 contacted** — nothing was sent.
* **1 refused by the duplicate guard** — outbox 18120, `crystal@wspartners.com`,
  `not_delivered:instantly_existing_other_campaign`: already in a different campaign,
  so enrolling would have duplicated a person. Reported as unverified rather than
  counted.
* **0 Apollo credits and 0 Apollo requests**: `request_attempts` is unchanged across
  the recovery, because the payload was already stored on the row.
* The nine campaigns stayed PAUSED throughout, and **FINANCE received nothing**.

Distribution of the 22: OPERATIONS 12, GTM_SYSTEMS 4, CUSTOMER_EXPERIENCE 2,
AI_TECHNICAL 2, ECOMMERCE 1, MARKETING_CREATIVE 1.

So the run that closed with **0** confirmed creations now stands at **22 confirmed
Instantly creations and 22 Airtable rows** from its 42 approvals.

### The original diagnosis, for the record

| | |
| --- | --- |
| Instantly rows `pending` | 25, `last_error = campaign_status_2` |
| Airtable rows `pending` | 25, `last_error = awaiting_instantly` |
| attempts / lease | 1 / none |
| `available_at` | 2026-10-03 01:16–01:17Z, already past |

**Why they did not process:** our own rule. `delivery.py` deferred on any campaign
status other than active, and the nine campaigns are paused. The rows were **deferred,
not blocked or failed** — by design they drain once a campaign is reachable. The
Airtable half waits on the Instantly half, which is the CRM invariant working.

**23 of the 25 are eligible, not 25.** All 25 carry the five copy fields with no
unresolved token, `skip_if_in_workspace` set, 25 distinct addresses, attempts 1 and an
expired deferral. They target OPERATIONS 14, GTM_SYSTEMS 4, CUSTOMER_EXPERIENCE 3,
AI_TECHNICAL 2, MARKETING_CREATIVE 1, ECOMMERCE 1 — **none to FINANCE**.

But two subjects are bare function nouns, which is exactly what the 287 parked leads
were parked for: outbox 18122 / approval 9070 `customer support role` (posting
`ISSM / IT Support`) and outbox 18150 / approval 9084 `operations role` (posting
`TELLER/CUSTOMER SERVICE REP`). Both posting titles pass the gates unchanged, so a
title is available, but both read badly as a subject, so they are **held back** with
the candidate recorded — the same treatment as the five department-like displays.

**A gap this exposed:** the production gates do **not** reject a bare function-noun
subject. `role_display_send_safe` checks length, characters, appended qualifiers and
headlines; the buzzword gates read the rendered copy; none objects to `operations
role`. These two rows were created on 2026-10-02, so the pipeline can still produce
such subjects. The 363 re-rendered earlier were a repair, not a gate. Closing it would
change the funnel, so it is flagged here rather than changed.

**A paused campaign accepts leads and sends nothing** — tested on the internal TEST B
campaign on 2026-10-03, not assumed. A genuinely new lead was created into a PAUSED
campaign: the campaign stayed at status 2, `emails_sent` did not change, and the lead
was never contacted (no last-contact timestamp, no sequence step). The probe lead was
then deleted and a re-read returned 404.

**Recovery path, prepared and not run.** `python -m tgtc_core deliver` drains the
existing outbox:

- **no enrichment is repeated** — the complete payload is already in
  `delivery_outbox.payload_json`; the drain makes zero Apollo and zero Fantastic calls
- **no duplicates** — the idempotency key, plus `resolve_membership` on any retry,
  which records an existing lead as `reconciled` instead of creating one, plus
  `skip_if_in_workspace` in the payload itself
- **no new budget** — `cmd_deliver` claims no budget, no day, no run lock and passes no
  window guard; it is delivery only
- the Airtable rows carry their own deferral, so they drain on a **second** pass, not
  in the same sweep

### The one choice the recovery needs

`deliver` drains every due row, so running it as-is creates all 25 — including the two
with a bare function-noun subject. Excluding them is a deliberate act, so neither
option is taken here:

- **deliver 23 and hold 2**: block outbox rows 18122 and 18150 first with a named
  reason, then drain. They stay recoverable by unblocking.
- **deliver all 25**: accept `customer support role` and `operations role` as subject
  lines for those two people.

Delivering into a paused campaign is gated behind `TGTC_DELIVER_INTO_PAUSED_CAMPAIGNS`,
**off by default**, because those 25 leads will send the moment anyone resumes their
campaign. COMPLETED campaigns are deliberately excluded: adding leads to a drained
campaign flips it ACTIVE and it starts sending.

## The 2026-10-03 tick was omitted

The Core cron was paused at 02:41:17Z, 19 minutes before the scheduled 03:00Z minute,
so no run started that day. Verified at 03:02:10Z: `scheduled_executions` holds **0
rows** for 2026-10-03, there are **0** `run_log` entries and **0** provider attempts
after 02:55Z, the run lock is free, and the 25 + 25 pending outbox rows are untouched.
The budget namespace `prod-scheduled-20261003` is unused.

**No recovery was launched.** A recovery must name itself through `TGTC_RUN_RECOVER`
and would spend that day's remaining allowance; that is a decision, not a default.

## Open technical item — generic subjects are accepted by production

**This is NOT the incident above, and it is recorded separately on purpose.** The
incident was *empty* copy: no subject and no body, 16,875 messages, now fixed and
guarded. This is a *quality* defect in copy that is present and complete: the pipeline
can produce a subject line that is a bare function noun, and nothing refuses it.

### What was measured

Two of the 25 rows approved by the 2026-10-02 run carried these subjects:

| outbox | approval | subject | the employer's own posting title |
| --- | --- | --- | --- |
| 18122 | 9070 | `customer support role` | `ISSM / IT Support` |
| 18150 | 9084 | `operations role` | `TELLER/CUSTOMER SERVICE REP` |

Both were **created on 2026-10-02**, after the copy fix was live, so this is current
behaviour and not incident residue. Both are now withheld as
`recovery_hold:withheld_by_operator`, which is a hold on two rows, not a fix.

### Why nothing stops them

`role_display_send_safe` checks length (48 characters), unsafe characters, an appended
qualifier and a posting headline. The buzzword gates read the rendered copy. **None of
them objects to `operations role`.** It is complete, resolved, in-length, character-safe
copy that simply says nothing.

The 363 subjects re-rendered earlier in this incident were a **repair** applied to
records that already existed. They were not a gate, so they do not prevent the next one.

### What the recovery does, and why that is not the fix

`tgtc_core/services/delivery_recovery.py` refuses a bare function noun with
`recovery_hold:subject_is_a_bare_function_noun` — deliberately in the recovery and
**not** in `DeliveryService`. Putting it in the delivery path would reject approvals
that production currently accepts, which changes the funnel: it would convert some
share of daily approvals into blocked rows, and nobody has measured that share or
agreed to lose it.

### What closing it would take

1. measure how many approvals per run currently carry such a subject (the 2 of 25 seen
   here is a single sample, not a rate)
2. decide whether the right outcome is to refuse them, or to re-render them from the
   posting title the way the 363 were
3. the evidence from this incident says re-rendering usually works: for the parked
   population, a concrete title was recoverable as a literal substring of the
   employer's own posting title for **1,490 of 1,973**. Both subjects above have a
   candidate — but `ISSM / IT Support` and `TELLER/CUSTOMER SERVICE REP` read badly,
   which is exactly why this needs a decision rather than an automatic rewrite

Flagged, not changed.

## Open, and needing a decision rather than a fix

- **Capacity: the next tick will NOT produce.** Re-measured after the 22 recoveries,
  and this corrects an earlier claim of mine that there was room for one more tick.

  | | |
  | --- | --- |
  | plan limit | 25,000 stored contacts |
  | stored now | **22,522** (20,541 in campaigns + 1,981 on lists) |
  | free | **2,478** |
  | the tick needs | target 1,000 + reserve 1,500 = **2,500** |
  | **short by** | **22** |
  | rotation candidates that are genuinely eligible | **11** |
  | after rotating all 11 | **still short by 11** |

  The 22 recovered leads consumed exactly the headroom: free slots went 2,500 → 2,478,
  which is 22 below what the tick requires. Of 269 leads in unprotected COMPLETED
  campaigns, 134 have an unfinished sequence and 124 have replied, leaving **11**
  eligible — fewer than the 22 needed. So the next tick stops at
  `instantly_slots_short_by` **before spending anything**. That is a clean refusal, not
  a failure, but it means zero production until a decision is made.

  The levers, all decisions and none of them a fix: the storage add-on; lowering the
  1,500-slot reserve; lowering the 1,000/night target; or releasing parked contacts —
  the hold list alone occupies **1,975** slots, far more than the 22 needed, which is
  why it is the largest lever and also the one explicitly off limits.
- **The Core is fully restored** as of 2026-10-03T04:20Z: original start command (713
  of 713 characters, compared against the saved copy), `cronSchedule` `0 3 * * *`, and
  none of `TGTC_RUN_WINDOW_UTC`, `TGTC_RUN_FORCE`,
  `TGTC_DELIVER_INTO_PAUSED_CAMPAIGNS` or `MAINTENANCE_ONLY` set. Run lock free. The
  next scheduled tick is 2026-10-04 03:00Z.
- **A deploy inside 03:00–05:59Z on an unclaimed day still starts a run.** The hour
  window only refuses starts outside those hours and the day claim only refuses a
  *second* start, so inside the window the first start wins whoever triggered it — and
  it takes the day's claim, making the genuine tick the duplicate. Asserted in
  `test_a_deploy_inside_the_window_on_an_UNCLAIMED_day_DOES_start_a_run`. Closing it
  needs something that distinguishes a cron start from a deploy start; narrowing the
  window to the cron's own minute is the available lever and is a decision about the
  nightly run's start tolerance.
- **The nine campaigns stay paused**, FINANCE on its earlier pause.

## Withdrawn claims

- "~850 leads a night" and "+280 Apollo credits" — unmeasured projections.
- "~2,634 credits saved per run" — unmeasured.
- "Freed 1,688 slots" — **retracted.** 21,069 + 1,688 = 22,757, the original
  `stored_before`. Nothing was freed; the count was wrong by 1,688.
- "Restored 9/9 campaigns" — it was 8 active with FINANCE paused.
- "Merging well before 03:00Z keeps the tick" — no measurement supports it.
- "A deploy inside the window will be declined" — wrong, see above.
