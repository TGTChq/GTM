# Phase 4 — contact discovery: implementer report

**BASE** `ce5c44d` (branch `audit/phase2-offline-fixes`, worktree `C:\TGTC\tgtc_rebuild`).
**HEAD** `8070e45`. `rule_version = "tgtc-core/3-audit"` (`domain/facts.py`, unchanged; every new gate
decision stamps it).

**Spend: 0.** No Apollo call of any kind was made in this session — not free, not paid. No Fantastic,
Anthropic, Airtable, Instantly, Railway write, deploy or outreach. The reason the free searches were
not made is a permission refusal, §5; it is the one thing this phase owes and could not deliver.

---

## 1. Status

Four commits, one hypothesis each, full suite green before each.

| SHA | change | reachable by the live pipeline? |
|---|---|---|
| `4318498` | the shared title normaliser (`domain/title_norm.py`) | **yes** — `pre_enrichment_check` and `evaluate_contact` both route through `title_matches` |
| `424a6af` | three distinct personas for all nine campaigns (`TALENT_PEOPLE_BUYER_TITLES`) | **yes** — `buyer_titles()` is both what the search SENDS and what the gates ACCEPT |
| `a7d4485` | deterministic ranking before enrichment (`domain/contact_ranking.py`) | **yes** — the order `process()` walks before every paid `people/match` |
| `8070e45` | never pay twice for a person whose answer is already stored | **yes** — in `process()`'s candidate loop, immediately before the paid match |

**Tests.** `python rebuild/run_offline_tests.py tests_core -q`: **1,334 → 1,493 passed / 0 failed**
(+159). The legacy suite `python -m pytest tests -q -p ci_no_network`: **3,754 passed / 0 failed** at
BASE and at HEAD. (Without `-p ci_no_network` three tests in `tests/test_offline_network_guard.py` fail
at BASE as well — the plugin is the documented invocation, not a regression.)

---

## 2. The five quantities, never collapsed

Frame: the **same** 100 units Stage 2a froze. `unit_frame.csv`
sha256 `98b94b89…6b7039`, selected-id digest `9785e4f7…153777`, unchanged. Re-scored offline over
Stage 2a's own saved free-search record `phase5_apollo_2a/state/dropscan.jsonl`
(sha256 `1fe364ee…3a61511`), which holds every person the free search returned, per unit.

| quantity | status | value |
|---|---|---|
| **1. candidate found** | **NOT MEASURED** — live free search only | see §5 |
| **2. mapper accepted** | **MEASURED**, 0 calls | **575 → 607** of 615 returned people |
| **3. verified work email** | projection (needs paid enrichment) | Stage 2a: 90/101 = 0.891 |
| **4. correct contact** | projection (needs paid enrichment) | Stage 2a: 98/101 = 0.970 |
| **5. net-new contact** | projection (needs paid enrichment) | Stage 2a: 99/101 = 0.980 |
| **6. outreach-eligible contact** | projection | Stage 2a: 88/101 = 0.871 cleared every gate; outreach additionally needs a documented legal basis (satisfied: the frame is US-only) and a configured Instantly route (not observable offline) |

Quantity 1 is the one that matters most and it is the one this session could not buy. Adding the third
persona changes the `person_titles[]` the search **sends**; Stage 2a's saved response was produced by
the old query and cannot contain people it never asked for.

### Before / after candidate availability — the whole point of the phase

Three arms, the same 615 returned people, only the selector varying. `be3af32` is Stage 2a's own
figure for deployed production; `ce5c44d` is this branch's base, which already carries phase 2's title
fixes and is therefore the **like-for-like** before.

| | deployed `be3af32` | **BEFORE** (`ce5c44d`) | **AFTER** (`8070e45`) |
|---|---:|---:|---:|
| mapper accepted (people) | 513 | 575 | **607** |
| units with ≥ 1 **candidate** | 61 | 66 | **72** |
| units with ≥ 2 **candidates** | 40 | 47 | **51** |
| units with ≥ 3 **candidates** | 30 | 36 | **39** |
| units with ≥ 1 **distinct persona** | — | 66 | **72** |
| units with ≥ 2 **distinct personas** | — | 26 | **27** |
| units with ≥ 3 **distinct personas** | — | 1 | **1** |
| `function_or_authority_mismatch` | 70 | 37 | **2** |

