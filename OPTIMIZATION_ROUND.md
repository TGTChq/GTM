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

---

# Outcome, 2026-09-21 (supersedes "Promotion decision" and the Instantly/package sections above)

**The +162 above was circular** (a relaxed acceptance could corroborate another).
The corrected rule seeds only from a strict-rule contact, a provider-confirmed
organization domain, or at least two independently verified current employees
whose Apollo org is the employer; same label under another suffix is never enough.

| same 976 paid people | A `6c79aa1` | B (non-circular) |
|---|---|---|
| approvals | 724 | 791 (+67) |
| US outreach-eligible | 639 | 702 (+63) |
| approvals per credit | 0.742 | 0.810 (+9.2%) |

Live B canary (zero Fantastic, zero paid Apollo, targeted release only): 31
approvals, all `independent_employees`; 28 eligible; 17 created in Instantly
exactly once (Challenger only, 0 Control); 11 deferred by the OPERATIONS ceiling;
3 blocked (unknown jurisdiction); 0 bad basis, 0 employer mismatch, 0
free/SaaS/ATS, 0 non-US eligible, 0 duplicates, 0 new provider reservations.
Web check of 26 employer/mail-domain pairs: 21 confirmed, 5 unreachable or
blocked (all same-employer subsidiaries or legacy domains), 0 wrong employer.

**Promoted:** `c260f3a` (B), then `faad112` (capacity). Deployment `ba69f1f4`.
Tags: `release-capacity-faad112`, `release-mail-domain-c260f3a`,
`pre-mail-domain-6c79aa1`.

## Instantly: the real per-campaign ceiling is `daily_max_leads`

All nine Challenger campaigns: `daily_max_leads=100`, `prioritize_new_leads=true`,
4 steps (delays 3,4,5,1), `stop_on_reply=true`, `daily_limit=550`. So a campaign
starts at most 100 new leads a day however many are enrolled, and steady state is
~4 emails per lead. Enrolled is not contacted: on 09-21 OPERATIONS took 338
eligible contacts and had contacted 191 of 329 enrolled.

Senders: 252, all active, warmup on, score 100, 20/day each (5,040/day). Eight
pools, each shared by one function's Challenger and Control campaigns (33 each;
PEOPLE_HR 21; AI_TECHNICAL and ECOMMERCE share one pool).

Changed (add-only, every sender keeps 20/day, copy/steps/schedule/identity
fingerprint-verified unchanged):

| campaign | senders | daily_limit | daily_max_leads | pipeline ceiling (new/day) |
|---|---|---|---|---|
| OPERATIONS | 33 -> **80** (+20 PRODUCT, +17 CX, +5 MARKETING, +5 GTM, shared) | 550 -> **1,600** | 100 -> **350** | 150 -> **350** |
| PRODUCT | 33 (13 exclusive) | 550 | 100 | 150 -> 60 |
| CUSTOMER_EXPERIENCE | 33 (16 exclusive) | 550 | 100 | 150 -> 80 |
| ECOMMERCE | shares AI's pool | 550 | 100 | 150 -> 30 |
| the other five | unchanged | 550 | 100 | 150 -> 100 |

Pipeline ceilings now equal what each campaign can actually contact, so an
excess waits in our outbox (`pending`, visible) instead of an invisible Instantly
backlog. `TGTC_DELIVERY_MAX_TOTAL` stays 1,100.

## Apollo and Fantastic

* Daily grant 1,000 -> **1,450 credits**, requests 3,000 -> 4,800 (09-21 hit the
  3,000-request ceiling at 986 credits). Hard rolling ceiling in code:
  `TGTC_APOLLO_ROLLING_30D_CREDITS=50000` across every daily budget.
* **No purchase.** No public add-on price (only inside the account), the signed-in
  browser was unreachable, and the API reports rate limits only, never a balance.
  Exhaustion is a free `refused` and work waits; it cannot become overage.
* Fantastic unchanged: 3,300 records/day, within the 100k/month plan. The +15,000
  authorization is unused: no measured marginal yield beyond 3,300/day yet.

## What 1,000/day still needs (derived, not observed)

At 1,450 credits, ~1,000 eligible/day is expected, but ~800-850 contacted/day:
OPERATIONS demand ~500/day against 350, FINANCE ~127 and AI_TECHNICAL ~115
against 100. Closing it: OPERATIONS ~2,000 emails/day (~103 shared senders,
above the authorized 1,400-1,600 band) and FINANCE/AI `daily_max_leads` ~135/130
inside their existing pools. No new inboxes: 5,040/day of capacity vs ~4,000
needed.

Rollback, in order: `scripts_ops/instantly_reallocate_operations.py - --rollback`;
`scripts_ops/set_core_schedule_rollback_1000.graphql`; unset
`TGTC_DELIVERY_MAX_BY_CAMPAIGN`, set `TGTC_DELIVERY_MAX_PER_CAMPAIGN=150`; push
`release-mail-domain-c260f3a` (or `pre-mail-domain-6c79aa1` and
`TGTC_CORROBORATED_MAIL_DOMAIN=0`) to `feat/rebuild-core`; redeploy.
