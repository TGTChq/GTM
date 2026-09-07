# Throughput correction checkpoint — 2026-09-06

Base: `6cd4fef879edee0b130cd9a5749982dc5b88427d`. Work branch:
`fix/recovery-approved-throughput`. This record distinguishes code execution in
offline tests from production evidence. No paid requests, deliveries, messages,
billing changes or production state changes have been performed by this work.

## Acceptance and authority

The user requires at least 1,000 distinct **new Approved Airtable leads per
production run**, and more when authorized resources permit. Recovered postings,
contact candidates, Pending rows and a previous run's approvals do not satisfy it.
The daily distinct union must remain separately reportable. Existing qualification,
dedupe, acquisition pause and spending constraints remain binding. The previous
50-call calibration authorization is exhausted and is not reused.

## Confirmed findings and corrections

1. Production watermark mode set the whole controller to one iteration. It now
   limits acquisition to one window while allowing remaining custody batches.
2. Queue accounting subtracted all adopted rows from an already-shrinking held
   count, stranding the unattempted tail after terminal releases. Availability is
   now read using the actual loader, excluding this run's adopted/suppressed IDs.
3. Domain People API Search consumed the internal paid-call grant even though
   `/mixed_people/api_search` is documented as zero-credit. Organization-ID search
   did not, causing inconsistent budgets for the same endpoint. Both are free in
   the new budget; historical consumed grants remain unchanged.
4. Potentially paid HTTP retries escaped the grant. Reservations now occur before
   each physical organization-enrich/person-match attempt, including validation
   fallback. A person without an ID reserves nothing. Reservations count requests,
   **not actual provider credits or dollars**.
5. Existing daily counting and send-safe counts could stop a run without its own
   1,000 new approved rows. A separate per-run target uses Airtable's returned
   Approved statuses; continuation beyond the target is configurable, default true.
6. Daily retention erased lifetime approved identities after 45 days. Daily display
   retention no longer changes lifetime dedupe or run attribution.
7. Related posting IDs were suppressed even for deferred leads, despite the
   terminal helper already correctly excluding them. Removed the unconditional
   extra suppression. Only delivered leads now extend in-run company/function
   coverage; withheld/failed candidates cannot satisfy an employer opportunity.
8. Send-safe withholding exposed only an aggregate. Named reason counts are now
   retained through every delivery slice, together with actual Approved keys.
9. The person-match cache namespace existed but neither live contact path used it.
   Both now persist/reuse verified matches by exact domain and Apollo person ID,
   with existing TTL/rules invalidation and all downstream gates rerun. Cache domain
   normalization also wrongly rejected legitimate hosts such as `fedex.com` because
   they contain `x.com`; it now excludes exact social hosts and their subdomains.
10. A corrupt Apollo budget ledger silently restarted consumed at zero. It now
    refuses paid requests and preserves the unreadable file for reconciliation.
11. The hiring-manager entry point reset its per-run match/cascade allowances on
    every orchestrator batch. RealEnrichmentStage now initializes these once and
    carries them across batches. A two-batch execution with allowance one permits
    only one match. The durable recovery grant also continues across run IDs.
12. A checkpoint keyed only by employer returned the first posting's outcome for
    a different posting in a later batch. Reproduced through both the hiring-manager
    entry point and RealEnrichmentStage: two inputs, only one processing, wrong ID
    returned. Workload fingerprints now bind checkpoint outcomes to exact posting
    evidence and gate settings. Earlier workloads remain available for exact resume.
    Legacy entries without an input fingerprint are preserved but cannot establish
    safe outcome reuse; independently verified person-cache evidence still applies.
13. Every batch overwrote the run's posting input and same-day enrichment output.
    Stable input-addressed batch directories now retain qualification and enrichment
    artifacts. The historical `enrichment/postings.json` reader path holds the
    distinct input union, atomically persisted before enrichment. Two batches and a
    fresh stage instance preserve both postings and reuse the original workload.
14. Corrupt enrichment checkpoints silently restarted, and failed writes allowed
    more paid work. Reproduced with a truncated file and failed atomic replacement.
    Both now stop the caller; existing/staged evidence is preserved. Successful
    checkpoints flush/fsync before replacement. No next company runs after failure.
15. Maintenance compared all enrichment inputs with new acquisitions, so a valid
    recovered batch failed reconciliation. It now reconciles flagged recovered
    postings against the emitted adoption count, and new inputs against new capture
    separately. A deliberately wrong adoption count still fails.
16. Small checkpoint/final-file populations were summed by forensics, although the
    existing large-file path already refused this. Reproduced one result counted
    twice. Parsed evidence now remains per file with explicit aggregation status;
    multiple populations without proven disjointness cannot produce a run total.