Evidence: `phase6_contact_discovery/replay_dropscan.py`, `state/replay_base_ce5c44d.json`,
`state/replay_c3_ranking.json`. Reproduce with
`PYTHONPATH=<tree> PYTHONDONTWRITEBYTECODE=1 python replay_dropscan.py <arm>`.

### The finding that changes the ceiling

**Candidate availability and role-diverse availability are not the same number, and only the second
one can be spent.** `select_next_contact` selects only a persona not already held — Luis's ruling that
three people in one role are not diversification. A unit with four accepted candidates all titled some
variant of "Finance Director" supplies **one** contact, not four.

- 51 units have ≥ 2 candidates. **27** have ≥ 2 personas.
- 39 units have ≥ 3 candidates. **1** has 3 personas.

Stage 2a's free-stage ceiling (1.01 at depth 2, 1.31 at depth 3, and 1.17/1.53 with a normaliser)
counted **candidates**. Measured by personas, the ceiling is **1.00 contacts per unit** after this
phase (0.93 before). Every one of those is still far below the 1.6412 target — but the gap is larger
than Stage 2a reported, not smaller.

Distribution of accepted personas per unit, after (before in brackets):

| personas present | units |
|---|---:|
| none | 28 (34) |
| executive leader only | 32 (30) |
| functional owner only | 12 (9) |
| owner + executive | 26 (25) |
| TA/People only | 1 (1) |
| all three | 1 (1) |

### Projection, labelled as one

Role-diverse availability × Stage 2a's measured per-contact conversion (88 of 101 paid matches were a
correct, verified-work-email, historically net-new contact). Interval: two-stage bootstrap, 20,000
reps — companies resampled (100 units on 99 company clusters, so the cluster is the **company**), and
the conversion resampled from Stage 2a's own 101 outcomes. `phase6_contact_discovery/project.py`,
`state/projection.json`.

| | BEFORE `ce5c44d` | AFTER `8070e45` |
|---|---:|---:|
| role-diverse ceiling, contacts/unit (**measured**) | 0.930 | **1.000** |
| projected correct verified net-new contacts/unit, portfolio-weighted | 0.8168 | **0.8747** |
| 95 % CI | [0.6769, 0.9730] | **[0.7336, 1.0273]** |
| target | 1.6412 | 1.6412 |

The after point estimate lands within 0.008 of Stage 2a's **measured** 0.8825, which is a consistency
check on the method, not a second measurement. **The upper bound is 1.0273; the target is 1.6412.**

Assumptions this projection rests on, all unvalidated: Stage 2a's conversion was measured on contacts
picked without persona diversity enforced, so applying it per persona assumes conversion is
persona-independent; no third contact was ever bought, so depth 3's conversion is entirely unmeasured
(it moves 1 unit in 100, so it cannot matter here); and the 100 units are deterministically qualified,
not label-confirmed.

**I did not extrapolate a third-contact yield from candidate availability.** Availability is an upper
bound on depth and nothing else — and the persona split above is precisely why the candidate-count
version of that bound is misleading.

### Per campaign (units with ≥ 1 / ≥ 2 / ≥ 3 distinct personas)

| campaign | n | before | after |
|---|---:|---|---|
| finance | 43 | 33 / 13 / 0 | 34 / 15 / 0 |
| ai_technical | 18 | 14 / 6 / 0 | 15 / 6 / 0 |
| marketing_creative | 16 | 9 / 3 / 0 | 9 / 3 / 0 |
| people_hr | 14 | 7 / 4 / 1 | 9 / 3 / 1 |
| product | 4 | **0 / 0 / 0** | **1 / 0 / 0** |
| operations | 2 | 2 / 0 / 0 | 2 / 0 / 0 |
| customer_experience | 1 | **0 / 0 / 0** | **1 / 0 / 0** |
| ecommerce | 1 | 0 / 0 / 0 | 0 / 0 / 0 |
| gtm_systems | 1 | 1 / 0 / 0 | 1 / 0 / 0 |

