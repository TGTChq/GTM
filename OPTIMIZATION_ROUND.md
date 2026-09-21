# Final optimization round — unique company x campaign yield

Baseline A: production `6c79aa1`, deployed and scheduled (`0 3 * * *`), untouched.
Candidate B: branch `opt/unit-yield`, from `6c79aa1`, at `bfd2a10`, NOT deployed.

## Where the compression is (same cohort, run `20260921T033253.666863Z-6c015841`)

| stage | n |
|---|---|
| records billed | 2,696 (159 re-bought postings already known) |
| unique employers | 1,185 |
| company x campaign units formed | 899 (144 already historical) |
| units closed on company size | 377 |
| units still open (waiting) | 363 |
| units with >= 1 contact | 197 (1: 111 · 2: 75 · 3: 11) |

Per acquisition cell, per 1,000 billed records:

| cell | employers | units | NEW units | size-closed | re-bought | eligible |
|---|---|---|---|---|---|---|
| ats · priority | 656 | 560 | 507 | 5 | 0% | **402** |
| jb · priority | 692 | 579 | 535 | 7 | 0% | **264** |
| ats · discovery | 668 | 450 | 258 | 175 | 24% | 108 |
| ats · exhaustive | 436 | 329 | 298 | 151 | 2% | 115 |
| jb · exhaustive | 438 | 325 | 291 | 185 | 5% | 58 |

Findings, and what was NOT built because the numbers did not justify it:

* The priority cells (already 80% of slots in `6c79aa1`) are diverse: ~1.5 postings
  per employer, 92% new units, near-zero size closures. Upstream employer
  saturation exclusion would recover at most the ~8% historical units there.
* Postings are already collapsed into one (employer, function) opportunity before
  any Apollo spend, so duplicate openings cost Fantastic records and cheap
  deterministic classification, never an Apollo credit. A separate saturation
  registry would duplicate that key; not built.
* The discovery cell's 24% re-buy is inherent: discovery's filter set contains
  priority's, so it re-returns priority's jobs in the same window. Its size
  closures are known-bad employers the upstream slug exclusion deliberately skips
  for discovery. Both together are worth ~12 eligible contacts a day at the
  current allocation.
* **The binding loss is inside the contact stage, and it is technical.**

## The highest-value correction: a company's mail domain

976 Apollo `person_match` credits paid, 724 approvals. Of the 252 that did not
become contacts, **222 were Apollo-verified and currently employed, rejected only
as `email:domain_not_employer`**. In 219 of 222 the person's own Apollo
organisation carried exactly our employer domain. Companies mail from a domain
that is not their website: Northern Trust `northerntrust.com -> ntrs.com`, JB
Poindexter `-> jbpco.com`, GALE `gale.agency -> galepartners.com`, OTO
`careersatoto.com -> otodevelopment.com`.

`corroborated_mail_domain()` (flag `TGTC_CORROBORATED_MAIL_DOMAIN`) accepts only
when: the person's own Apollo org domain is our employer; the domain is not free
mail or a hosted-service subdomain; it does not name a FORMER employer (a past
role at the same Apollo organisation is a promotion, not a former employer); and
it is corroborated by another verified employee of the same employer or keeps the
employer label under another suffix. New alignment label
`CORROBORATED_MAIL_DOMAIN`; the UK gate still demands `EXACT_EMPLOYER_DOMAIN`.

### Shadow on identical inventory (real gate code, the 976 paid people)

| | A `6c79aa1` | B |
|---|---|---|
| Apollo credits | 976 | 976 (zero extra) |
| approvals | 724 | **886** (+162, 50 employers) |
| approvals per credit | 0.742 | **0.908** (+22%) |
| additions by resolved contact country | — | US 150, UK 1 (still blocked), unknown 11 (blocked) |

Still rejected by B: 50 uncorroborated, 6 former-employer domains, 2 Apollo org
not the employer, 1 hosted/free. B cannot change a job or compliance decision: it
runs after every job gate and does not touch the compliance matrix. Conservative:
production corroborates against every stored person at the employer, the shadow
only against the day's 976.

## Exclusion audit (uncertain buckets only)

