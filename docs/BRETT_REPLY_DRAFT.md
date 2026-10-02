# Draft reply to Brett — NOT SENT

Verified facts only. Anything still open is marked as open.

---

Hi Brett,

Confirmed, and it's worse than a formatting problem. Here's what I've got.

**What happened.** The nine Challenger campaigns don't hold any copy of their own.
Every step body is just `{{rendered_email_N_html}}` plus the signature, and step
1's subject is `{{rendered_subject}}`. When the rebuilt core started creating
leads it never sent those variables, so recipients got an empty subject and a
body with nothing in it but Devan's signature block.

**Scope.** I pulled every message actually sent between Aug 31 and today and
classified them, so these are counts, not estimates:

- 16,875 broken messages to 7,777 distinct recipients
- started Sep 21 at 13:15 UTC, last one went out today at 16:22 UTC
- all 252 sending mailboxes were involved

**It didn't start on the 14th.** There are zero broken sends before Sep 21. The
1,214-message export from the 17th is actually a clean baseline, every one of
those has a real subject and body. The first core-created lead landed Sep 20 at
23:00 UTC and the 20th was a Sunday, so the first bad send was the Monday.

One thing to flag so it doesn't get read as confirmation: the outbox happens to
hold exactly 1,214 blocked rows too. Different population, same number,
coincidence.

**Where it stands.** All nine campaigns are paused and I confirmed the paused
state by reading each one back. Configs were backed up first and nothing in the
sequences or schedules changed. Finance was already paused before I touched
anything, so it stays that way when we restore.

The fix is written and the full suite passes (2,092 tests against real
Postgres). It's in PR #129 and still needs a merge before it deploys, so the
core can't create new leads correctly yet. Campaigns being paused is what's
holding the line until then.

**The part that needs a call from you.** I can rebuild the copy for 5,653 of the
affected leads from their own approved records, and that's running now. The other
1,689 I can't: their job titles fail the copy QA gates we already had
(unsafe characters, trailing qualifiers, over 48 characters). There's no approved
copy for them, so they'd send blank again.

That means I can't cleanly resume any campaign that still holds those leads. The
options are to fix the title data, pull those leads out, or leave the campaigns
paused. I didn't want to pick for you.

Campaigns stay paused until the repair is verified lead by lead and I've sent
myself a test through the real sequence. I'll confirm when that's done.

Luis