`product` scored 0 of 4 in Stage 2a and was singled out there as the cell worth re-testing: the
normaliser recovers one of the four (a spelled-out "Chief Technology Officer" and two "Head Product …"
titles). `customer_experience` recovers its single unit ("SVP of Customer Experience"). `operations`,
`ecommerce`, `customer_experience`, `gtm_systems` are **counts, not rates** — n ≤ 2 by design.

---

## 3. What each change actually did, with its cost

### Title normaliser (`4318498`)

`gates.py` held phase 2's contiguous-phrase matcher. A phrase search cannot bridge word order:
"Director of Human Resources" and "Director Human Resources" contain each other in neither direction.
The predicate moved to `domain/title_norm.py` — one module, three callers, plus
`opportunity.contact_persona` which was already reading phase 2's copy — and gained a token-set rule
guarded three ways.

+34 people gained, −2 lost, net +32. The 34 are ordinary: `director human resources`,
`director of financial reporting` ×3, `chief executive officer` ×5 spelled out, `avp finance policy
controls`, `svp of customer experience`, `vice president of ai integration`.

**Two admissions are questionable and are reported, not hidden**: "VP Financial Institutions" and
"Head of Financial Crimes and Fraud" reach the finance hierarchy through the `financial → finance`
equivalence. Neither is the finance owner. 2 of 34.

**Two deliberate narrowings, both counted:**

1. the operations over-match guard now fires symmetrically ("Operations Manager, Warehouse" as well as
   "Warehouse Operations Manager") — the token rule has no notion of "the word immediately before";
2. a title that says the person is not an employee in this role now ("Former", "Fractional",
   "Consultant") never matches. **2 of 513** accepted candidates: a consulting accounting manager and
   a "Founder, Fractional CFO". At unit level that costs **one people_hr unit its second persona** —
   the fractional CFO was its only executive-tier candidate. That is the whole measured cost, and it
   is arguable: a fractional CFO is often the person who decides, but they are a contractor, not an
   employee at the true hiring company, and "unknown or ambiguous records are never approved" points
   the conservative way.

`is_department_label` fires once in the frame (a person whose Apollo title is the bare word
"Marketing"). It changes no outcome — the title matched nothing before either — it splits one loss
bucket into two so the accounting stops calling "no function evidence at all" a "wrong function".

### Three personas for all nine campaigns (`424a6af`)

`contact_persona` knew three personas for **one** function key, from a two-title tuple hard-coded in
`services/opportunity.py`. `opportunity.py`'s own termination rule documented the consequence: "9 of
10 functions expose exactly 2 reachable personas". Depth 3 was structurally unreachable outside
people_hr whatever Apollo returned, and the shipped default quota of 3 could never be met.

`TALENT_PEOPLE_BUYER_TITLES` is now the third persona for every function key and is searched for every
function key. people_hr's own "Talent Acquisition Director" / "Head of Talent Acquisition" **moved out
of** its direct and executive lists into the new one: the searched set is identical, but a TA leader is
now people_hr's third persona instead of a second spelling of its functional owner. The three lists are
disjoint per function and a test asserts it for all ten keys.

The list is short (4 titles, 6 outside people_hr) and ordered last, because these titles are OR'd into
`person_titles[]` and compete for the same two pages of results as the campaign's own owners.

**This change scores 0 offline, by construction** — the saved response was produced by the old query.
It is the single reason §5's live run matters.

### Deterministic ranking (`a7d4485`)

`_rank` keyed on the matched title index alone. Three consequences: it was not a total order, so every
tie fell through to Apollo's own response order; the founder demotion sat in a branch
`pre_enrichment_check` makes unreachable, so "Founder & CTO" inherited the CTO's rank and was bought
ahead of the real Director of Engineering; and employer evidence was not a signal at all, although
`email:domain_not_employer` was Stage 2a's largest post-enrichment loss (9 of 101).

The order is now: founder last, buyer-list preference, exact employer domain, compatible employer name,
`person_ref` as the tiebreak that makes it total. Evidence breaks ties **within** a function and never
re-orders the functions. The free reuse path ordered by `email_verified_at DESC` while the paid search
path ordered by title; both now use the one order.

It changes **nothing** about what is accepted — 607 either way — and everything about which credit is
spent. In `test_nine_routes_end_to_end` the wrong-company candidate now sorts last and is never bought:
**20 paid matches instead of 30** across ten routes, same ten leads delivered.

