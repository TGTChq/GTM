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
copy renderer out of the image), and I found three more real defects, two of them
mine:

- my own fix raised an exception inside the approval transaction, which would have
  killed the nightly run on the first messy job title, about one in four
- the capacity check counted campaign membership instead of stored contacts, so it
  would have told the run it had 3,931 free slots when it had 2,237
- rotation had been scoring every successful deletion as a failure and giving up
  after three. That's why the run created nothing for four days straight

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
you, fine. If it wasn't, tell me and I'll dig.

**Three things that need a call from you.**

1. **The people who already got a blank first email.** Instantly builds a thread's
subject from what it actually sent, which was nothing. So their follow-ups now
carry correct copy under an empty subject. That's 5,630 people. The body is fixed,
the thread subject can't be. I'm not restarting sequences or resending to paper
over it — whether we keep emailing that group at all is your call, and the
campaigns stay off until you've made it.

2. **1,974 leads are parked, not fixed.** 1,687 have job titles that fail the copy
quality gates we already had, so there's no approved copy for them; another 287
could only produce a generic subject. They're in a hold list, out of every
campaign, so they can't send. Either we clean up the title data and re-render, or
we write them off.

3. **Capacity, which is the real reason volume stopped.** This was never the copy
fix: the run needs 2,500 free contact slots in Instantly and only had about 2,240.
It created nothing from Sep 29 to Oct 2. Rotation has now cleared 263 slots, which
should let the next run through, but it used up essentially the whole safe
population to do it. The levers are the storage add-on, the 1,500-slot reserve,
the 1,000/night target, or releasing those parked contacts. All of those are
decisions, not bugs.

One correction to something I said earlier: I'd floated "about 850 leads a night"
and "280 more Apollo credits". Withdraw both. They assumed we have to pay for the
23% of contacts the copy gate then discards, and we don't — that refusal is
visible from the job posting before we buy anybody. I've built and tested that
check but not switched it on, because it changes the funnel and I'd rather do that
deliberately than quietly.

Luis
