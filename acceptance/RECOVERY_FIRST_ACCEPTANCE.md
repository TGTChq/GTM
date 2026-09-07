# Acceptance of the next pipeline execution

Updated against `c160244` on 2026-09-07. The filename is retained for existing
references. This replaces the older recovery-first instructions and cost estimates.

## Objective and present authorization

The user prefers **new acquisition** for the next execution. The objective remains
at least **1,000 distinct, new, Approved Airtable contacts per production run**, with
continuation above the target while authorized resources remain. Neither an input
batch of 2,000 postings nor a grant of 1,000 calls is that output objective.

Preparing this procedure does not resume production. The current instruction keeps
acquisition paused and billing unchanged, with no unauthorized paid experiment.
Keep maintenance enabled and the Apollo grant at zero until an explicit execution
authorization supplies spending limits and delivery scope. Do not buy credits,
change subscriptions, or infer an unlimited grant from the output target.

## Established state and evidence limits

Both services were confirmed deployed on `c160244` through the Railway deployment
API. The recorded container readback has maintenance on, Fantastic acquisition off,
and `APOLLO_RECOVERY_BUDGET_CALLS=0`. Recheck deployed commit and effective values
before launch; repository defaults are not production evidence.

The retained cohort was reported as **3,595 distinct postings / 3,006 company ×
function opportunities** after the suppression identity fix. Its previous 2,998
opportunity count is superseded. That finite backlog does not establish daily supply.

The calibration `20260906T202534Z-0395cf0a` created zero Approved rows. Its two
verified contacts were withheld on company display identity. Offline replay of the
corrected path is not a production delivery receipt. Do not extend the calibration's
zero to a claim that the system has never created an Approved row in its history.

## Readiness, balance, and spending units

Read remaining credits and their cycle in Apollo's **Plan overview / Credits and
activity** UI; record the timestamp and workspace. The previously shown approximately
2,000 credits are a historical observation, not today's verified balance. HTTP 200
on enrichment proves that request was served; it does not prove a balance.

Do not run `acceptance/apollo_readiness.py` as a free preflight. That legacy script
makes a direct organization enrichment request outside the durable run budget. Its
fixed-cost assertions and instruction to resume acquisition after HTTP 200 are not
launch authorization. Use a useful workload request through the budgeted production
client as the serving check only after the run itself is authorized.

