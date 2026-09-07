# Corrections supported by the September 7 trial

Base: `43ee7ba4d6a6a390aea147fb675919590406f8fb` (PR #115).
These changes are local. GitHub refused branch creation through this integration
with HTTP 403 `Resource not accessible by integration`; no new branch, PR or
deployment was created here. An authorized publishing client must apply the patch.

## Observations and limits

Run `20260907T062915Z-f79f4de1` used its 200-attempt Apollo authorization and
stopped on that budget. It found 56 contacts: 28 Airtable creations, 27 withheld
before creation, and one person/employer duplicate. The 27 are part of the 56.
An Airtable creation is not evidence of an Approved status.

The subsequent maintenance scan examined only the first 200 retained rows. It
reported 28 send-safe, 126 missing-email, 24 company holds, 21 without an actionable
final decision, and one unverified Apollo email. That population is not the writer's
27 withheld contacts. Multiple retained files may overlap. Its pretty-printed logs
interleaved; a complete, per-contact reconstruction is not available from those logs.

## Changes

1. Add reviewed display identities for EndeavorB2B / endeavorbusinessmedia.com,
   BS&B Safety Systems / bsbsystems.com, Kai / kai.security, Nash / usenash.com,
   and Zwicker & Associates / zwickerpc.com. Each entry includes its first-party
   or company-owned profile source, review date, and exact identity scope in
   `company_display_overrides.json`. The resolver's general matching rules are
   unchanged. A different incoming domain or LinkedIn identity remains held.
   Contact, email, role, ICP, dedupe and approval checks still apply.
2. Examine all retained enriched rows by default. An explicitly requested cap,
   unreadable artifact, or mapping failure is reported as incomplete. Keep file
   populations separate and label aggregate row occurrences, not unique leads.
   Remove the independent 25-detail cap. Emit each diagnostic record as a
   self-contained JSON log line with run and artifact identity, without contact
   emails, raw contact keys, or signing material.
3. Expose original Airtable writer receipts and reason counts independently of
   recomputation: distinct confirmed Approved keys, missing status evidence, and
   reconciliation of original withholding reasons against the writer count.
   Missing evidence stays unknown. This adds no Airtable request.
4. Refresh the per-run Approved goal after the final delivered batch. A budget or
   provider interruption previously skipped the next loop header and could leave
   the reported approval count stale. Preserve the actual interruption reason.
5. Use source-returned billing totals for source economics. In this run, 328 kept
   rows were wrongly presented as 328 credits; 500 were billed. With 22 attributed
   creations that means 44 creations per 1,000 billed rows, not 67.1. This is an
   observation for an interrupted mixed-cohort run, not a daily forecast.
   The historical per-ID ledger cannot attribute missing paid repeats to title
   segments. Those rates are now unknown, so automatic segment allocation falls
   back to its existing broad allocation instead of learning from biased costs.

## Verification

Final offline gate: **3,477 passed / 1,001 subtests passed**, with sockets and DNS
blocked and an empty environment. Integrity **30 checked / 0 mismatch / 0 absent**;
undefined names **0**; `git diff --check` clean. No live improvement is claimed.

The first nine regressions fail on the base: five false company-display holds,
the scan stopping at 200, and absent completeness/population accounting. The
corrected tests pass. Negative controls retain conflicting domains/LinkedIn
identities and unverified email, unsafe contacts and roles. A production-loop test
with offline boundaries retains three Approved receipts when its final batch
ends in an Apollo-budget interruption. All such counts are fixture results.

The source-yield regression includes 328 kept / 500 billed and a source with
100 billed / zero kept. Missing/disabled outcome recording does not imply zero
yield. Original writer receipts remain independent of a deliberately capped replay.

No paid trial, acquisition, billing change, Airtable write or message is part of
this patch. It does not establish 1,000 Approved/day or release all 27 contacts.
The original 172 duplicate events have no retained response-ID trace here; this
patch fixes the cost denominator, not their unproven root cause.

## Publication and evidence checkpoint

Apply this one mail patch on an isolated branch based on current `origin/main`.
Inspect any intervening changes; do not reset main to the patch base. Run the
repository's offline tests, undefined-name and integrity gates, publish a PR, and
merge only after CI passes. Verify both service deployments at the resulting
commit before using maintenance. No older patch attachment is needed.

Keep production paused while publishing: `MAINTENANCE_ONLY=1`,
`FANTASTIC_JOBS_ENABLED=0`, `APOLLO_RECOVERY_BUDGET_CALLS=0`.
The spent grant `luis-20260907-newjobs-2045-stage1` must never be reset or reused.
Preserve crons `0 3 * * *` and `0 0 * * *`, state and reporting acceptance.

After CI and deployment verification, the existing maintenance path can evaluate
`MAINTENANCE_QUALIFY_RUNS=20260907T062915Z-f79f4de1`. Read the
`send_safe_forensics_summary` original receipt block first, then the individually
identified contact records. Clear the switch and restore any temporary scheduling.
Never edit the start command, inject executable environment values, or bypass the
maintenance dispatcher. A deployment is not evidence that maintenance executed.

Finish the original 27-contact reconciliation before claiming all retentions are
resolved. Additional aliases need their own identity evidence; do not blanket-
release them. This patch requires no other attachment and replaces no prior patch.