**Six existing tests changed, and that is the finding**: they pinned a sequence that held only because
their fixtures happened to be listed in that order. Their candidate ids now express the intended order
through the ranking's own rule.

### Never pay twice (`8070e45`)

Three guards existed and each covered a neighbouring case: `_excluded_refs` is scoped to one
opportunity, the approval index covers only people already approved, and `_reusable_person` queries
`email_status = 'verified'` so it only catches purchases that **succeeded**. The gap: a person paid for
and then rejected. An opportunity is employer × **function**, so one employer hiring in two campaigns
is two opportunities and the same person can be a legitimate candidate for both — and the previous
commit makes that the normal case, since the Talent/People owner is now searched for all nine.

Reproduced red first (2 paid matches, one person), fixed, green (1). Restricted to stored evidence
about **this** employer, and released after `person_evidence_ttl_days`, so it is a deferral, not a
blacklist.

---

## 4. The rest of the audit list — verified, not changed

| item | finding |
|---|---|
| current vs former employer | `current_employment_evidence` post-enrichment; the title-level half is new in `4318498` |
| parent / subsidiary / alias | `employer_aliases` (`_alias_domains`) ∪ `corroborated_alternate_domains`, which requires the Apollo organization to be **proven** the employer (name-compatible or same org id); similarity alone never adds a domain |
| exact and validated alternate domains | `evaluate_email` accepts `EXACT_EMPLOYER_DOMAIN` or `CORROBORATED_ALTERNATE_EMPLOYER_DOMAIN` only |
| free and personal email | `FREE_MAIL_DOMAINS` on both sides — `safe_employer_domain` refuses a free-mail host as an employer domain, `email_on_domains` refuses a free-mail candidate address; `is_generic_mailbox` covers `info@`; `reveal_personal_emails=false` and `reveal_phone_number=false` on every match |
| verified work email | Apollo `verified` is the only authority; nothing promotes unverified/extrapolated/unknown |
| reuse across openings in one company × campaign | needs no rule: the opportunity **is** employer × function, so those openings are one opportunity by construction |
| historical person and email suppression | `person_email`, `company_function` and `account` suppressions, checked before approval; `_excluded_refs` carries suppressed emails into the candidate loop |
| duplicate enrichment | the gap found and fixed in `8070e45` |
| campaign-specific suitability | per-function `buyer_titles`, now three personas deep for all nine |

---

## 5. What is missing, and why — the free-search measurement

**The live free-search A/B was not run. `APOLLO_API_KEY` could not be read in this session.**

It exists only in the Railway service variables for GTM Core Canary 1000. Two attempts to read it —
the read-only GraphQL helper `tgtc_canary_evidence/rq_readonly.py` that Stage 2a itself used, and a
check for a local `.env` — were both refused by the harness with *"Permission for this action was
denied by the Claude Code auto mode classifier. Reason: [Production Reads]"*. `os.environ` carries no
key either. I stopped rather than look for a way around it.

The harness is written, reviewed and left ready:
`phase6_contact_discovery/live_free_search.py` (`preflight` → `search` → `report`). It names only
`/mixed_people/api_search` and `/usage_stats/api_usage_stats`; there is no reference to `people/match`
or `organizations/enrich` anywhere in it. It re-verifies live that search bills 0 before any volume
(usage_stats before/after inside one minute, the paid counter must move by 0, no unmasked address),
logs every call, and stops at a hard cap of 1,200. **It has never been executed**, and no number in
this report comes from it.

The control arm needs no calls at all: `buyer_titles()` sent the identical title list at `be3af32` and
at `ce5c44d` (phase 2 changed the matcher, not the lists), so Stage 2a's saved response **is** the
control. Only the treatment arm has to be searched — ~200–400 free calls for all 100 units, well
inside the cap.

To unblock: grant the read of the Railway service variable, then run the three subcommands from
`C:\TGTC\tgtc_rebuild`.

---

## 6. Concerns