The durable Apollo budget reserves **potentially paid physical request attempts**
before organization enrichment and person matching, including retries. Cache reuse
and [People API Search](https://docs.apollo.io/reference/people-api-search) do not
consume those reservations; the search endpoint is documented as zero-credit and
does not return email addresses or phone numbers.

**Calls, credits, and approved contacts are separate units.**
[People enrichment](https://docs.apollo.io/reference/people-enrichment) pricing
depends on requested data and options. `APOLLO_RECOVERY_BUDGET_CALLS` is a request
ceiling, not an exact credit or dollar ceiling. Reconcile provider usage separately;
do not derive credits from the local counter. If authorization is in credits or
money, establish an applicable upper cost per allowed request and a conservative
request ceiling first, or retain the pause. Never assume one call costs one credit.
Other users of the shared workspace also consume its balance.

There is no supported forecast here of three paid calls per company, 8,400–12,000
calls for the backlog, or a recommended 1,000-call grant. The earlier calibration
counted free search reservations and was interrupted; it cannot price the target.

## Preparation for new acquisition

Prepare one reviewed configuration while maintenance remains on. Variable edits can
cause redeployments: capture the deployment using the final configuration and avoid
concurrent pushes or configuration changes during its execution.

| Setting or control | Requirement before a future authorized run |
|---|---|
| `MAINTENANCE_ONLY` | Keep `1` during preparation; clearing it is the final launch action after configuration and authorization checks. |
| `FANTASTIC_JOBS_ENABLED` | Currently false. New Fantastic jobs require an explicit acquisition allowance and eventual enablement. |
| Fantastic spending controls | Read the effective monthly governor, remaining allowance, `FANTASTIC_JOBS_MAX_JOBS_PER_RUN`, source caps, and runtime/iteration limits. Keep approved ceilings; do not reset the ledger to manufacture allowance. |
| `APOLLO_RECOVERY_BUDGET_ENABLED` | Require `1` for a funded execution. Disabled means this control does not enforce its ceiling. |
| `APOLLO_RECOVERY_BUDGET_ID` | A new, explicitly authorized label. Never reuse spent `calib-2026-09-06-50` or rotate labels automatically. |
| `APOLLO_RECOVERY_BUDGET_CALLS` | An explicitly authorized positive request count. Current value remains `0`. Changing the ceiling alone preserves consumed requests. |
| `PENDING_WORK_ENABLED` | Keep enabled so newly purchased work is durably retained before cursor advancement. |
| `PENDING_WORK_RESUME_MAX_PER_RUN` | Existing `2000` bounds a resumed batch in the top-up loop, not total daily output. Multiple batches can drain in one run. |
| `RUN_APPROVED_TARGET_ENABLED` / `RUN_APPROVED_TARGET` | Recorded effective values are `true` / `1000`; this already activates when maintenance is cleared. |
| `RUN_APPROVED_CONTINUE_AFTER_TARGET` | Recorded effective value is `true`; continuing above 1,000 never overrides budgets, runtime guards, or genuine exhaustion. |
| `ACQUISITION_EXTRA_LANES` | Recorded value is empty. Adding `ats` enables another inventory source, but downstream enrichment can spend Apollo credits. It is optional, not a prerequisite for the new Fantastic cohort. |
| Airtable and Approved Sync | Record intended write scope, approval gates, and both schedules. A pipeline run can write Airtable and the separate sync can later enroll those rows. Maintenance is a different mode. |

New acquisition does **not** mean today's rows are all unseen. Repeated provider
responses and previously completed jobs remain suppressed. Preserve seen IDs,
watermarks, source continuations, company/function suppression, caches and custody.
The existing age and ICP rules remain authoritative.

The ordinary loop can also adopt older pending work after adding newly acquired
work. There is no independent resume-only switch in the configuration audited here:
turning off `PENDING_WORK_ENABLED` also disables custody for new acquisitions.
Do not use that switch to force a fresh-only experiment. Keep attribution separate
and evaluate the newly acquired cohort separately. If exclusive fresh processing is
required, that scheduling requirement must be implemented and verified before launch;
simply enabling acquisition does not provide it.

## Evidence required from the execution

1. Preserve exact run ID, deployed commit, UTC window, selected sources, actual query
   filters, pagination cursors/offsets, physical requests and paid rows.
2. Separate first occurrences, query/source overlap, previously processed jobs,
   genuine repeats, rejected jobs, retained pending work and interruption. A
   duplicate counter alone is not proof of repeated paid provider delivery.
3. Attribute outcomes through posting identity, company × function opportunity,
   distinct contact identity and Airtable delivery receipt. Keep fresh acquisition
   and resumed cohorts separate. If one opportunity contains postings from both,
   report that overlap; do not count its contact twice or infer fresh-only output
   by subtracting aggregate totals.
4. Count output only when a distinct new contact has an actual Approved Airtable
   receipt and passes existing gates. A creation count, FINAL_PASS, cached result,
   posting, or existing row is not independently sufficient proof of new approval.
   For daily output use the union across runs in the stated business timezone.
5. Report contact-search coverage separately from outcome coverage. A disposition
   does not prove Apollo search ran. Separate actual search, cached evidence,
   terminal rejection and work deferred before search; keep unprocessed and
   unreconciled populations explicit.
6. Record each stop reason and consumption unit. A local budget stop is not a
   provider credit refusal. A source failure or iteration limit is not exhausted
   inventory. Confirm completed work is preserved and unfinished work is resumable,
   without recycling previous approvals as new output.
7. Reconcile any authorized downstream enrollment to the same contact identities
   after Approved Sync. Preserve Brett's accepted report format and unknown metrics;
   do not send his report as part of technical acceptance.

## Optional recovery-only execution

The backlog can still be processed without new acquisition. In that mode keep
Fantastic disabled and new acquisition lanes inactive, and measure resumed postings /
distinct opportunities / distinct approved contacts separately. It costs no new
Fantastic rows but may require paid Apollo enrichment. It is an alternative cohort,
not an obligatory prerequisite to the user's preferred new acquisition.

## Completion criterion

Offline regressions establish that particular code defects were corrected and that
the controller can reach and continue above its target on controlled input. They do
not establish live contact coverage or a stable 1,000/day minimum.

Report measured new approvals and explicit remaining losses from the authorized
execution. A single run establishes that run's result; sustained daily capacity
requires observations across daily windows. Preserve missing evidence as unknown.
Neither the historical 18.8% / 25.5% figures nor an approximately 111/day projection
is a supported forecast for the corrected system. A failure to reach the target
does not, by itself, prove that Apollo is the only remaining limitation.
