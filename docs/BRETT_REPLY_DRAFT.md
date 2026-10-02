# Draft reply to Brett — NOT SENT

Verified facts only. Open items are marked as open.

---

Hi Brett,

Confirmed, and it was worse than a formatting problem. It's fixed and contained
now. Here's the short version.

**What happened.** The nine Challenger campaigns don't hold any copy of their own.
Every step body is just `{{rendered_email_N_html}}` plus the signature, and step
1's subject is `{{rendered_subject}}`. When the rebuilt core started creating
leads it never sent those variables, so people got an empty subject and a body
with nothing in it but Devan's signature.

**Scope.** I classified every message we actually sent between Aug 31 and Oct 2,
so these are counts, not estimates:

- 16,875 broken messages to 7,777 different people
- started Sep 21 at 13:15 UTC, last one went out Oct 2 at 16:22 UTC
- all 252 sending mailboxes were involved

**It didn't start on the 14th.** There are zero broken sends before Sep 21. That
1,214-message export from the 17th is actually a clean baseline, every one of
those has a real subject and body. The first core-created lead landed Sep 20 at
23:00 UTC and the 20th was a Sunday, so the first bad send was the Monday.

One thing to flag so it doesn't get read as confirmation: the outbox happens to
hold exactly 1,214 blocked rows too. Different thing entirely, same number.

**What's fixed.** The core now builds the copy from each lead's own approved
record and refuses to create or send a Challenger lead that's missing a subject or
any of the four bodies. Full test suite passes against real Postgres (2,092
tests). It's deployed, commit 4efb21e.

Worth knowing: the first merge didn't actually deploy. The build failed because
there's a per-Dockerfile ignore file that was keeping the copy renderer out of the
image. Second merge fixed it and I added a test so that can't happen again.

**Records repaired.** 5,653 leads rebuilt from their approved records, and I
re-read every one back from Instantly to confirm it: all five copy fields correct,
nothing else on the lead touched, no contact deleted, no sequence restarted, no
mass resend.

**Tested for real.** Two actual emails to my inbox through the live sequence
shape. Step 1 arrived with the subject "Documentation Manager" and a personalised
body, and a second one confirmed bodies 2, 3 and 4 render properly.

**Campaigns are back on.** Eight active, Finance left paused since it was already
paused before I started. I checked every sendable lead in all nine first, and
there are now zero leads in them without copy.

**Two things that need a call from you.**

1,688 of the affected leads I couldn't rebuild. Their job titles fail the copy
quality gates we already had, so there's no approved copy for them and they'd send
blank again. I moved them to a hold list, so they're out of every campaign and
can't send. They're parked, not fixed. Either we clean up the title data and
re-render them, or we write them off.

Related, and this one affects volume: about 28% of new approvals hit those same
title gates. The pipeline now blocks those instead of enrolling them blank. I
think that's the right call, but it does mean fewer leads per run, so you should
know it's happening rather than find it in a number later.

Luis