| code | n | examples | verdict | recovered |
|---|---|---|---|---|
| `role:quota_carrying_sales` | 14 | Inside Sales Rep, Sales Executive, Strategic Accounts | **bug**: flag only reached the deterministic path | 14 jobs -> GTM |
| `deliverability:professional_license` | 95 | 89 Life Insurance Agent (1 employer), RN, pharmacy tech, laborer; 2 Tax Managers | licence waived only as the SOLE exclusion on a Finance title | 2 jobs |
| `employment:other` | 48 | Housekeeper, Busser, Cook, Machinist, "Contingent Upon Award" base roles | KEPT: explicit non-standard employment value | 0 |

Confirmed exclusions (part-time, contract, physical, staffing, government,
non-profit, clearance) were not reopened.

## Capacity with B (derived from one day of measured yields — NOT an observed day)

Inputs: blended 4:1 yield 0.8 x 0.315 + 0.2 x 0.108 = 0.274 eligible/record under A,
x 1.23 under B = **0.337** (95% interval from the arm Wilson bounds: 0.29–0.39);
0.975 delivered per eligible; **1.24 Apollo credits per eligible** under B (1.53 under A);
0.26 Anthropic calls per record; ~3.5 sequence emails per lead (4 steps, stop on reply).

| target / day | Fantastic records | Apollo credits | Anthropic | leads added | steady emails | vs current grants |
|---|---|---|---|---|---|---|
| 750 | 2,282 (68k/mo) | 954 (29k/mo) | ~590 | 750 | ~2,600 | fits (3,300 / 1,000) |
| **1,000** | 3,044 (91k/mo) | **1,272 (38k/mo)** | ~790 | 1,000 | ~3,500 | Fantastic fits the 100k plan; **Apollo +272/day** |
| 1,250 | 3,804 (114k/mo) | 1,590 (48k/mo) | ~990 | 1,250 | ~4,400 | Fantastic +15k/mo over plan; Apollo +590/day |

Interval: at the low yield bound (0.29), 1,000/day needs ~3,540 records/day, just
over the plan's 3,333/day.

Expected distribution at 1,000/day (today's created shares): operations ~397,
finance ~179, ai_technical ~125, gtm ~92, marketing ~79, people_hr ~46,
customer_experience ~44, product ~31, ecommerce ~8.

### Instantly

Instantly does not reject leads beyond a campaign's sending limit; they queue.
The binding constraint is sending capacity for OPERATIONS: ~397 leads/day x 3.5
= ~1,400 emails/day against its daily_limit of 550. Across all nine campaigns
the nominal capacity is 9 x 550 = 4,950/day, enough in aggregate; the other
eight have slack. The per-campaign INSERTION ceiling (150) is this pipeline's own
control, set to what each campaign can send; raising it to ~450 lets 1,000/day
be inserted, with Operations accumulating ~240 queued leads/day until its sending
capacity rises.

### The package for 1,000/day, none of it bought

| item | amount | cost |
|---|---|---|
| Apollo | +272 credits/day (+~8,200/month) over the 1,000/day grant; **account balance unknown** | ~$131/month at $160 per 10,000 credits — **user-provided, UNVERIFIED** |
| Instantly | OPERATIONS daily_limit 550 -> ~1,400 (33 senders -> ~42/sender/day; verify health or add senders); insertion ceiling 150 -> ~450 | no purchase if existing senders can carry it |
| Fantastic | none at the point estimate; ~+200 records/day at the low yield bound | within the 100k plan at the point estimate |

## Promotion decision

B wins the shadow on identical inventory. It is **not promoted yet**, by design:

1. `6c79aa1` stays deployed so tomorrow's 03:00 UTC scheduled run is the clean
   Phase 1 measurement of the corrected 4:1 allocation (the instruction).
2. The units B would recover are mostly in 24-hour `buyer_search_pending` waits,
   so a canary today would find almost nothing to process.

Runbook after the scheduled run:

```
# 1. Phase 1 same-cohort waterfall for the scheduled run
psql -v run_id="'<run_id from run_log target/end>'" -f scripts_ops/phase1_waterfall.sql
# 2. bounded B canary on that run's inventory, then compare contacts per record
# 3. promote the exact tested commit only if B beats A; tag both
```
