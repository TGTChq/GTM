# Internal limits on the active path: what binds, what does not, what changed

Deployment `8177270`, both services SUCCESS. Every effective value below was read
from Railway (`variables` / `serviceInstances`), and every "was it reached" comes
from the 2026-09-08 run `20260908T030247Z-e711a350` (deployment
`52e6184e-4c9e-4910-9323-0b36bb7ae2c7`), not from a default or an assumption.

**Three kinds of value are kept apart:** a repository default, a variable an
operator configured, and a limit actually applied on the active path. Several
limits people quote as ceilings are none of those three.

## The deployed start command

```
python -u run_orchestrator.py --mode live_acquisition_and_enrichment \
  --lanes fantastic --target 300 --airtable-write --global-budget 1500 \
  --artifact-root /app/data/state/orchestrator_v2
```

`--lanes fantastic` is widened by `ACQUISITION_EXTRA_LANES=ats`, which can only add.
The ATS budgets people cite -- 400 lane, 120 provider, 100 board, 8 boards -- are
**argparse defaults**, not values in that command and not configured variables.

## The table

| limit | effective value | what it limits | reached on 09-08? | change |
|---|---|---|---|---|
| `ALTERNATE_CONTACT_MAX_ENRICHMENTS_PER_RUN` | **100** (default; unset) | how many buckets may advance past their FIRST ranked candidate, per RUN | binding by construction: 1,506 companies considered, 536 contacts found | **CHANGED** — an absent operator limit now inherits continuous authorization |
| `--target 300` | 300 | `target_final_pass`; would break the company loop at 300 FINAL_PASS | **NO** — the break needs `not CONTINUE_AFTER_FINAL_PASS_TARGET`, and that is `1`. FINAL_PASS reached 358 and the run continued | none — it is not an Approved ceiling |
| `--global-budget 1500` | 1500 | `RequestBudget.limit`: HTTP **requests** for the ATS/free-feed lanes. Not credits, not Approved | **NO** — 456 physical requests | none |
| `--ats-lane-budget 400` | 400 (default) | ATS lane requests | **YES** — 393 requests, 85 of 145 boards skipped | none — see below |
| `--provider-budget 120` | 120 (default) | requests per ATS provider | **YES** — `workday:daikinapplied:budget_exhausted:provider` | none — see below |
| `--board-budget 100` | 100 (default) | requests per board | not reported as reached | none |
| `MAX_ELIGIBLE_COMPANIES_PER_RUN` | **0** = no cap | eligible companies per run | n/a | none |
| `TOPUP_MAX_ITERATIONS` | 40 | top-up slices per run | **NO** — 1 iteration | none |
| `TOPUP_RUNTIME_BUDGET_SECONDS` | 0 = none | wall clock | n/a | none |
| `PENDING_WORK_RESUME_MAX_PER_RUN` | 2000 (default) | custody adopted per **iteration** | 2,000 adopted | none — already per-iteration, drains across a run |
| `APOLLO_MAX_PERSON_MATCH_ATTEMPTS_PER_BUCKET` | 3 | paid matches per bucket | n/a | none — this is what bounds a bucket |
| `APOLLO_MAX_PERSON_MATCH_CALLS_PER_RUN` | unset = 0 = no ceiling | paid matches per run | n/a | none |
| `READY_DAILY_DELIVERY_LIMIT` | 0 = deliver everything | delivery | n/a | none |

**What actually stopped the run:** `final_stop_reason apollo_circuit_open`. Apollo
refused with balance zero after 1,506 companies. No internal limit ended it.

## The one change

`ALTERNATE_CONTACT_MAX_ENRICHMENTS_PER_RUN` counts **advances to another candidate**,
not credits and not calls: a bucket that moves to rank 2 spends one whether or not
that move ends in a paid match. It exists so a pathological batch cannot multiply
work, and 100 was chosen when a run was a few hundred companies.

In continuous mode that default is a recovery ceiling rather than a safety one. On
09-08, buckets 101 onward stopped at their first candidate whatever Apollo would
have served. An **absent** operator limit now inherits continuous authorization,
the identical shape `APOLLO_ORG_ID_FALLBACK_BUDGET_CONFIGURED` already uses.

Unchanged: an explicit limit still applies, **an explicit zero still disables
advancing**, the cascade flag still gates everything, advances are still counted, and
the availability circuit and quality gates are untouched. The env var was NOT set to
zero -- zero is off.

**Why it cannot loop.** Two bounds survive the run-level ceiling and are what
actually stop a bucket: candidates are walked by index (`_index + 1 < len(...)`) so
none is revisited, and `APOLLO_MAX_PERSON_MATCH_ATTEMPTS_PER_BUCKET` (3) caps a
bucket's paid matches. Demonstrated by driving `process_company` over 130 buckets
without resetting the run budget between them: **130 advances with continuous
authorization, exactly 100 without it**, and the walk terminates either way.

## Why the ATS budgets were NOT raised

They did bind: 393 of 400 lane requests, a Workday provider stop, 85 of 145 boards
skipped. Raising them was still not justified, on the run's own yield table:

| direct ATS provider | net-new postings | ICP pass | HM found | Airtable rows |
|---|---:|---:|---:|---:|
| smartrecruiters | 1,439 | 0 | 0 | **0** |
| workday | 876 | 0 | 0 | **0** |
| ashby | 772 | 0 | 0 | **0** |
| lever | 417 | 1 | 1 | **0** |
| greenhouse | 202 | 1 | 0 | **0** |
| workable | 80 | 0 | 0 | **0** |

3,786 net-new direct-ATS postings produced **zero** Airtable rows, while Fantastic's
639 kept rows produced 129. Buying more of the first would not have produced Approved
contacts, and the run ended on Apollo -- enrichment, not acquisition, was the binding
constraint.

**And nothing was stranded.** A board skipped for budget is never fetched, so its age
keeps growing, it stays in the `overdue` bucket and is ordered first next run:
`ats_boards_selected_overdue 145`, `ats_boards_remaining 0`, and the run recorded
`cycle_length=3; full registry covered every 3 runs`. Coverage is achieved by
rotation across runs rather than by one run's budget.

The direct-ATS zero-delivery figure is **not** by itself proof that the lane is
worthless -- processing order, company grouping, the gates and the interrupted tail
would each need cohort-linked evidence. It is enough to show that raising a request
budget is not the demonstrated fix.

## Demonstrated in tests versus measured in production

Everything above is either a reading of production configuration, a counter from the
09-08 run, or an offline demonstration. **No Approved gain has been measured for this
change**: the first run under it is the next `0 3 * * *` cron. 130 advances in a test
is 130 advances in a test.

The historical unattributed losses are untouched and still open: the 4,505 duplicate
events of 09-08, the 5,218 / 2,722 / 172 counts, the 779 no-contact delivery skips and
the 81 withheld rows.
