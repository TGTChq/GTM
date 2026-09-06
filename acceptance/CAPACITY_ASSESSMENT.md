# 1,000 new approved leads/day — assessment on measured identities

Supersedes the earlier capacity sections, which reasoned from posting counts and
from flag names. This one uses the production identity functions on real retained
payloads, and states each number's cohort and unit.

## What was measured, and how

`run_maintenance.capacity()` ran inside the production container against
`run_artifacts/<run>/enrichment/postings.json`, using the same functions production
uses — `multi_source_acquisition._classify` for `_matched_role`,
`role_mapping.get_bucket_name_for_job` for the function bucket, and
`airtable_client._company_identity_keys_from_job` for the employer identity the
suppression rule keys on.

| cohort | postings | distinct companies | **company × function opportunities** | postings/opp | role-relevant |
|---|---|---|---|---|---|
| 2026-09-04 (≈10-day backlog) | 6,205 | 3,819 | **4,147** | 1.496 | 6,113 |
| 2026-09-06 (**one day**) | 226 | 170 | **174** | 1.299 | 223 |

This is the quantity that bounds approvals: `AIRTABLE_SUPPRESS_EXISTING_COMPANY_FUNCTION`
allows one active row per company × role bucket, so **approved rows/day ≤ new
company × function opportunities/day**.

Daily addressable inventory, counted 2026-09-06 over one 24h `date_created` window
with production filters (0 Jobs credits, 1 request each):

| source | postings/24h |
|---|---|
| Fantastic LinkedIn + title expression | 361 |
| Fantastic ATS + title expression | 520 |
| **union of the two** | **unmeasured** — the LinkedIn query sends `exclude_ats_duplicate`, so ≤ 881 |
| Wellfound / Y Combinator | not counted in this window (205 postings on 09-04) |
| 145 direct ATS boards | **inactive** — the lane is built only when `"ats"` is in `--lanes`, and production passes `--lanes fantastic` |

## Inventory already paid for and preserved

Measured 2026-09-06T12:13Z by loading custody through the pipeline's own
`pending_work.load` and running the same identity functions as the table above:

| held | distinct companies | **company × function opportunities** | role-relevant | unidentifiable |
|---|---|---|---|---|
| 3,595 postings | 2,808 | **2,998** | 3,500 | **0** |

Every held posting resolves to an employer and a function, so this is work that can
be enriched rather than a store of stubs. It is already bought: resuming it costs no
provider credits, only Apollo enrichment.

Against the 1,000/day target that is roughly **three days of opportunity inventory
sitting in hand** — it does not raise the daily ceiling, but it means the first days
after Apollo is restored are not inflow-bound.

## The internal loss, and its correction

The 2026-09-06 run billed **5,444** provider rows and kept **226** — 4% useful. The
cause was the cross-run offset cursor: the provider documents offset paging only for
draining a set in one pass, and the rising 7-day frame floor makes a resumed index
address different rows. It re-bought inventory it already held while rows expired
unacquired.

Replaced with a `date_created` slice cursor. Measured on the moving-floor replay:

| cursor | budget | acquired | expired unacquired | billed | useful |
|---|---|---|---|---|---|
| offset | 240 | 376 | 44 | 600 | 62.7% |
| **slice** | 240 | **420** | **0** | **420** | **100%** |

Every row bought is a row not already held. Applied to the 09-06 run's own budget,
5,444 credits would buy ~5,444 net-new postings instead of 226 — about **24× the
inventory for the same spend**. That is the largest internal loss found, and it is
corrected. It has **not** yet run in production: acquisition is paused.

### The slice boundaries were validated against the live feed

Counted 2026-09-06T11:15Z with the count endpoint (0 Jobs credits, 5 requests), same
production query:

    whole 24h window            350
      slice 11:00Z->17:00Z      164
      slice 17:00Z->23:00Z       82
      slice 23:00Z->05:00Z       57
      slice 05:00Z->11:00Z       47
      -------------------------------
      sum of 4 slices           350      MATCH

The slices tile the window exactly against the provider's own `date_created`
semantics -- no gap, no double count. That is the property the whole cursor rests
on, and it is now confirmed on the real API rather than only in a fixture.

Note also that this day's LinkedIn count is **350** where 2026-09-06 earlier gave
**361**: these are observations of a moving feed, not a stable ceiling, exactly as
stated above.

## What that implies for 1,000/day

Steady state is bounded by **daily inflow**, not by budget once the cursor is fixed:

    ≤881 postings/day  ÷  1.3 postings/opportunity  ≈  ≤678 opportunities/day

The 1.3 comes from the 2026-09-06 daily cohort (226 → 174). The backlog cohort gives
1.496 (6,205 → 4,147), which would yield ~589. Taking the smaller divisor is the
CONSERVATIVE choice -- it produces MORE opportunities per posting and therefore the
most favourable estimate. The conclusion below holds at either ratio.

Three conversions from the 09-04 run, kept apart because they are different stages
and all three are floors (Apollo truncated it at 2,170 of 2,410 leads):

    posting     -> approved   781 / 6,205 = 12.6%
    opportunity -> approved   781 / 4,147 = 18.8%
    lead        -> approved   781 / 2,410 = 32.4%

The projection below uses the opportunity ratio, matching the unit it projects from.

**Even at a perfect 100% opportunity → approved conversion, the two currently active
sources cannot sustain 1,000 approved leads/day.** The shortfall is on the correct
unit, from measured identities, on a matched window.

