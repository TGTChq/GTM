# Draft reply to Brett — NOT SENT

Verified facts only. Open items are marked as open.

---

Hi Brett,

Confirmed, and it was worse than a formatting problem. Fixed and deployed. Here's
where it stands.

**What happened.** The nine Challenger campaigns don't hold any copy of their own.
Every step body is just `{{rendered_email_N_html}}` plus the signature, and step
1's subject is `{{rendered_subject}}`. When the rebuilt core started creating
leads it never sent those variables, so people got an empty subject and a body
with nothing in it but Devan's signature.

**Scope, and how I measured it.** I pulled every message Instantly actually sent
between Aug 31 and Oct 2 straight from its `/emails` log, 24,300 of them, and
classified each one. The campaign bodies carry no literal text, so the first block
of a sent body is the copy slot: if it has no visible text, the person got the
signature alone. That gives **16,875 broken messages to 7,777 different people**,
across all 252 sending mailboxes. I cross-checked it a second way: every broken
message also had an empty subject and every good one didn't, and the two measures
agree on 100% of the 24,300. So it's a count, not an estimate.

**It didn't start on the 14th.** Zero broken sends before Sep 21. That
1,214-message export from the 17th is actually a clean baseline. The first
core-created lead landed Sep 20 at 23:00 UTC and the 20th was a Sunday, so the
first bad send was the Monday. Also worth flagging so it doesn't get read as
confirmation: the outbox happens to hold exactly 1,214 blocked rows too.
Different thing entirely, same number.

**Fixed and deployed** (commit `b6bbc65`). The core now builds the copy from each
lead's own approved record and refuses to create or send a Challenger lead that's
missing a subject or any of the four bodies. I proved the refusal on five real
stored payloads: all three guards reject them and Instantly is never called.

Along the way the first merge didn't actually deploy (the build was keeping the
copy renderer out of the image), and I found four more real defects, two of them
mine:

- my own fix raised an exception inside the approval transaction, which would have
  killed the nightly run on the first messy job title, about one in four
- the capacity check counted campaign membership instead of stored contacts, so it
  would have told the run it had 3,931 free slots when it had 2,237
- rotation had been scoring every successful deletion as a failure and giving up
  after three. That's why the run created nothing for four days straight
- a deploy starts the nightly container whether or not it's the scheduled time, so
  merging a fix could kick off a second run

**Records repaired.** 5,653 leads rebuilt from their approved records, every one
re-read back from Instantly to confirm: all five copy fields correct, nothing else
on the lead touched, no contact deleted, no sequence restarted, no mass resend.
Then another 363 whose subject was a generic "operations role" now carry the real
job title from the posting.

**Tested for real.** Two actual emails to my inbox through the live sequence
shape. Step 1 arrived with the subject "Documentation Manager" and a personalised
body; a second confirmed bodies 2, 3 and 4.

**Campaigns are all paused, and the nightly job is paused too.** I turned eight
back on at 19:45 UTC after checking every sendable lead, then at 22:10 UTC all
nine were paused again — that wasn't me, my write log stops at 19:45. If that was
you, fine. If it wasn't, tell me and I'll dig. 125 emails went out in that window
and I checked every one: all had a real subject and real copy.

**One thing I tested rather than assumed, and it changes the options.** I wanted to
know whether we could pull the affected people out of a campaign and put them back
later without losing where they were in the sequence. I tried it on my own address,
in a paused campaign. Moving a lead out wipes its step history, and moving it back
returns it as a fresh active lead with its last-contact date cleared — so it would
get step 1 again. Taking anyone out is a one-way door. That's why nothing below
proposes putting people back.

**Three things that need a call from you.**

1. **The people who already got a blank first email.** Instantly builds a thread's
subject from what it actually sent, which was nothing. So their follow-ups now
carry correct copy under an empty subject. The body is fixed, the thread subject
can't be, and from the test above we can't quietly move them aside and restore
them later. **5,343** of them are still sitting mid-sequence in the campaigns.
Whether we keep emailing that group at all is your call, and the campaigns stay
off until you've made it.

2. **The 1,975 parked leads — I went back to the original job postings.** For
**1,490** of them there's a real job title sitting inside the posting title that the
quality gates had rejected for its shape: "Senior Product Manager, Ad
Monetization" was refused for the comma, "Customer Success Manager - EMEA" for the
trailing region. Every recovered title is a literal piece of the employer's own
posting text, nothing invented, and all of them pass every gate. The other 483
genuinely have no title in there to recover. But here's the catch, and it's the
reason this isn't just a fix: **1,961 of those 1,975 had already received a broken
email** before I parked them. Only 14 never got anything, and 12 of those have a
recoverable title. I've repaired and verified those 12. The other 1,478 have a
tested repair sitting ready and unapplied, because applying it changes nothing
until you decide about re-contacting people who already got a blank email.

3. **Capacity, which is the real reason volume stopped.** This was never the copy
fix: the run needs 2,500 free contact slots in Instantly and only had about 2,240.
It created nothing from Sep 29 to Oct 2. Rotation has now cleared 263 slots, so
the next run can get through, but it used up essentially the whole safe population
to do it — 11 candidates left. The levers are the storage add-on, the 1,500-slot
reserve, the 1,000/night target, or releasing those parked contacts. All of those
are decisions, not bugs.

**What's ready to switch on, when you say so.** 23 contacts are genuinely clean —
never emailed, real job title, complete copy. 5 of them I'm holding back because
the recovered title reads like a department rather than someone's job ("Promotional
Review Operations", "Student Health Services"), so **18** are in the proposal, split
OPERATIONS 7, PEOPLE_HR 3, AI_TECHNICAL 2, CUSTOMER_EXPERIENCE 2, FINANCE 2,
GTM_SYSTEMS 1, PRODUCT 1. All 18 route to the campaign they were already in, so
nothing gets reassigned. Finance stays paused either way. I've prepared it and
haven't run it.

**Last night's run, for completeness: it produced nothing.** 42 approvals, zero
leads actually created in Instantly, zero rows in Airtable, 56 Apollo credits spent.
17 of the 42 were blocked — 7 on compliance, 10 because the job title can't produce
sendable copy — and those 10 correctly produced neither an Instantly lead nor a CRM
row. It stopped because it ran out of Apollo requests, not credits.

Two corrections to things I said earlier. I'd floated "about 850 leads a night" and
"280 more Apollo credits" — withdraw both, they assumed we pay for contacts the copy
gate then discards and we don't; that refusal is visible from the job posting before
we buy anybody. I've now built and tested that check and it's in review, not live.
And separately, while reading the Apollo spend I found the run had burned 1,351 of
its 10,000 daily requests re-hitting the same rate limit, because a throttle wasn't
being remembered between contacts and because we were cutting short the wait Apollo
asked for. Every one of those 1,351 refusals came back asking us to wait at least
15 minutes and we were ignoring it. Both fixed in the same review, no extra budget.

Luis
