# Instantly replies: out of office, departures, opt-outs

Audited 2026-09-24: Instantly was receiving the replies and the core had never seen one.
`outcome_events` and `suppressions` were both empty, `people.opt_out_status` was NULL for
all 5,347 people, and no webhook was configured. The code that would have recorded them
existed (`services/suppression.apply_outcome_event`) and nothing called it.

This is what now runs.

## The defect that had to be fixed first

`OUTCOME_SUPPRESSING_EVENTS` contained a generic `reply`. Of the 540 most recent replies,
**293 were out-of-office auto-answers**. Wiring replies in without changing that would
have permanently suppressed every contact who happened to be on holiday.

Only an explicit **opt-out**, a **departure**, a **bounce** or a **complaint** ends a
relationship now. An out-of-office and a human reply are recorded and suppress nothing.

## Five labels, because they need five reactions

| label | what happens |
|---|---|
| `out_of_office` | the follow-up **waits**. Nothing is suppressed, nobody else is contacted, and it waits until the date the reply gives when it gives one |
| `no_longer_here` | that address stops being used, and the company × campaign unit gets a queued replacement search |
| `opt_out` | respected immediately, through the same suppression acquisition already consults, so it holds before the next run as well as the next send |
| `human_reply` | queued for a person. It can never become an automatic email |
| `unknown` | review. It triggers nothing |

**The text decides, not Instantly.** Measured on the same 540 replies: Instantly's
`i_status` marked 5 of 293 real out-of-office replies and 18 of 75 real departures, while
labelling 78 unrelated replies as out of office. Its label is stored beside each verdict
as evidence and is never the verdict.