17. Forensics read `hunter_status`, `email`, and `primary_reason`, but the actual
    hiring-manager rows emit `hunter_email_status`, `hiring_manager_email`, and
    `_final_primary_reason`. A recorded valid Hunter result reproduced as absent.
    Fixed the field mapping and kept aliases for older artifacts. Consequently the
    earlier claim that no Hunter opinion ran based on absent `hunter_status` is
    **not established**. Re-read original artifacts before drawing that conclusion.
18. The delivery boundary committed every enrichment FINAL_PASS to posting
    suppression, including failed/withheld/dry Airtable outputs. Reproduced three
    failed deliveries causing all three postings to leave custody. Both production
    paths now require an evidenced delivered key for FINAL_PASS completion;
    business REJECT remains terminal, and related posting IDs follow the same rule.
19. `persisted_lead_keys` can include an existing row whose repair failed. The
    delivery adapter treated that failed repair as delivered despite carrying its
    key in `failed_lead_keys`. Failed keys now take precedence and cannot create
    a local delivery receipt or terminate recovery.
20. Weekly reporting treated recovered review and new capture as the same cohort.
    A mixed run reproduced a plausible but invalid 50% review rate. Recovery
    counts now persist in the ledger, including artifact backfill, and the new
    capture cohort is explicitly separate whenever recovery is recorded. Both
    measured totals remain available; an unsupported percentage/loss is withheld.
21. Zero acquisition was described as zero pipeline input even when recovered
    postings were reviewed. That diagnosis now checks observed downstream work.
    The report also inferred provider requests from every non-budget stop label;
    it now requires an emitted request counter and preserves missing activity.
22. The full-week stakeholder heading omitted actual boundaries/timezone, and
    missing-metric actions leaked internal field paths into Brett's text. The
    agreed Period line now names both boundaries and their local zones. Plain
    stakeholder actions are separate from internal remedies and evidence. Weekly
    window attribution and the existing counting units are unchanged.
23. Recovery/reporting integration was executed locally through the real
    orchestrator loop with fake enrichment/delivery boundaries: 7 resumed in three
    batches, 0 new captured, 7 reviewed. The emitted ledger retains the recovery
    count. Removing heavy artifacts in an isolated copy yields identical runs,
    stakeholder text, metric values, completeness, units and cohorts. This is
    locally executed acceptance, **not a new production-volume A/B result**.
24. The report still subtracted 1,048 contacts minus 781 creations when any
    delivery reason was present. Existing tests explicitly expected the invalid
    267 loss. A skip reason does not prove cohort linkage. Non-nested pairs now
    produce no subtraction or conversion rate. Actual recorded delivery outcomes
    remain actionable: the 900 unaccounted submitted rows are a separate
    reconciliation finding. Updated tests preserve that finding and explicitly
    forbid the invented 267 loss/rate. The affected reporting suite passes 143
    tests before the final full gate.

Provider references checked:
- https://docs.apollo.io/reference/people-api-search
- https://docs.apollo.io/docs/api-pricing

## Verification so far

Focused suite before findings 7–8: **70 passed**. Real orchestrator loop with offline
provider/delivery fakes demonstrates 7 owed / batch 3 / all 7 resumed in watermark
mode, no duplicate adoption; 1,000 earlier-run approvals do not satisfy this run;
and 1,200 available rows can pass through a 1,000 target when continuation is on.
These are machinery tests, **not 1,200 live approved leads**.

After findings 7–10, an affected suite passed **65 tests** before the final cache
normalization/coverage tests were added. A strict-path contact replay across a cache
reload made **one paid-boundary mock call for two processings**, with ContactGate
executed twice. Wrong domains, unverified emails and expired cache entries miss.

The first full test session was stopped by automatic approval review over a possible
Apollo request. There is no evidence of an actual provider charge from that session;
the original socket blocker was active. Strengthened `ci_no_network` with Python
audit hooks covering DNS as well as sockets (not removable by replacing a socket
function), verified the blocker directly, then restarted tests in an empty environment
without inherited provider credentials. The isolated run found two old source-string
assertions pinning the removed queue arithmetic. Replaced them with actual loop
execution proving no recapture, no repeated adoption and no adoption of in-flight
work owned by this same run. Acceptance behavior is retained.

## Release verification

**Latest final gate, after all listed corrections: 3,374 passed / 1,001 subtests
passed in 95.95 seconds. Integrity: 27 checked / 0 mismatch / 0 absent. Undefined
names: 0. Diff whitespace check: clean.** The command runs with an empty environment
and `ci_no_network`, blocking external DNS/sockets. No live provider calls, Airtable
writes, Instantly imports, messages or billing changes were made.

