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

### The 145 direct ATS boards — MEASURED, and they cost nothing

Swept 2026-09-06T14:40Z inside the production container: **all 145 boards, none timed
out, 0 provider credits** (each employer's own public board), nothing persisted —
`acceptance/ats_board_yield.py` calls the stateless `fetch_board_jobs`, so no lane,
checkpoint, board-health update or pipeline entry is involved.

| | |
|---|---|
| postings currently open | **20,293** |
| distinct companies | 134 |
| **company × function opportunities (stock)** | **615** |
| postings carrying a date | 7,953 |
| posted in the last 7 days | 3,251 → **464/day** |
| opportunities in that 7-day cohort | **308** (~44/day) |
| role-relevant under the production catalog | **21.84%** |

Two things follow, and they point in opposite directions.

**It is a fixed employer set, so it saturates.** 145 boards is 134 companies. Their
whole open inventory is 615 company × function opportunities; after the first sweep,
cross-run suppression means only genuinely new roles count, and that flow is ~44
opportunities/day. Real and free, but not a path to 1,000 on its own.

**Its relevance rate is the free half of authorization item 1.** These 20,293
postings arrive with **no title query at all** — it is the title-unfiltered sample
that the Fantastic corpus cannot provide. **21.84%** of them match the production
role catalog. Against that, the Fantastic titled/untitled ratio is 2,134/30,736 =
**6.9%**.

The two populations are not the same — the board registry is 134 hand-picked ICP
employers, the Fantastic untitled feed is every employer passing the firmographic
filters — so this does not settle the question. But it is the first evidence that the
title expression may be selecting well below the rate at which real ICP employers
actually hire for catalogue roles, and that is precisely what item 1 is for.

Per-provider, so a slow or broken adapter is visible rather than averaged away:

    workday          68 boards  12,857 postings   903.1s   1 failed (HTTP 422)
    smartrecruiters  31 boards   5,163 postings   204.7s
    ashby            22 boards     943 postings    12.2s
    greenhouse       13 boards     664 postings
    lever             6 boards     425 postings              1 failed (HTTP 404)
    workable          4 boards     241 postings     2.7s
    cornerstone       1 board        0 postings              1 failed (invalid JSON)

Workday is 63% of the postings and 78% of the wall clock. Three boards fail; they are
named here rather than smoothed into an average.

### Where conversion is actually lost — the pre-Apollo half, measured

Replayed 2026-09-06T14:52Z inside the production container over the retained
payloads: `run_precontact_qualification(..., fetch_sources=False)` — JobGate and
RoleGate, **no provider contacted, nothing spent, nothing written to production
state**.

| cohort | input | contact-eligible | rate | rejected | unverified |
|---|---|---|---|---|---|
| 2026-09-04 (6,205) | 6,205 | **5,753** | **92.7%** | 452 | 472 |
| 2026-09-06 (226) | 226 | **194** | **85.8%** | 32 | 13 |

**The pre-Apollo gates are not the loss.** They pass 93% of what acquisition brings
in. Every rejection reason that fired, on the 09-04 cohort:

    REJECT_QUALITY_GUARD_OTHER            288
    REJECT_ROLE_MISMATCH                   87
    REJECT_ROLE_NOT_GLOBALLY_COVERABLE     28
    REJECT_SECURITY_CLEARANCE_REQUIRED     23
    REJECT_EXCLUDED_SENIORITY              13
    REJECT_INTERNSHIP                      11
    REJECT_NON_PAYING                       1
    REJECT_FIELD_WORK_REQUIRED              1

So the 18.8% opportunity → approved decomposes as:

    4,147 opportunities
      -> 1,048 contacts found        25.3%   <-- the loss, and it is the APOLLO stage
      ->   781 Airtable rows         74.5%   of contacts
      -------------------------------------
                                     18.8%   overall  (0.253 x 0.745)

**Reaching ~51% therefore means moving `opportunity -> contact`, which is hiring-
manager discovery on Apollo.** At the observed 74.5% downstream rate it would need
about **68% opportunity → contact** against 25.3% observed — and that 25.3% comes
from a run Apollo truncated at 2,170 of 2,410 leads, so it is a floor of unknown
tightness.

**This makes the Apollo billing blocker the critical path, not a side issue.** Every
remaining question about the target — is 25.3% real or an artefact of truncation, does
the alternate-contact cascade or the org-ID fallback move it, what does an untruncated
run convert at — is downstream of Apollo answering. Nothing else in this document is
blocked; this is.

**One observability defect found in passing.** `REJECT_QUALITY_GUARD_OTHER` is 288 of
452 rejections — the single largest pre-Apollo rejection reason is literally "other".
It is 7% of postings so the volume at stake is small, but a catch-all cannot be acted
on, and this is the second time a fallback label has hidden the real reason (the first
was `REJECT_UNRESOLVABLE_POSTING`, 704 misattributed, fixed in PR #55). Recorded as a
finding, not fixed here: it changes no number in this assessment.

### Is any of the contact-discovery loss ours? Measured: no.

The 25.3% opportunity → contact was being attributed wholesale to Apollo. Part of
that stage never reaches Apollo: `_process_company` computes `_best_input_domain(job)`
offline, and an opportunity with no resolvable search domain is recorded
`missing_company_domain` without a people search ever running. If a large share
arrived domainless, that would be an **internal** loss fixable by domain resolution
rather than by billing — and it would be fixable today.

Measured offline over the retained payloads, grouped by `company_key_for_job` (the
key `_process_company` itself uses), 2026-09-06T15:16Z:

| cohort | opportunities | with first-party domain | depending on Apollo for one | recoverable from a sibling posting |
|---|---|---|---|---|
| 2026-09-04 | 4,148 | **4,109 (99.06%)** | 39 | 4 |
| 2026-09-06 | 175 | **173 (98.86%)** | 2 | 0 |

**WITHDRAWN — this table does not say what I claimed it said.** A resolvable
first-party domain proves an opportunity was ELIGIBLE to be searched. It does not
prove a search ran. The 2026-09-04 run was interrupted by Apollo partway through
contact discovery, so an unknown share of those 4,109 were never attempted at all —
and dividing 1,048 contacts by 4,109 *eligible* opportunities silently counts
never-attempted work as a hiring-manager failure.

**`25.5% opportunity → contact` is retracted. The rate is UNKNOWN pending
reconciliation against the original execution's own stage-entry evidence** — not a
regrouping of retained payloads with current code, which is what the table above is.

What the denominator has to be reconciled into, from the original run:

    opportunities actually SEARCHED        (people search issued)
      contacts returned
      genuine no-match                     (searched, nobody found)
    internal skips                         (never submitted to a search, and why)
    provider errors                        (attempted, Apollo refused)
    never attempted                        (the run stopped first)

Until that reconciles, no share of the conversion loss may be called external.

(A real but tiny structural quirk surfaced while testing it: `company_key_for_job` is
"domain or name", so one employer splits into two groups when only some of its
postings carry a domain, and the domainless half spends an Apollo organisation enrich
to return `missing_company_domain` while the domain sits on a sibling posting. It
affects **3 companies and 4 opportunities** in 4,148. Recorded, not worth a change.)

### Reconciled from the 2026-09-04 execution's own sealed record

Read 2026-09-06T17:39Z from that run's `waterfall.json` — the `hiring_manager` stage
as the execution sealed it, not a regrouping of payloads with current code.

    opportunities retained                       4,147
    leads the hiring-manager stage produced      2,410
      passed                                       711
      rejected                                     712
      deferred                                     987
      errored                                        0

    primary reasons (they sum to rejected + deferred exactly)
      not_icp                          606   internal skip   (our ICP rule)
      company_unresolved               106   internal skip
      hiring_manager_not_found         247   genuine no-match
      email_unverified                 740   contact FOUND, email unverifiable

    contact_discovery_entered                  ABSENT
    opportunity -> contact rate                UNKNOWN

**Two conclusions, and the first one corrects me.**

**1. The remaining conversion loss is NOT all external.** 712 of the 2,410 outcomes —
**29.5%** — are internal skips: 606 rejected by our own ICP rule and 106 whose company
could not be resolved. Those are decisions and data problems of ours, not Apollo
coverage. The claim that everything left was external is withdrawn; the run's own
record refutes it.

**2. The rate is still unknown, and the denominator is still missing.**
`contact_discovery_entered` — the counter emitted at the people-search decision point
— is absent from this run, which wrote no enrichment funnel at all. So how many
searches actually ran cannot be established. What CAN be said is a **lower bound on
work never attempted**: 4,147 − 2,410 = **1,737 opportunities never produced a lead at
all.** It is a lower bound because some of the 2,410 were also never searched — a
bucket with no domain gets a lead without a people search — so the true
never-attempted figure is higher.

**A third outcome that had been hidden inside "no contact".** `email_unverified` at
**740** is the largest single result: a person WAS found and their address could not be
promoted to verified. That is neither "nobody there" (247) nor a provider error (0),
and its remedy is entirely different — only Apollo can promote an email to verified,
so 740 opportunities reached a real person and stopped at verification. My first
classification filed it under provider errors; correcting it moved the largest bucket
on the run.

**What this does to the earlier decomposition.** `18.8% = 25.3% x 74.5%` was arithmetic
over incomparable populations and is withdrawn along with its inputs. The honest
statement is that of 4,147 opportunities the stage reached 2,410, of which 711 passed,
712 were rejected by us, 247 found nobody and 740 found somebody unverifiable — and
1,737 were never reached at all.

### The three outcomes, decomposed from the run's own corpus

Streamed 2026-09-06T18:58Z from the 09-04 enriched corpus. Counts are **per file**;
the run writes the same leads twice and summing reported 2,068 for 1,034.

**740 `email_unverified` — the bottleneck is COVERAGE, not verification.**

    apollo_email_status   verified 1,034   extrapolated 14
    unverified_no_valid_contact                       532
    unverified_email                                  109
    reroute_email_identity_mismatch                    96   <- OUR policy
    hunter_status (absent)                            452   <- no second opinion ran

Apollo verified 1,034 of the addresses obtained and left only **14** unverified. There
is no large population of "address present, Apollo would not verify it". The mass is
`unverified_no_valid_contact` — no usable contact found. **A second verification
provider would address ~14 leads, so it is not worth a contract**; the finding is
recorded in `EMAIL_VERIFICATION_DEPENDENCY.md` as considered and declined on evidence.

**606 `not_icp` — named families, and the largest is a headcount rule.**

    reject_company_too_large     596
    reject_excluded_industry     326
    reject_company_too_small     126
    reject_government             66
    reject_staffing               44
    reject_healthcare             30

These are posting-level occurrence counts and the stage's 606 is in leads, so they do
not sum to it and are not presented as a partition of it. What they establish is the
SHAPE: the dominant ICP exclusion is `company_too_large`, a headcount bound that is
ours and deliberate. **No demonstrated false rejection was found** — every family
corresponds to an agreed rule. Whether the headcount bound is set correctly is a
business decision, not a defect, and the ICP is preserved as agreed.

**106 `company_unresolved` — not decomposable from this run.** The artifacts carry no
`company_criteria_reason__*` stats, so the failure mechanism cannot be attributed.
Recorded as not decomposable rather than guessed. The related counters
`unverified_organization` (112) and `unverified_employer_identity` (82) are in the same
region and suggest identity resolution rather than a missing domain — consistent with
the separate finding that **99% of opportunities carry a first-party domain**, which
rules out "no domain" as the mechanism.

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
conversion-bound — and the conversion loss is Apollo-bound.** Corrected twice today:
first from "not achievable" (built on a weekend window), then narrowed from "raise
conversion" to the specific stage that loses it.

* Business-day addressable inventory: **2,362–2,897 postings/day**, five matched
  windows, zero credits.
* That is **~1,700–2,200 company × function opportunities/day** on the production
  identity functions — above the 1,000/day target before conversion.
* Plus **615 opportunities of free stock** on the 145 direct boards, ~44/day
  thereafter.
* Reaching 1,000 approved/day needs **~51% opportunity → approved**. Observed 18.8%.
* That 18.8% is **25.3% opportunity → contact × 74.5% contact → approved**. The
  pre-Apollo gates pass **92.7%** and are not the loss.
* 99.1% of opportunities carry a first-party domain, so they were **eligible** to be
  searched. That is not evidence that they were searched, and on an interrupted run
  the two differ. See the reconciliation section.
* **The opportunity → contact rate is UNKNOWN** — the denominator was never recorded.
* **The remaining loss is not all external.** The 09-04 stage record shows **712 of
  2,410 outcomes are internal skips** (606 `not_icp`, 106 `company_unresolved`), and
  **1,737 opportunities never reached the stage at all**.
* The largest single outcome is **740 `email_unverified`** — a person found, their
  email unverifiable. Only Apollo can promote an email to verified.
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

**The single largest identified lever, and now the only one still unmeasured.** The
production title expression admits ~2,134 postings/day; the same firmographic filters
without it admit **30,736/day — 14.4×**. Whether any of that is commercially relevant
is unmeasured, and a title-selected corpus cannot estimate it.

*What the free work already established.* The 145 direct boards give a
title-unfiltered corpus of 20,293 real postings at ICP employers, of which **21.84%**
match the production role catalog — against a Fantastic titled/untitled ratio of
**6.9%**. Different populations, so it does not settle the question; but it is
evidence that the title expression may select well below the rate at which ICP
employers actually hire for catalogue roles, which raises rather than lowers the value
of measuring it properly.

*Design.* One bounded pass: fetch 500 rows on the production firmographic filters
with the title expression removed, classify every row offline with the production
`_classify`, and report the role-relevant fraction with its confidence interval. 500
rows gives roughly ±4 points at 95% confidence — enough to tell "a few percent" from
"a fifth", which is exactly the range in dispute.

*Cost.* 500 Jobs credits, ~0.6% of the ~77,600 remaining. No Apollo, no enrichment, no
delivery. The rows are classified and counted, never enriched.

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

### 2. Activate the 145 direct ATS boards — **0 credits, one variable** — MEASURED

No longer a request to measure something; it is now a decision with the number
attached. Swept 2026-09-06: **20,293 open postings, 134 companies, 615 company ×
function opportunities, ~464 postings/day and ~44 opportunities/day** in the trailing
7 days, at **zero provider cost**. See the measurement section above.

*What it does not do.* 145 boards is a fixed employer set, so it saturates: the first
sweep takes the 615, and after that only new roles count. It is a modest, permanent,
free addition — not a route to 1,000/day.

*What it costs downstream.* Nothing at the provider, but every opportunity it finds
consumes Apollo enrichment like any other, so it waits on the same prerequisite.

*Design.* `ACQUISITION_EXTRA_LANES=ats` on GTM — an authorized `variableUpsert`; the
start-command change is no longer needed (`6369bd9`). Today's production preflight
already reads `boards 145 from ats_board_registry (145 tracked) err=none`.

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
