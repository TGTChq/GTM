# Operational status of each capability

**Rebuilt 2026-09-07 against deployed `a71159a`.** The previous version predated the
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
| Deployed commit, both services | `a71159a` | Railway deployment API |
| GTM cron | `0 3 * * *` | Railway API |
| Approved Sync cron | `0 0 * * *` | Railway API |
| Paid acquisition | `FANTASTIC_JOBS_ENABLED=False` | container |
| Maintenance mode | delegates to `run_maintenance`; **no run directory, lane runner, engine or delivery manager is created** | container |
| Package integrity | `checked=28 mismatch=0 absent=0` | container |
| ATS board registry | `145 from ats_board_registry (145 tracked) err=none` | container |
| Provider credentials present | Apollo, Hunter, Fantastic, RapidAPI, Airtable | container preflight |

Credentials being **present** is not the same as a capability being **funded** or
**enabled**. Apollo's key is present and the recovery grant is zero.

## Capability status

| capability | state | why |
|---|---|---|
| Paid acquisition (Fantastic) | **PAUSED** | `FANTASTIC_JOBS_ENABLED=False`, verified in the container |
| Paid enrichment (Apollo) | **UNFUNDED** | `APOLLO_RECOVERY_BUDGET_CALLS=0`; an unset grant is zero, not unlimited. The 50-reservation grant `calib-2026-09-06-50` is spent and is not reused |
| Custody of paid-for work | **ON, exercised** | 3,595 distinct postings, `resumable: true`, `unidentifiable_employer: 0` |
| Sep 6 recovery | **DONE** | 226/226/226, with `new_capture_agrees` and `recovery_agrees` reconciling separately |
| Reporting A/B | **PASSING** | `ACCEPTED: True` on production files, nine ledger entries |
| Employer identity (enrichment) | **FIXED, deployed** | shared ATS hosts rejected as employer identity |
| Employer identity (Airtable suppression) | **FIXED, deployed** | `b4d1796`; measured +24 companies / +8 opportunities on custody |
| Send-safe auto-approval | **ON** | container: `send_safe_auto_approve=ON` |
| Per-run approved target | **NOT ACTIVE** | `RUN_APPROVED_TARGET_ENABLED` defaults from `NET_NEW_SEND_SAFE_TARGET > 0`; production value not readable. Irrelevant while maintenance mode is on, because the pipeline loop never runs |
| Daily approved target + reserve | **NOT ACTIVE** | `DAILY_APPROVED_TARGET_ENABLED` default False |
| Window slice cursor | **ON in code, NEVER EXERCISED in production** | acquisition is paused, so it has run only offline and against 0-credit count probes |
| Apollo person cache | **NOT VERIFIED LIVE** | `APOLLO_CACHE_ENABLED` default False; production value not readable. Do not claim reuse until a run shows it |
| Direct ATS boards (145) | **NOT ACTIVE** | registry loads cleanly, but the lane is built only when `"ats"` is in `--lanes` and the start command passes `--lanes fantastic`. `ACQUISITION_EXTRA_LANES` now makes this one authorized variable instead of a start-command change — it is unset |
| Functional discovery | **NOT OPERATIONAL** | flag off; live incremental yield unmeasured |
| Historical recovery (6m) | **NOT OPERATIONAL** | needs a flag *and* a row budget; neither granted |
| Wellfound / Y Combinator | **ENABLED, contributed nothing** on 09-06 | reported `already_drained_this_window` on offset-era flags |
| Weekly report → Slack | **ON** | start command carries `--slack --if-due friday` |

## What this means for lead volume today

Nothing downstream of acquisition can produce approved leads: paid acquisition is
paused and paid enrichment is unfunded. Every capability that could add inventory —
the 145 ATS boards, functional discovery, historical recovery — is switched off or
unbudgeted.

**Measured production output is 0 approved leads.** None of the capabilities above is
claimed to have produced any.

## The honest gap in this table

`RUN_APPROVED_TARGET_ENABLED`, `APOLLO_CACHE_ENABLED` and several others are marked
"not readable" rather than given a value. That is a real limitation of the supported
access path, and the correct response is to read them from a container that prints
them rather than to assume. The maintenance pass prints only a subset today; extending
that printout is the concrete next step for closing this gap, and it costs nothing.
