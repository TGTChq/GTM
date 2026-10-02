# Draft reply to Brett — NOT SENT

Verified facts only. Open items are marked as open.

---

Hi Brett,

Confirmed, and it was worse than a formatting problem. Fixed and contained. Here's
where it stands.

**What happened.** The nine Challenger campaigns don't hold any copy of their own.
Every step body is just `{{rendered_email_N_html}}` plus the signature, and step
1's subject is `{{rendered_subject}}`. When the rebuilt core started creating
leads it never sent those variables, so people got an empty subject and a body
with nothing in it but Devan's signature.

**Scope, and how I measured it.** I pulled every message Instantly actually sent
between Aug 31 and Oct 2 straight from its `/emails` log, 24,300 of them, and
classified each one individually. The campaign bodies carry no literal text, so
the first block of a sent body is the copy slot: if it has no visible text, the
recipient got the signature alone. That gives 16,875 broken messages to 7,777
distinct recipients, across all 252 sending mailboxes. I cross-checked it a second
way: every broken message also had an empty subject and every good one didn't,
and the two measures agree on 100% of the 24,300. So it's a count, not an
estimate.

**It didn't start on the 14th.** Zero broken sends before Sep 21. The
1,214-message export from the 17th is actually a clean baseline, every one of
those has a real subject and body. The first core-created lead landed Sep 20 at
23:00 UTC and the 20th was a Sunday, so the first bad send was the Monday.

One thing to flag so it doesn't get read as confirmation: the outbox happens to
hold exactly 1,214 blocked rows too. Different thing entirely, same number.

**Fixed and deployed.** The core now builds the copy from each lead's own approved
record and refuses to create or send a Challenger lead missing a subject or any of
the four bodies. I proved the refusal on five real stored payloads: all three
guards reject them and Instantly is never called at all. Deployed, commit 4efb21e.

Two things worth knowing. The first merge didn't actually deploy, the build failed
because a per-Dockerfile ignore file was keeping the copy renderer out of the
image. And after that I found a real bug in my own fix: it raised inside the
approval transaction, which would have ended the nightly run on the first lead
with a messy job title, about one in four. It now records the lead and blocks it
instead. That one is still waiting on a merge.

**Records repaired.** 5,653 leads rebuilt from their approved records, and I
re-read every one back from Instantly: all five copy fields correct, nothing else
on the lead touched, no contact deleted, no sequence restarted, no mass resend.

**First real sends after turning it back on: 125, all clean.** Steps 1, 2 and 3
went out. Step 1 arrived with a real subject and a body personalised on name,
role and role focus.

**What I could not repair, and you should know about.** For leads that already
got a blank first email, Instantly builds the thread subject from what it
actually sent, which was nothing. So their follow-ups now carry correct copy
under an empty subject. That's 5,630 of the 5,653. The body is fixed, the thread
subject can't be. Those recipients will most likely see "(no subject)".

**Campaigns are all paused right now.** I turned eight back on at 19:45 UTC after
checking every sendable lead, and left Finance paused because someone had paused
it half an hour before I started. At 22:10 UTC all nine were paused again. That
wasn't me, my write log stops at 19:45, and it was aimed at exactly our nine and
nothing else in the workspace. If that was you, fine, they're ready whenever you
want them on. If it wasn't, tell me and I'll dig.

**Three things that need a call from you.**

1,686 of the affected leads I couldn't rebuild. Their job titles fail the copy
quality gates we already had, so there's no approved copy for them. They're in a
hold list, out of every campaign, so they can't send. Parked, not fixed. I tried
two fixes and rejected both on the evidence: the generic fallback produces
subjects like "operations role" and none of our 591 known-good leads reads like
that, and the title resolver only recovered 2 of them.

Related: 650 of the leads I did repair have subjects like "operations role"
already, because that's what the pipeline itself produced for them. Not blank,
but not good either. Worth a decision on the copy.

And the volume one. The nightly run has created nothing for four days straight,
since Sep 29, and that's not the copy fix, it's Instantly storage: it needs 2,500
free contact slots and only had 2,243. Moving those 1,688 leads out of the
campaigns freed that up, so tonight's run can actually proceed. But with the copy
guard discarding 23% of approvals after we've already paid Apollo for them, the
measured ceiling is about 850 created per run, not 1,000. Getting back to 1,000
needs roughly 280 more Apollo credits a night. I didn't spend anything on that.

I'm watching tonight's run and will reconcile creations, copy blocks, Airtable and
spend against it.

Luis