**A covering contact is only taken when the reply names one for the matter** ("please
reach out to X"). A name in passing is not a referral, and the successor is recorded as
evidence for whoever works the replacement rather than acted on — we do not tell somebody
that a colleague referred us when they did not.

## How it runs

Service **GTM Replies** `0f030043-82c2-40a4-bf97-4356652c0155`, built from the same image,
cron `15 * * * *`, restart NEVER. Variables are references to the core service's
credentials; it has no Fantastic or Apollo key, so it cannot spend.

```bash
python -m tgtc_core replies-poll              # read, decide, record, queue
python -m tgtc_core replies-poll --dry-run    # decide everything, write nothing
python -m tgtc_core replies-poll --resume     # continue a first sweep deeper into history
```

* **Exactly once**: keyed by Instantly's own message id, inserted with
  `ON CONFLICT DO NOTHING`. Re-polling a page records nothing new.
* **Newest first**: each poll starts at the top and stops when a page holds replies of
  ours that are all already known. Resuming from the last cursor would walk into history
  and never see what arrived since. A page holding none of ours proves nothing — 483 of
  540 replies belong to other campaigns — so it does not end the sweep.
* **Reading never sends.** What follows is a work item: `reply_followup_when_back`,
  `replace_departed_contact`, `review_reply`. The daily runner does not pick those kinds
  up, so nothing acts on them until a step is built for it.
* **Only the nine Challenger campaigns** are ingested.

## First production ingestion (2026-09-24)

540 replies read, **57 belong to the nine campaigns**: 40 out of office, 10 departures,
1 opt-out, 6 human replies, 0 unknown. 20 came from addresses the core does not know
(leads from before the rebuild) and were recorded without queueing anything.

Written: **11 suppressions** (10 departures, 1 opt-out) and **37 queued items** (28
follow-ups, 8 replacement searches, 1 review). No out-of-office suppressed anybody.
15 of the 40 out-of-office replies carried a usable return date.

Two things the first run exposed and that are now fixed and tested: a return date was
being read against the poll's clock instead of the reply's own (which turned "back
September 22" in a reply from the 19th into September 2027), and a rollover beyond 120
days is now treated as no date at all rather than a follow-up parked for a year.

## What Instantly itself does, measured 2026-09-24

Our database is only half the answer. The nine campaigns run with **`stop_on_reply:
true`**, so **any** reply ends that contact's sequence in Instantly -- including an
out-of-office auto-answer. Checking all 57 ingested replies against the live workspace:

| our verdict | found in the nine | their lead state in Instantly |
|---|---|---|
| out of office | 38 | **all 38 `status 3` (sequence completed)** |
| no longer here | 9 | all 9 `status 3` |
| human reply | 5 | all 5 `status 3` |
| opt-out | 0 | that person's lead is not in the nine campaigns |

Two consequences, and the second corrects what this file used to imply:

* **a departed person does not keep receiving the sequence.** Instantly stopped it the
  moment they replied, and our suppression stops them being approved again.
* **an out-of-office does not "pause" anything: the sequence is already over.** Nothing
  is suppressed, so the person may be approached again, but the remaining steps will
  never fire by themselves. A `reply_followup_when_back` item is therefore a note for a
  person, and acting on it means uploading the contact again -- a new lead slot, with
  the destination's capacity gate in front of it -- not resuming a sequence.

The opt-out is enforced where it governs every send we initiate: our own suppression,
which acquisition, qualification and delivery all consult. Instantly's own block list is
not exposed at the v2 paths tried (`/block-lists`, `/block-list-entries`, `/blocklist`
all answer 404), so it is not part of this.

## Why a return date is not a follow-up, and what now happens instead

Four things were measured against the live workspace on 2026-09-25, because together
they decide what this step can honestly do:

* `stop_on_reply: true` means an out-of-office auto-answer **ends** the sequence. All 75
  contacts who had replied across the nine campaigns sat at lead status 3, and not one
  was still in sequence;
* Instantly exposes **no way to resume a finished lead**: `/leads/{id}/resume` and
  `/leads/{id}/restart` both answer "route not found", and `PATCH /leads/{id}` answers
  200 while silently ignoring `status`;
* `POST /leads/subsequence/move` **does** accept a finished contact and keeps them in
  their own campaign -- but it flips them to status 1, which puts them back in that
  campaign's live sending pool, and nothing puts them back. The full probe, including
  what it cost and what was put back, is in `OOO_SUBSEQUENCE_PROBE.md`;
* so the mechanism is a campaign whose sequence is **one step**, because campaign
  membership is a guarantee that can be checked and a status field is not.

A queued `reply_followup_when_back` item could never, by itself, cause an email -- and 41
of them had been sitting there with nothing looking at them.

`services/followups.py` now decides each one when its return date arrives, bounded by
`TGTC_FOLLOWUPS_PER_RUN` (default 50):

| case | what happens |
|---|---|
| the address is suppressed, or the person opted out | **closed**, and never written to. An out-of-office is not an opt-out, and an opt-out is not a follow-up |
| the approval was revoked, or the vacancy is gone | closed as `no_current_vacancy`: there is no longer a reason to write |
| everything still true, and `TGTC_OOO_FOLLOWUP_CAMPAIGN_ID` names a one-step campaign | the contact is **moved there** -- one further message, not the four they already had. The move is confirmed against Instantly's own record of where the contact is, because `POST /leads/move` answers 200 with a background job that is still `pending`; an unconfirmed move waits and is tried again |
| everything still true, and no such campaign is configured | marked **verified and due**, held for a week, and re-examined -- so a suppression that arrives later still closes it |
| the contact is no longer in Instantly at all | closed as `lead_no_longer_in_instantly`: rotation or their own request removed them, and there is nobody to write to |
| the contact's sequence is still running | **held**, never moved: moving them would stop emails that are already going out, and the follow-up is for somebody whose sequence has already ended |

It suppresses nothing, and it never re-enrols anybody in the four emails they already
received. The destination is refused outright if it is one of the nine Challenger or nine
Control campaigns. What it still needs is the one message itself, which is a copy
decision: until that exists the setting stays unset and every due case is simply held.

## Filling the vacancy a departure leaves

`services/replacements.py`, live since 2026-09-24. Before it drains the backlog, the
daily run hands each queued `replace_departed_contact` back to **ordinary qualification**
for that company x campaign unit, bounded by `TGTC_REPLACEMENTS_PER_RUN` (default 25).

Every gate that ever applied still applies -- current employer, verified work email, the
suppression now holding the departed address, compliance, campaign routing -- so a
replacement is **discovered**, never promoted, and a colleague named in the reply stays
evidence for whoever reads the case. A unit whose posting is gone is not revived: that
would be inventing commercial evidence, so it closes as `no_current_vacancy`.

It is not "fully automated" until an end-to-end case has run in production. The first
one is due on the next scheduled run; 8 are queued.

Rollback: set the service's `cronSchedule` to null (ingestion stops; nothing else reads
those tables), or delete the service. The suppressions it wrote are ordinary suppression
rows and stay valid on their own.