At the only observed conversion — 09-04 produced 781 Airtable rows from 4,147
opportunities, **18.8%**, and that run was truncated by Apollo at 2,170 of 2,410
leads so 18.8% is a **floor** — the same inflow yields roughly **127 approved/day**.

### The external requirement, quantified

To reach 1,000 approved/day:

| if approval conversion is… | opportunities/day needed | postings/day needed (@1.3) | deficit vs ≤881 |
|---|---|---|---|
| 18.8% (observed floor) | ~5,320 | ~6,900 | **~6,000/day** |
| 50% | 2,000 | ~2,600 | ~1,700/day |
| 100% (unreachable) | 1,000 | ~1,300 | ~420/day |

**No conversion improvement alone closes it.** More inventory is required in every
scenario.

### Where inventory could come from — and what is unmeasured

* **The title filter is the largest single lever.** The same firmographic filters
  without the title expression returned **5,511 postings/24h** on LinkedIn — about
  15× the titled 361. Whether any of those are commercially relevant is **not
  established**, and the title-selected corpus cannot estimate it. Functional
  discovery is the mechanism built to measure exactly this, and it is **off**.
* **145 direct ATS boards** are registered and never invoked. Yield unmeasured.
* **Wellfound and Y Combinator** contributed 205 postings on 09-04; not counted in a
  matched 24h window.
* **The source union** is unmeasured, so ≤881 is an upper bound, not the figure.

### Sending is not the constraint

Measured read-only on Instantly: 16 of 18 campaigns ACTIVE, `daily_limit` 550 each,
33 sending accounts, 3-step (control) and 4-step (challenger) sequences. That is
roughly 8,800 sends/day of capacity against a target of 1,000 approved leads/day, so
nothing downstream of approval limits throughput.

(The 769 leads showing zero executed steps are queued, not stuck: campaigns run
Mon-Fri 08:00-18:00 America/Chicago and the leads landed Friday 19:02 CT, so the
first send is Monday 08:00 CT.)

### Still dependent on Apollo

The opportunity → contact → send-safe → approved conversion cannot be re-measured
while Apollo refuses every credit-consuming call. The 18.8% floor comes from a
truncated backlog run and should not be treated as the steady-state rate.

## Conclusion

The target is **not achievable from the sources currently active**, and the gap is
now quantified on the right unit rather than asserted. It is **not** shown to be
unachievable in principle: three inventory sources are unmeasured and one — the
title-excluded inventory, 15× the current volume — has a purpose-built measurement
path that is switched off.


---

# One consolidated authorization request

Everything implementable has been implemented. These are the only decisions left,
each with its design and its cost, so they can be approved or declined once.

### 1. Measure relevance among title-excluded inventory — **500 Fantastic Jobs credits, 0 Apollo**

The single largest identified lever: the production title expression admits 361
postings/24h on LinkedIn while the same firmographic filters without it admit
**5,511**. Whether any of that 15× is commercially relevant is unmeasured, and the
title-selected corpus we hold cannot estimate it.

*Design.* One bounded pass: fetch 500 rows on the production firmographic filters
with the title expression removed, classify every row offline with the production
`_classify`, and report the role-relevant fraction with its confidence interval. 500
rows gives roughly ±4 points at 95% confidence — enough to tell "a few percent" from
"a third" and therefore to decide whether widening the catalog is worth anything.

*Cost.* 500 Jobs credits, ~0.6% of the 77,642 remaining. No Apollo, no enrichment,
no delivery. The rows are classified and counted, not enriched.

*Decision it informs.* Whether to widen the role catalog — the only lever of the
right order of magnitude for the 1,000/day target.

### 2. Activate the 145 direct ATS boards — **0 credits, one variable**

`ATS_DIRECT_ACQUISITION_ENABLED` is already true, but the lane is built only when
`"ats"` is in `--lanes` and the production start command passes `--lanes fantastic`.
The boards are scraped directly, so they cost no provider credits.

*This no longer needs an access I do not have.* It previously did — the
start-command mutation is denied — so `ACQUISITION_EXTRA_LANES` was added
(`6369bd9`): a comma list that is **added** to `--lanes` and can only widen it, so
the deployed start command remains the floor of what runs. It is resolved before the
strict preflight, so the added lane faces the same dependency checks as a requested
one. Today's production preflight already reads `boards 145 from ats_board_registry
(145 tracked) err=none`, so that dependency is met.

*Design.* Set `ACQUISITION_EXTRA_LANES=ats` on GTM — an authorized `variableUpsert`.
One run then measures the boards' yield in postings and company × function
opportunities using the `capacity()` step already built.

*Cost.* Zero provider credits. It does consume Apollo enrichment for whatever it
finds, so it waits on the same Apollo prerequisite as everything else — which is why
it is listed as a decision rather than already done.

### 3. Historical backfill — **a row budget, to be chosen**

`run_historical_recovery` is implemented and offline-verified but needs both a flag
and a non-zero `FANTASTIC_HISTORICAL_RECOVERY_MAX_ROWS_PER_RUN`; it is deliberately
unreachable by flipping one switch. Cost is exactly the row budget granted, one
credit per row. No amount is requested here: it only becomes worth spending once (1)
tells us whether wider inventory converts.

### 4. Apollo billing — **no cost, read-only**

Five screens, listed in `INCIDENT_2026-09-06_apollo_credits.md`. Without them the
opportunity → approved conversion cannot be re-measured, and the 18.8% floor stands
as the only observation.

**Nothing above is spent unless approved. Acquisition remains paused meanwhile.**