Implementation commits are `72ec723` and `0f3f07f`, following base `6cd4fef`.
GitHub status re-read after the final changes again shows both Railway services
successful for the **base** commit `6cd4fef`, not for these local corrections.
Publication remains blocked by API 403; production acceptance and the 1,000-new-
approved-lead target remain unfinished. No new paid test is required or authorized
by this checkpoint. The user's reported remaining Apollo balance was not re-read.

The first complete isolated gate passed **3,358 tests / 1,001 subtests** in 93.54s.
Subsequent focused integration after the final missing-status and shared-batch-budget
corrections passed **33 tests**. Final full gate: **3,360 passed / 1,001 subtests
passed**, 91.77 seconds. Integrity **27 checked / 0 mismatch / 0 absent**; full tracked
Python undefined-name gate **0**; `git diff --check` clean.

The independent follow-up (findings 12–17) passed **3,367 tests / 1,001 subtests**
in 93.61s, with integrity 27/0/0 and zero undefined names. Fresh post-gate review
then demonstrated findings 18–19; both were reproduced before correction and
their affected delivery/recovery suite passed **26 tests**. A final release gate
is required after these last corrections; the 3,367 result predates them.
The next full gate found one obsolete success fixture: it asserted that FINAL_PASS
released custody while its delivery stub returned zero entered and no receipts.
The fixture now models actual delivery acknowledgements and supplies contact keys;
the requirement that completed work leaves custody is unchanged. The focused
custody/throughput suite passed 28 tests after this fixture correction.

Startup configuration executed with acquisition paused, maintenance on, grant zero
and the existing top-up target: `run_target=True, minimum=1000,
continue_after_minimum=True, acquisition=False, maintenance=True, apollo_grant=0`.
Existing top-up deployments default to the new run target on upgrade; an explicit
`RUN_APPROVED_TARGET_ENABLED=0` retains legacy mode. No budget is increased by this.
Missing returned Airtable status is explicitly marked unknown/partial rather than
being counted as a measured Pending row or an approval.

## Remaining work

- Publish the fixes and verify CI/deployment through supported access.
- Inspect the two original withheld calibration rows and reproduce their exact
  gate reasons. Their original per-lead files are not in this checkout.
- Execute a newly authorized bounded production recovery only after access,
  remaining provider quota and a numerical grant are established.

## Publication checkpoint

Implementation commit: `72ec72301f38457c3673944b96d53c899e195d81` on local branch
`fix/recovery-approved-throughput`. Both final release gates and the source review
completed before publication.

`git push` could not authenticate in this environment (`could not read Username`).
The connected GitHub app's supported create-tree publication was then rejected by
its approval step: `user rejected MCP tool call`; no further reason was supplied.
The user subsequently authorized continuing publication. The same supported GitHub
create-tree action then returned an API **403 `Resource not accessible by
integration`**, establishing a repository write-access blocker rather than missing
user authorization. No alternate write route was used to bypass it. No branch, PR
or deployed fix is claimed. The code and checkpoint are exported as a reviewable
patch archive.

Remaining production work is **unfinished**. The user connected Railway during this
turn, but callable Railway methods have not appeared in this execution's registry.
Use that already-connected plugin when available; do not ask to reconnect. Publication
needs repository write access for the connected GitHub integration. User approval
for this release is already present; do not ask for the same approval again.

## Historical access boundary before the latest retry

GitHub repository access works. **The user connected Railway successfully during
this turn.** The current executable tool registry has not yet exposed any Railway
methods or skills; no supported CLI credentials are present either. This is a tool
availability issue after connection, not a request to reconnect the account. Production
commit, active flags, budget, and volume have not been independently re-read.
Do not turn the prior handoff's deployment claim into a fresh verification.
Reported prior state: cron `0 3 * * *`, acquisition paused, maintenance enabled,
Apollo grant zero. Preserve it pending a supported production connection.

GitHub's current commit statuses independently confirm both GTM and Approved Sync
reported successful deployment of `6cd4fef`. Status links do not expose configuration
or prove a pipeline run succeeded. Continue with the connected Railway plugin as
soon as its callable methods appear; do not ask the user to install/connect again.

## Concrete activation and production acceptance (not executed)

1. Inspect both services' active commit, cron, start command and effective flags;
   obtain/back up the original `20260906T202534Z-0395cf0a` per-lead and budget files.
   Explain the two withheld verified contacts from their actual gate facts first.