1. **The decisive number is still unmeasured.** Everything above holds the returned people fixed.
   Whether adding the Talent/People persona actually *finds* anyone — and whether it **crowds out** the
   campaign's own owners inside the two-page, 100-per-page search budget — is unknown. Crowding-out is
   a real risk in the direction that would make things worse: the titles are OR'd, Apollo's result
   ordering is undocumented, and the ranking only re-orders what came back. This is the first thing the
   live run must check, not just the availability delta.

2. **Role-diverse availability, not candidate count, is the binding constraint, and it is worse than
   Stage 2a reported.** 51 units have a second candidate; 27 can supply a second *contact*. The ceiling
   is 1.00 contacts/unit against a target of 1.6412, and no matcher improvement can move it much: 26 of
   100 units return **nobody at all** under the verified-email search filter, and 44 more return only
   one persona. The lever with real headroom is the search configuration itself — Stage 2a's untested
   `contact_email_status=verified` lever, more pages, or the organization-id selector — not the
   selector.

3. **A latent wait-forever path, found and deliberately not fixed here.** When the candidate list
   empties by *consumption* rather than by persona exhaustion, `no_further_candidates` stays False and
   the opportunity requeues at `buyer_search_pending` — where the next pass finds the same people, all
   now excluded, and waits again. Phase 2's fix round 2 closed the persona-exhaustion path and left
   this one. It was invisible before this branch because ranking order happened to route these cases
   through the other branch; `test_nine_routes_end_to_end` changed from `wait` to `approved` for
   exactly this reason. It is a fifth change in the same layer and a separate hypothesis, so it is
   reported rather than bundled. It matters: an opportunity stuck there can never reach depth 2 or 3.

4. **Four changes in one layer** (`opportunity.py` / `gates.py` / `campaigns.py`), which is the skill's
   own red flag. The architectural observation behind them: buyer-title *data*, the title *matcher*,
   the persona *classifier* and the *ranking* were four statements of one policy — "who do we contact
   at this company, in what order" — spread across three modules, and two of them had already drifted
   into private copies. This batch consolidated the matcher and the ranking into `domain/` and made the
   persona classifier read the same data the search sends. `candidate_qualification._ABBREVIATIONS` is
   a fourth, still-separate abbreviation table (for job-posting titles, not contact titles); it was not
   touched and should be reviewed next.

5. **`campaigns.POLICY_VERSION` was deliberately not bumped.** It stamps `Validation Version` on every
   Airtable row and keys the inference cache, so changing it has delivery-visible side effects outside
   this phase's scope. The new decisions stamp `facts.RULE_VERSION = "tgtc-core/3-audit"` as instructed.
   Someone should decide whether a persona-policy change ought to move the policy version too.

6. **Apollo's free search returns neither `departments` nor `seniority`** — empty on all 512 candidates
   Stage 2a saved. The "department versus job title" evidence the brief asked for therefore has exactly
   one reachable half (`is_department_label`, on the title Apollo does return). I wrote the other half,
   found it had no input, and deleted it rather than ship a dead signal.

7. **`contact_mapping.py` remains four-group scoped and unwired.** It shares this phase's base
   normalisation and non-decision-maker list, so those cannot drift, but its `GROUPS` / `offer_group`
   vocabulary is still the void four-group scope. Nothing in the live path reads it.

8. **The holdout was not touched.** Nothing here was scored against it.

---

## 7. Files

| path | contents |
|---|---|
| `tgtc_core/domain/title_norm.py` | the one title normaliser and the predicates the gates and ranking share |
| `tgtc_core/domain/contact_ranking.py` | the total order a paid call is spent along |
| `tgtc_core/policy/campaigns.py` | `TALENT_PEOPLE_BUYER_TITLES`, the third persona for all nine |
| `tests_core/test_phase4_title_normaliser.py` · `test_phase4_personas.py` · `test_phase4_ranking.py` · `test_phase4_duplicate_enrichment.py` | 159 new tests |
| `…/funnel_audit_20260919/phase6_contact_discovery/replay_dropscan.py` | the offline A/B replay, 0 provider calls |
| `…/phase6_contact_discovery/project.py` | the five quantities, the projection and its bootstrap interval |
| `…/phase6_contact_discovery/live_free_search.py` | the free-search harness — **written, never executed** |
| `…/phase6_contact_discovery/state/` | `replay_*.json`, `projection.json` |
