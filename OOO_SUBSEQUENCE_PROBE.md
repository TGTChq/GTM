# Can one further email be sent inside the Challenger campaign itself?

**Tested live on 2026-09-25, 01:27-01:37Z, against the production Instantly workspace.**
Asked because `POST /api/v2/leads/subsequence/move` would be a better answer than moving
the contact to another campaign: the contact stays where they are and receives one more
message. The probe was run before deploying the follow-up step, on purpose.

## Verdict

**Not adopted.** The route works and accepts a finished contact, but it puts them in a
state there is no way back from, and the part that matters -- that it sends exactly one
step -- could not be proven without copy nobody has written. The one-step campaign
design stays, and stays switched off.

## What was measured

| # | Question | Answer |
|---|---|---|
| 1 | Does the route exist? | Yes. `POST /api/v2/leads/subsequence/move`, body `{subsequence_id, id}`. Both required; it validates the subsequence first (`404 Subsequence not found`). |
| 2 | Do the nine campaigns have subsequences? | No. Zero on all nine, before and after this probe. |
| 3 | What does creating one need? | `parent_campaign`, `name`, `conditions` (an object; `{}` is accepted). It is created at `status: 0` with `sequences: []` -- inert, which is how the probe was kept safe. |
| 4 | **Does it accept a contact whose sequence is finished (status 3)?** | **Yes -- 200.** `subsequence_id` and `timestamp_added_subsequence` were set and `campaign` stayed on the Challenger campaign. |
| 5 | **What happens to the contact's state?** | **Status flipped 3 -> 1.** Instantly then listed them under `FILTER_VAL_ACTIVE` (2,451) and no longer under `FILTER_VAL_COMPLETED` -- i.e. back in the live sending pool of a campaign that sends 1,600/day from 08:00 America/Chicago. |
| 6 | Is that reversible? | **No.** `POST /leads/subsequence/remove` returns 200 and clears `subsequence_id`, but leaves the contact at **status 1**, still in the live campaign. `PATCH /leads/{id}` with `{"status": 3}` returns **200 and silently ignores it**. No `/leads/stop`, `/complete`, `/pause`, `/update`, `/bulk-update` route exists. |
| 7 | Does it schedule only that one step? | **Unproven.** The probe subsequence was deliberately empty and paused, so nothing could send. Proving it needs an approved one-step message and a real send window. |
| 8 | Is the state normal anywhere else? | No. Of the 75 contacts who have replied across the nine campaigns, **74 were status 3** and the only status-1 one was the contact in this probe. |

## Why the verdict is "not adopted"

Question 4 is a yes, and that was the hopeful part. Question 6 is what decides it: every
out-of-office contact put through a subsequence becomes permanently *active* in a
campaign holding four emails, and nothing in the API can put them back. If the
subsequence then fails, is paused, or is deleted, the contact does not return to
"finished" -- they sit in the live pool waiting for step 2. That is exactly the outcome
the follow-up exists to prevent, and it would apply to all 41 queued contacts.

Moving the contact to a campaign whose sequence is one step keeps the guarantee where it
can be checked: **campaign membership**, not a status field. A campaign with one step can
only ever send one email, whatever the contact's status says.

## What this cost, and what was put back

One real contact was used, in OPERATIONS, whose sequence ended on 2026-09-21 after an
out-of-office auto-reply (their follow-up was due 2026-09-22). After the probe they were
at status 1 in the live pool, so:

* they were moved to a new campaign, **`aa271e68-1081-4099-bf33-2ce96759f6cc`** -- status
  `0` (draft), **zero sequence steps, zero sender accounts**, all-days-off schedule.
  Three independent reasons it cannot send anything;
* OPERATIONS' active pool went back to **2,450**, confirmed by re-listing it;
* the probe subsequence was deleted; OPERATIONS has **zero** subsequences again;
* no email was sent at any point, to anyone. Their mail history is still the two messages
  it had before: our step 1 and their out-of-office reply nine seconds later.

That holding campaign is the vehicle `TGTC_OOO_FOLLOWUP_CAMPAIGN_ID` is meant to name. It
stays empty and paused until there is a message to put in it, which is a copy decision,
not an engineering one.

## What the probe changed in the code

`POST /leads/move` answers 200 with a **background job** (`{"type": "move-leads",
"status": "pending"}`), so a 200 means accepted, not done -- and `PATCH /leads/{id}`
proved a 200 can mean nothing happened at all. The follow-up step now reads the contact
back from Instantly and only records a follow-up when Instantly itself says the contact
is in the one-step campaign; an unconfirmed move waits and is retried. It also takes the
*source* campaign from that same live read rather than from our creation receipt, and
closes the case if the contact is no longer in Instantly at all.

## What would have to happen to revisit this

1. one approved message for the out-of-office follow-up;
2. a subsequence carrying only that step, on one campaign;
3. one contact through it in a send window, checking: exactly one email in
   `GET /emails`, and the contact's status afterwards. If the subsequence returns them to
   status 3 on completion, question 6 loses its force and this becomes the better design.

Until step 3 has a measured answer, a 200 from that route is not evidence of anything.