2. Deploy this reviewed branch after the release gate. Leave acquisition paused and
   maintenance enabled until the documented recovery/budget readiness checks pass.
3. Set `RUN_APPROVED_TARGET_ENABLED=1`, `RUN_APPROVED_TARGET=1000`,
   `RUN_APPROVED_CONTINUE_AFTER_TARGET=1`. These are output controls, not spending
   authorization. The legacy daily target cannot satisfy a new run. Verify the
   existing `APOLLO_CACHE_ENABLED` and durable cache path before claiming reuse live.
4. Recovery acceptance needs a fresh explicitly authorized numerical request/credit
   grant, current endpoint billing terms and sufficient workspace quota. Keep
   `APOLLO_RECOVERY_BUDGET_ENABLED=1`; never reset or refund the old consumed grant.
   Clear maintenance only for the authorized execution; keep acquisition and test
   delivery disabled for that bounded acceptance. Restore maintenance afterwards.
5. Production resumption follows existing authorization. Trace distinct Approved
   Airtable keys to this run and then Approved Sync imports; count each identity
   once. Measure repeat days including the weekend. Do not count prior approvals
   or reserve drawdown as this run's newly approved output. If the target is short,
   report actual budget/time/eligible-work/gate outcomes rather than a fabricated
   lower capacity ceiling.

## Latest authorized retry: Railway accessible, GitHub write still forbidden

The user's instruction "Intenta de nuevo, deberías de poder" triggered one retry
of the supported GitHub create-tree operation for the exact 35-file release
`41620be`. It again returned API 403 `Resource not accessible by integration`.
No alternate write mechanism was attempted. The installation-list response was
empty; this alone does not diagnose which organization/app permission is missing.
The concrete dependency is write access to TGTChq/GTM for this connected integration,
not renewed permission from the user to publish.

Railway is now callable. Original API responses establish:

- GTM deployment `35545a59-50e5-4758-810d-74b5e32195c0`, commit `6cd4fef`, SUCCESS;
  cron `0 3 * * *`, production volume mounted at `/app/data/state`.
- Approved Sync deployment `b1a0ef60-2c61-4ad4-a4d5-a628ee67c187`, SUCCESS;
  cron `0 0 * * *`.
- OAuth withholds variable values. A variable name is not evidence of its setting.
  No variables, schedule, deployment, delivery or billing were changed by the retry.
- The current GTM deployment has no returned runtime logs. The historical calibration
  deployment remains readable after removal: 146 entries, nine complete JSON ledger
  records, all nine IDs distinct. Original response preserved in
  `evidence/railway_calibration_20260906.json`; access responses in
  `evidence/access_retry_20260906.json`.

### Contradictory original execution evidence

The calibration preflight explicitly says `airtable=write` and autoapproval ON.
The statement that the 50-counter calibration had external delivery disabled is
withdrawn: zero creations resulted from 24 `no_contact` and two `send_safe_withheld`
outcomes. No corresponding per-lead gate reasons were logged, so the original two
withheld contacts remain unexplained pending their files. This retry made no new
provider or delivery request. The old internal 50-counter is still not a measurement
of billed Apollo credits. The old reported 100% contact rate is still invalid.
The original run also reports `topup:max_iterations_guard`, despite the in-stage
budget stop; neither label alone establishes the underlying provider balance.

### Reporting recheck from recovered production records

Using the already tested release renderer with network access disabled:
- Last completed period Aug 28 00:00 PDT–Sep 04 00:00 PDT includes three runs.
- Current partial period Sep 04 00:00 PDT–Sep 06 13:28 PDT includes six runs,
  including all three recovery/calibration attempts on Sep 06.
- Current totals: 6,431 captured, 1,050 contacts, 781 Airtable creations (not an
  approved-contact count). Review and qualification are partial. Instantly is
  unavailable in these ledger records; the old 769 count is not reused across
  an extended observation window without its source response.
- Completed-week contacts are measured zero; captured is partial and review /
  qualified unavailable. No synthetic historical numbers were added.
- The 900 unaccounted delivery submissions remain a reconciliation issue; no
  1,048-minus-781 conversion calculation appears.

`evidence/railway_ledger_recheck_20260906.json` contains the full report documents,
per-run census and provenance. This newly accessible evidence improves verification
of the report against production records. It does not replace the still-pending
heavy-artifact comparison or demonstrate 1,000 new approved leads per run.

No runtime code changed during this retry; the release remains covered by the
3,374-test / 1,001-subtest gate. Only access evidence and the progress record changed.
Release/deployment and original per-lead volume access remain unfinished.
