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

## RETRACTED: the "<=881 postings/day" ceiling

The earlier version of this document bounded daily inventory at **<=881 postings**
from two count probes taken on **2026-09-06 — a Sunday**, and added them together.
Both halves of that were wrong, and the conclusion built on them ("the target is not
achievable from the sources currently active") does not survive.

Re-measured over **five matched 24h `date_created` windows** with the production
query builders and the production title expression, `/v1/active-jb-count` and
`/v1/active-ats-count`, **0 Jobs credits, 35 requests**
(`acceptance/inventory_probe.py`, raw JSON in
`evidence/20260906-maintenance/inventory_probe.json`):

| window ends | linkedin | ats | wellfound | yc | **addressable** | untitled |
|---|---|---|---|---|---|---|
| 2026-09-06 (Sat→Sun) | 327 | 111 | 6 | 2 | **446** | 8,341 |
| 2026-09-05 | 1,159 | 1,161 | 30 | 12 | **2,362** | 37,052 |
| 2026-09-04 | 1,266 | 1,076 | 28 | 10 | **2,380** | 32,945 |
| 2026-09-03 | 1,437 | 1,108 | 30 | 9 | **2,584** | 35,066 |
| 2026-09-02 | 1,571 | 1,291 | 28 | 7 | **2,897** | 40,274 |

**Business-day inventory is 2,362–2,897 postings/day; the weekend window is 446.**
The single day the old ceiling was measured on is the lowest of the five by a factor
of five. Quoting it as a ceiling was the error, not the arithmetic.

Two provider facts had to be established before any of this could be counted, and
both were previously assumed:

* **The ATS source is a different DATASET, not a `source` value.** It lives on
  `/v1/active-ats`; sending `source=ats` to active-jb returns **0**, which the first
  version of the probe would have published as "ATS contributes nothing".
* **`/v1/active-ats-count` rejects `include_basic_organization_details`** with a
  400 — it shapes a row payload and that endpoint returns no rows. Found by
  bisecting the parameters.

The two are **additive, not overlapping**: the active-jb queries send
`exclude_ats_duplicate=true`, so a posting carried by both is returned by active-ats
only. Wellfound and Y Combinator are additive for a different reason — they carry no
provider firmographics, so the null-excluding predicates drop them from the
firmographic union entirely and they must be counted on their own.

### What that does to the target

    2,556 postings/day (business-day mean)  /  1.3 postings per opportunity
      ~=  1,966 company x function opportunities/day

At the conservative backlog ratio (1.496) it is ~1,708. **Either way, daily inventory
supports well over 1,000 opportunities/day.** The target is *not* inventory-bound
from the sources already active, which is the opposite of what this document said
this morning.

The binding constraint is the **opportunity -> approved conversion**, observed once
at **18.8%** (781 of 4,147) on a run Apollo truncated at 2,170 of 2,410 leads — a
floor, not a rate.

| if conversion is… | approved/day at ~1,966 opportunities |
|---|---|
| 18.8% (observed floor) | ~370 |
| 35% | ~688 |
| **51%** | **~1,000** |
| 60% | ~1,180 |

So the external requirement is now specific: **roughly 51% opportunity -> approved,
or more inventory.** And there is a great deal more inventory — the same filters
without the title expression return **30,736/day on average, 14.4x** the titled
volume.

### The spend this implies, stated plainly

Acquiring ~2,556 postings/day costs ~2,556 Jobs credits/day, about **77,000/month**
against ~77,600 remaining in the current cycle. Running at full measured inventory is
therefore roughly a whole month's allowance, and it is a budget decision rather than
a capability one. Nothing here is spent without authorization; acquisition remains
paused.

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

## What that implies for 1,000/day — SUPERSEDED SECTION, corrected

Everything between here and the next heading was reasoning from the retracted
weekend measurement. The three conversion ratios below are still valid — they come
from the 09-04 run's own identities — but the inventory figure they were multiplied
against was wrong by about 3x, and the conclusion is reversed.

Three conversions from the 09-04 run, kept apart because they are different stages
and all three are floors (Apollo truncated it at 2,170 of 2,410 leads):

    posting     -> approved   781 / 6,205 = 12.6%
    opportunity -> approved   781 / 4,147 = 18.8%
    lead        -> approved   781 / 2,410 = 32.4%

~~**Even at a perfect 100% opportunity → approved conversion, the two currently
active sources cannot sustain 1,000 approved leads/day.**~~ **WITHDRAWN.** That
sentence rested on <=881 postings/day, measured on the one Sunday in the range.
Business-day inventory is 2,362–2,897 postings/day, so the two active sources supply
roughly **1,700–2,200 company × function opportunities/day** — comfortably above
1,000 before any conversion is applied.

### The external requirement, corrected

To reach 1,000 approved/day at ~1,966 opportunities/day (business-day mean ÷ 1.3):

| if approval conversion is… | approved/day | verdict |
|---|---|---|
| 18.8% (observed floor, truncated run) | ~370 | short |
| 35% | ~688 | short |
| **~51%** | **~1,000** | **target met on current inventory** |
| 60% | ~1,180 | headroom |

**The requirement is a conversion requirement, not an inventory requirement.** That
is a different and far more tractable problem than the ~6,000 postings/day deficit
this document previously reported, and it can only be measured once Apollo serves —
the 18.8% floor comes from a run that was cut off partway through contact discovery.

If conversion turns out to be genuinely near 19%, the inventory to close the gap
also exists: the same filters without the title expression return **30,736
postings/day**, 14.4x the titled volume. Whether any of it is commercially relevant
is the one thing still unmeasured, and item 1 below is the bounded way to find out.

### Where inventory could come from — three of the four are now measured

* **Wellfound and Y Combinator — MEASURED.** 24.4 and 8.0 postings/day respectively
  over five matched windows. Small, real, and additive: they carry no provider
  firmographics, so the null-excluding predicates drop them from the firmographic
  union and they must be counted separately rather than assumed inside it.
* **The source union — MEASURED.** Not 881. active-jb and active-ats are
  complementary by construction (`exclude_ats_duplicate=true`), so they add:
  1,152 + 949 + 24 + 8 = **2,134/day** on average, 2,362–2,897 on business days.
* **145 direct ATS boards — measured separately**, in the next section. They are
  scraped from each employer's own board and cost no provider credits at all.
* **The title filter remains the largest single lever, and the one thing still
  unmeasured.** Same filters without the title expression: **30,736 postings/day**,
  **14.4x** the titled volume. Whether any of it is commercially relevant is not
  established, and a title-selected corpus cannot estimate it. Functional discovery
  is the mechanism built to measure exactly this, and it is off.

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

**The target is inventory-feasible from the sources already active, and
conversion-bound.** Corrected from this morning's opposite conclusion, which was
built on a single weekend window.

* Business-day addressable inventory: **2,362–2,897 postings/day**, measured over
  five matched windows at zero credits.
* That is **~1,700–2,200 company × function opportunities/day** on the production
  identity functions — above the 1,000/day target before conversion.
* Reaching 1,000 approved/day therefore needs **~51% opportunity → approved**. The
  only observed figure is 18.8%, and it is a floor from a run Apollo truncated.
* If real conversion is far below that, **14.4x more inventory** sits behind the
  title filter, unmeasured.
* Sustaining full inventory costs ~2,556 Jobs credits/day — about a month's current
  allowance. A budget decision, not a capability one.

**Not achieved, and not claimed as achieved.** What has changed is that the obstacle
is now identified and bounded: measure conversion on an untruncated run, which
requires Apollo, which requires the billing screens in
`INCIDENT_2026-09-06_apollo_credits.md`.

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

*Why this cannot be shortcut by "just adding the obvious adjacent titles".* The
catalog already holds **131 roles across 10 function buckets** (engineering 32,
marketing 27, gtm_revenue 15, finance 12, people_hr 12, operations 9, customer
success 7, customer support 6, ecommerce 6, product 5). Probing it for the titles one
would reach for first — revenue operations, growth, partnerships, business
development, sales enablement, solutions, lifecycle, brand, content, SEO, product
marketing, sales development, community — finds **all but three already present**
(demand generation, account management, channel). So the 15× excluded inventory is
unlikely to be adjacent GTM roles that were simply forgotten; it is far more likely
to be genuinely different work. That makes a guess a bad instrument and a
measurement a cheap one, which is exactly why this is the request rather than a
catalog patch.

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
