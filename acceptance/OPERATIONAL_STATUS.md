# Operational status of each capability

**Current procedure:** [acceptance of the next execution](RECOVERY_FIRST_ACCEPTANCE.md).
The user now prefers new acquisition for the next authorized run. This preference
does not change the recorded pause, grant or billing. Both service deployments were
rechecked at `c160244`; the effective-value table below is the dated container
snapshot, not a fresh read of every variable.

**Rebuilt 2026-09-07 against deployed `7e91cb4`.** The previous version predated the
throughput release, the full-flow release and the suppression-identity fix, and was
therefore describing a system that no longer existed.

Read back from the running services where the container prints it. A capability that
is implemented but disabled, unfunded or unreachable is **not** operational and is not
counted as improving production.

## How to read the evidence column

The Railway OAuth path returns variable NAMES but withholds their VALUES, so for most
settings "what production is set to" is not directly readable through supported
access. This table therefore separates three very different things, and does not
present the third as the first:

* **container** — the deployed container printed it during a maintenance pass. This is
  evidence.
* **not readable** — the variable exists on the service; its value is withheld by
  OAuth. Recorded intent only.
* **code default** — the repository default. **This is not production.** Local defaults
  and production differ on at least `MAINTENANCE_ONLY`, which reads `False` in the
  repo and is `1` in production.

## Verified from the container

| fact | value | source |
|---|---|---|
| Deployed commit, both services | `7e91cb4` | Railway deployment API |
| GTM cron | `0 3 * * *` | Railway API |
| Approved Sync cron | `0 0 * * *` | Railway API |
| Paid acquisition | `FANTASTIC_JOBS_ENABLED=False` | container |
| Maintenance mode | delegates to `run_maintenance`; **no run directory, lane runner, engine or delivery manager is created** | container |
| Package integrity | `checked=28 mismatch=0 absent=0` | container |
| ATS board registry | `145 from ats_board_registry (145 tracked) err=none` | container |
| Provider credentials present | Apollo, Hunter, Fantastic, RapidAPI, Airtable | container preflight |

Credentials being **present** is not the same as a capability being **funded** or
**enabled**. Apollo's key is present and the recovery grant is zero.

## Capability status — now container-verified

Effective values printed by the deployed container 2026-09-07T04:55Z. These are
production, not repository defaults.

| capability | effective value | state |
|---|---|---|
| Paid acquisition (Fantastic) | `FANTASTIC_JOBS_ENABLED=false` | **PAUSED** |
| Paid enrichment budget | `APOLLO_RECOVERY_BUDGET_CALLS=0` | **UNFUNDED**; the `calib-2026-09-06-50` grant is spent and not reused |
| Overall paid-match ceiling | `APOLLO_MAX_PERSON_MATCH_CALLS_PER_RUN=0` | off by design (0 = no ceiling here) |
| Custody | `PENDING_WORK_ENABLED`, batch `2000` | **ON**; 3,595 distinct postings, resumable |
| **Per-run approved target** | `RUN_APPROVED_TARGET_ENABLED=true`, `RUN_APPROVED_TARGET=1000`, `CONTINUE_AFTER_TARGET=true` | **ACTIVE** |
| **Apollo person cache** | `APOLLO_CACHE_ENABLED=true` | **ON** |
| Daily target + rolling reserve | `DAILY_APPROVED_TARGET_ENABLED=false`, `APPROVED_RESERVE_FLOOR=0` | **NOT ACTIVE** |
| Legacy send-safe target | `NET_NEW_SEND_SAFE_TARGET=1000` | active; this is what self-enabled the run target on upgrade |
| Window slice cursor | `FANTASTIC_WINDOW_SLICING_ENABLED=true` | ON in code, **never exercised in production** — acquisition is paused |
| Direct ATS boards (145) | `ATS_DIRECT_ACQUISITION_ENABLED=true`, `ACQUISITION_EXTRA_LANES=""` | **NOT ACTIVE**; registry loads 145 cleanly, the lane is never built |
| Functional discovery | `false` | **NOT OPERATIONAL** |
| Historical recovery | `false`, rows `0` | **NOT OPERATIONAL** |
| Send-safe auto-approval | `FANTASTIC_AUTO_APPROVE_SEND_SAFE=true` | **ON** |
| Send-safe-only writing | `AIRTABLE_WRITE_SEND_SAFE_ONLY=true` | **ON** — why 2 verified contacts produced 0 rows |
| Company × function suppression | `true` | **ON** |
| Account-level suppression | `AIRTABLE_SUPPRESS_ACCOUNT_LEVEL=false` | **OFF** — confirms the report defect that once blamed it |
| Enrollment person-employer uniqueness | `true` | **ON**; does not cap an employer |
| **Second email opinion** | `VERIFY_WITH_HUNTER=false` | **OFF on GTM** |
| Alternate-contact cascade | `true` | ON, but unreachable while enrichment is unfunded |
| Org-ID zero-people fallback | `true` | ON, but unreachable while enrichment is unfunded |

### Two claims this readback CORRECTED

The previous version of this file marked both "not readable" and guessed conservatively.
The container disagreed:

* **Per-run approved target is ACTIVE, not inactive.** `RUN_APPROVED_TARGET_ENABLED`
  defaults from `NET_NEW_SEND_SAFE_TARGET > 0`, which is 1000 in production, so the
  upgrade switched it on. It is harmless today only because maintenance mode stops the
  pipeline loop ever running — but it would take effect the moment maintenance is
  cleared, and that should be a decision rather than a surprise.
* **The Apollo person cache is ON, not unverified.** Verified-match reuse is live.

### One earlier finding now established rather than inferred

`VERIFY_WITH_HUNTER=false` on GTM. The claim that no second opinion ran was previously
made from ABSENT `hunter_status` fields, and the full-flow audit correctly downgraded
it to "not established" after finding the forensics had been reading the wrong field
name. The flag itself now settles it: on GTM there is no second opinion at all, not
even the deliverability reroute.

## What this means for lead volume today

Maintenance stops the pipeline loop. The Apollo run grant is zero. New Fantastic
acquisition is paused; the additional inventory lanes below are inactive or
unbudgeted. These are separate conditions: acquiring more jobs is not a prerequisite
to enriching retained work, and retained work may reuse cached provider evidence.

**The cited 50-reservation calibration created 0 Approved rows.** No live execution
reported here demonstrates the corrected release's output against 1,000/day. This
does not establish zero Approved rows across the system's entire history.

## The gap that was here, and is now closed

An earlier version of this section said `RUN_APPROVED_TARGET_ENABLED`,
`APOLLO_CACHE_ENABLED` and several others were "not readable", because the Railway
OAuth path returns variable names and withholds their values -- and proposed extending
the container printout as the free next step.

That was done (`7e91cb4`): the maintenance pass now prints the effective value of every
capability flag, and the table above is sourced from it. Values only, with the
credential guard on the NAME rather than on a curated list, so a careless addition
cannot leak a key into a log.

**What remains genuinely unreadable is nothing in this table.** The remaining
unknowns are not configuration:

* the current Apollo credit balance -- read it in the workspace's Plan overview /
  Credits and activity UI and record its timestamp. A paid probe is not necessary
  to read the displayed balance, and HTTP 200 would not establish that balance;
* whether any of the corrected paths raises live yield, which needs a funded run.

Both are recorded in `PROGRESS.md` as external blockers with their concrete missing
action, not as properties of the code.

The configured Apollo grant counts potentially paid physical requests, not exact
provider credits. The updated acceptance procedure supersedes historical grant
estimates and the old automatic-resumption instruction in the readiness script.
