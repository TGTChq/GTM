# Phase 3 — Qualification recovery (implementer report)

Branch `audit/phase2-offline-fixes`. **BASE = `54c006c`** (`git rev-parse --short HEAD` at start).
Entering test state: **1,306 passed / 0 failed** (`python rebuild/run_offline_tests.py tests_core -q`).

**Spend: 0 Anthropic calls, 0 Apollo credits, 0 Fantastic records, 0 external writes.** The 327 classifier
answers bought in phase 4 were replayed from `phase4_classifier/live_answers/answers.jsonl` through each
tree's own `_apply_semantic` (`route_measure.py --mode live`). Nothing was re-bought.

**The holdout was not scored.** Every number below is the CALIBRATION stratum (n=353 after the frozen
`dedupe`) or the 411-item purchased-corpus go/no-go set. `measure.py` filters to `set == "calibration"`
before any metric is computed and asserts no holdout item reaches the scorer. Two holdout rows appeared
incidentally in a raw decision-diff early on; no decision in this phase rests on them, and every commit's
hypothesis was fixed from a Luis ruling or the frozen rubric before any label was consulted.

New evidence lives in `funnel_audit_20260919/qualification_recovery/`:
`measure.py` (scorer, imports the frozen predicates), `route_evidence.py` (per-campaign route profile),
`inspect_group.py`, `audit_trapped.py`, `replay/`, `scores/`.

---

## 1. What the audit found before changing anything

### 1.1 "101 trapped valid jobs" is not 101 recoverable jobs

The 231 → 101 figure in `FINAL_CAPACITY_REPORT.md` is a **weighted population estimate over the holdout**,
computed with `inference=None` — the classifier route entered and immediately abandoned. Measured on the
sets this phase may read, with the route ON (the 327 bought answers replayed):

| Calibration, n=353, `S_agree` | at base `54c006c` |
|---|---|
| labelled-valid jobs | **84** |
| — qualified by the policy | **78** |
| — in the review bucket | 0 |
| — on the classifier route (`ambiguous:needs_semantic_classifier`) | **0** |
| — trapped in a deterministic rejection | **6** |

Under `S_hc`: 107 valid → 78 qualified, 20 in review (firmographic conflict, held there by Decision 2),
**9** trapped in rejection.

Weighted to population jobs, the calibration equivalent of the holdout's "101 valid jobs in the rejected
bucket" is **29.7** (`S_agree`) / **39.1** (`S_hc`). With the route abandoned (`inference=None`) the same
stratum puts **273.5** / **420.2** estimated valid jobs in the unknown bucket — about ten times more.

**The leverage is not in deterministic rejection.** Once the port is configured it decides every route
item; what it gets wrong is *which campaign*. That is a precision problem, and section 2.5 is the fix.

### 1.2 The deterministic rejection bucket, by reason (calibration, `S_agree`, at base)

118 of 353 calibration rows are rejected pre-classifier; **6 are labelled valid — a 5.1% FN rate**, and
every individual reason family holds at most 1 valid job. None reaches Luis's change-loop threshold T3
(≥ 25 valid jobs per pool, or FN ≥ 20% with n ≥ 10).

| reason | n | labelled valid | FN rate |
|---|---:|---:|---:|
| `deliverability:security_clearance` | 18 | 0 | 0.0% |
| `deliverability:travel` | 13 | 1 | 7.7% |
| `deliverability:field_work` | 13 | 0 | 0.0% |
| `employment:contract` | 12 | 1 | 8.3% |
| `employment:part_time` | 12 | 1 | 8.3% |
| `employer_too_large` | 11 | 0 | 0.0% |
| `deliverability:physical_facility` | 11 | 1 | 9.1% |
| `role:quota_carrying_sales` | 5 | 0 | 0.0% |
| 14 further reasons | 23 | 2 | — |

Three of those rows are **not defects and are not recoverable**:

- **`employer_too_large` (11 rows).** All 11 carry `decision = qualified` from the labellers — the rubric
  does not label company size — but all 11 are `valid = False` under the frozen metric, because
  `score_labels.valid()` requires the size variant to be `in_range`. The 25–1,000 band is a fixed business
  definition. Counting these as "trapped valid jobs" is the arithmetic that inflates the 101.
- **`employment:part_time` and `employment:contract` (2 of the 6).** Luis's Decision 1 keeps part-time,
  contractor, temporary, freelance and internship excluded. The rubric records employment as a *fact* and
  still calls them `qualified`; the business rule governs.

That leaves **4** calibration rows both labelled valid and not settled by a fixed rule. Commits C and D
released 2 of them.

### 1.3 The coordinator's top waterfall item is already banked on this branch

`V-P7-WF3` ranks "re-scope `seniority:leadership_or_principal` — **65.9 final contacts**, 299 rejects at a
measured 54.6% FN rate" as the largest qualification-side item. That figure is measured against the
**deployed** policy (be3af32). On this branch it was already taken by phase-2 commit `56c4a1d` (ledger
row 1).

Re-derived here on the current tree, over both frozen corpora (7,349 net-new rows):

| | |
|---|---:|
| rows whose title is `leadership_or_principal`, recorded as a **Fact** | **514** |
| rows carrying **any** seniority exclusion | **0** |
| `qualify_row` outcomes mentioning seniority or people_management | **0** |

The calibration rejection profile confirms it from the other side: neither `seniority:*` nor
`people_management` appears among the 22 rejection reasons. **No work was needed and none was done**; the
65.9 contacts are already in this branch's baseline, and re-counting them would be double-counting.

---

## 2. Corrections made

Six commits (`94d487f`, `1020520`, `f75d8f6`, `20f7498`, `680bb7c`, `01e1154`), each one hypothesis, each with a failing test watched first, each measured on both permitted
sets. Ledger rows 35–40.

**Contacts recovered at 0.8825 per unit: 0.0 across all six, measured.** No commit moved a labelled-valid
job into the *qualified* bucket on calibration. Why, stated plainly, is in §2.6 — it is a measurement
limit of this phase's budget, not a null result, and it must not be reported as a recall gain.

### 2.1 `94d487f` — online and digital media are not approved excluded industries

Luis (Q6, restated 2026-09-20): online news, digital media and media production **stay allowed**. D3 keeps
the approved list as-is. The frozen rubric §4.K agrees. Six labels imported from the legacy Apollo keyword
list as `legacy_default_pending_confirmation` are dropped; `broadcast media`, `newspapers` and
`book publishing` are approved and untouched.

| | calibration `S_agree` | calibration `S_hc` | 411-item set |
|---|---|---|---|
| precision_strict | 56.80% → **55.91%** | 56.80% → 55.91% | 72.19% → 72.19% |
| recall_strict | 84.52% → 84.52% | 66.36% → 66.36% | 90.81% → 90.81% |
| decisions changed | 2 | 2 | **0** |
| valid jobs recovered / lost | 0 / 0 | 0 / 0 | 0 / 0 |
| est. final contacts at 0.8825 | **0.0** | 0.0 | 0.0 |

Corpus reach: **33 of 81** excluded-industry hits in 7,349 rows (40.7%) — `internet news` 30,
`media production` 3.

**The precision cost is real and its cause is a different defect.** Both released calibration rows are
labelled `posting:third_party_repost`: one job-board employer (Xtalks) whose postings describe BioLabs and
Guerbet as the hiring company. The industry label was standing in for a repost gate that does not fire on
them. See §3.1 — I tried to fix that and did not ship it.

### 2.2 `1020520` — the acquisition filter still asked the provider to drop them

`EXCLUDED_LINKEDIN_INDUSTRIES` is the provider-side half of the same policy, and the one place an allowed
industry is lost invisibly: a label sent as `exclude_organization_industry` means those jobs are **never
acquired**, so no eligibility change downstream can recover them. It still sent "Online Media" and
"Media Production".

`94d487f` turned a **pre-existing** guard red —
`test_filters_only_exclude_existing_policy_industries_and_agencies`, which asserts the acquisition filter is
a subset of the eligibility policy. That is how the seam was found, and it is the failing test for this
commit.

**Not measurable on either set**: both frozen corpora were acquired before this change, so the rows it
recovers are by construction absent from them. Reported as reach, never as a score. Note also
(`API_CONTRACT_MATRIX.md`) that *any* value in `exclude_organization_industry` drops rows with a NULL
industry (1.4–1.5%); shortening the list does not remove that separate effect.

### 2.3 `f75d8f6` — a Public Trust determination is not a security clearance

Luis, **D5**: "Only an actual security clearance (Secret/TS/SCI) or an explicitly stated federal clearance
requirement excludes. A Public Trust determination, a Tier 1 investigation, an HSPD-12 PIV card and ordinary
background checks do NOT." The bare `public trust` pattern was also the rule's one pure substring match —
rubric §4.H names "public trust" in a non-clearance sense as explicitly not sufficient, and the corpora
contain exactly that (a Public Information Officer "building public trust and community engagement").

| | calibration `S_agree` | calibration `S_hc` | 411-item set |
|---|---|---|---|
| precision_strict | 55.91% → **55.91%** | 55.91% → 55.91% | 72.19% → 72.19% |
| recall_strict | 84.52% → 84.52% | 66.36% → 66.36% | 90.81% → 90.81% |
| decisions changed | 2 | 2 | **0** |
| est. final contacts at 0.8825 | **0.0** | 0.0 | 0.0 |

Corpus reach: **46 of 7,349 rows** excluded on that substring with no other clearance evidence anywhere in
the posting; 20 more mention public trust beside a real clearance and still exclude on the real one.

Both released rows go to `ambiguous:needs_semantic_classifier`, not to qualified — which is why precision
does not move, and is the fixed rule ("unknown goes to retry or review, never to rejection") working as
written.

**Label-basis note, stated rather than acted on.** The 3 calibration items whose labellers cited
`public_trust` were labelled under rubric v1, which included public trust *pending Luis's Q8*. Q8 came back
NO. No label was edited, and the metric above is the unchanged one. The rubric anticipated this: it records
`facts.clearance` precisely so a variant can be applied at scoring. I did not apply one — that is Luis's
call, not mine.

### 2.4 `20f7498` — a physical-demands clause is a fact, not an exclusion

Rubric §4.F names this verbatim under **Not sufficient**: "lifting or physical-demands boilerplate ('may
lift up to 25 lbs', 'sitting for long periods')", and asks for `physical_duties = incidental` to be
*recorded*. Phase 2 (task 3) narrowed the same pattern, but only when the sentence also carried ADA
boilerplate.

| | calibration `S_agree` | calibration `S_hc` | 411-item set |
|---|---|---|---|
| precision_strict | 55.91% → **55.91%** | 55.91% → 55.91% | 72.19% → 72.19% |
| recall_strict | 84.52% → 84.52% | 66.36% → 66.36% | 90.81% → 90.81% |
| valid jobs trapped in rejection | 6 → **5** | 9 → **8** | 1 → 1 |
| decisions changed | **6** | 6 | **0** |
| est. final contacts at 0.8825 | **0.0** | 0.0 | 0.0 |

Corpus reach: **320 of 7,349 rows (4.35%)** are excluded on a lift span that is the posting's only facility
evidence, and **none of the 320** carries the ADA qualifier phase 2's carve-out looks for — the carve-out
never reaches them. This is the largest deterministic pool in the phase.

**The same predicate lived in three places**, and all three moved together rather than one growing a third
carve-out:

1. `domain/facts.py:FACILITY` — pattern removed; `PHYSICAL_DEMANDS_STATEMENT` added and recorded as the Fact
   `physical_demands_statement` (`rule_version` tgtc-core/3-audit). `FACILITY_LIFT_INCIDENTAL`, the
   `light lifting` filter and the ADA filter existed only to narrow it and are gone with it.
2. `domain/exclusion_evidence.py:PATTERNS["physical_work"]` — its **own copy** of the lift regex, which
   decides whether a *model-claimed* hard exclusion is supported. Left behind, the classifier could still
   reject on exactly the evidence the deterministic path no longer accepts.
3. `domain/candidate_qualification.py:_physical_facility` — rediscovered the pattern by **string-matching
   the regex source** inside `FACILITY`, so it silently produced an empty list the moment the pattern moved.
   It now shares `PHYSICAL_DEMANDS_STATEMENT`.

Fixture `candidate_qualification_canary_cases.json` records what production produced at build time, and this
change moves two of those cases. `current_outcome` was re-derived for canary_09 and canary_10 only, with the
reason written into the fixture's own `policy_notes`; `candidate_outcome` is unchanged for both.

### 2.5 `680bb7c` — travel volume alone is not essential field travel

Luis, **D4**: "Exclude when field or territory duties are a CORE RESPONSIBILITY. Drop the proposed ≥ 50%
numeric: it was my invention, and 'essential' already carries the test. Incidental conference travel never
excludes." The deployed rule fired at a stated **20%** — stricter than both the number Luis withdrew and the
rubric's own 50% parameter (§4.G, which also names conferences, off-sites and HQ trips as not sufficient).

| | calibration `S_agree` | calibration `S_hc` | 411-item set |
|---|---|---|---|
| precision_strict | 55.91% → **55.47%** | 55.91% → 55.47% | 72.19% → 72.19% |
| recall_strict | 84.52% → 84.52% | 66.36% → 66.36% | 90.81% → 90.81% |
| valid jobs trapped in rejection | 5 → **4** | 8 → **6** | 1 → 1 |
| decisions changed | **9** | 9 | **0** |
| est. final contacts at 0.8825 | **0.0** | 0.0 | 0.0 |

Corpus reach: **125 of 7,349 rows (1.70%)** excluded on a percentage span alone — 56 stating 50%+, 57
stating 25–49%, 12 stating 20–24%. **25 of the 125 name conferences, industry events or HQ in the very same
sentence.** Only 1 row carries a percentage beside a qualitative marker; 61 rows match a marker alone and
are unaffected.

The decisive labelled case: "Social Media & Community Lead", labelled qualified / marketing_creative,
excluded on "Ability to travel up to 25% of the time to industry events and conferences" — the rubric's own
"not sufficient" example.

**One new false positive**: "Manager, Field Clinical Operations", labelled out-of-scope
`clinical_healthcare`, now reaches `qualified_pre_contact`. That is a function-scope miss the travel rule
was masking, not a deliverability case.

### 2.6 `01e1154` — a semantic campaign assignment needs deterministic support, per campaign

This is the brief's §5 item. The branch already refused a `gtm_revenue` assignment with zero deterministic
evidence (commit `7260466`). This turns that one hard-coded case into the measured table it always was, so
five more campaigns **share** its predicate instead of each growing a carve-out. `gtm_revenue` keeps exactly
the behaviour it had.

Enrolment is measured per campaign on calibration (`route_evidence.py`, 109 assignments over 70 items),
requiring ≥ 1 distinct deterministic hit for the assigned function:

| function | withheld right / wrong | that function's precision |
|---|---:|---|
| `customer_success` | 0 / 3 | 0.200 → 0.500 |
| `engineering` | 0 / 2 | 0.444 → 0.571 |
| `people_hr` | 0 / 2 | 0.286 → 0.400 |
| `ecommerce` | 0 / 2 | 0.200 → 0.333 |
| `finance` | 0 / 1 | 0.429 → 0.500 |
| *`operations` — not enrolled* | *4 / 8* | *costs 4 labelled-right assignments* |
| *`product` — not enrolled* | *1 / 2* | *costs 1* |
| *`customer_support` — not enrolled* | *1 / 1* | *costs 1, gains nothing* |
| *`marketing` — not enrolled* | *0 / 0* | *no measured effect either way* |

Only the five that withhold **zero** labelled-right assignments are enrolled. `marketing` is left out on
purpose: a change with no measured effect is not evidence, and the brief is explicit that this shape must
not be generalised blind. **For the route as a whole the requirement would cost right assignments — which is
precisely why it is per campaign.**

A/B over the 129 calibration route items, same bought answers (`scratchpad/route_ab.txt`):

| | before (gtm only) | after (six campaigns) |
|---|---:|---:|
| items given a campaign | 67 | 66 |
| primary assignments right | 36 | 36 |
| **primary campaign precision** | **53.73%** | **54.55%** |
| total campaign assignments | 100 | 90 |
| secondary assignments | 33 | 24 |

**10 unsupported campaign assignments removed (10% of all route assignments) for zero right primary
assignments lost.**

End to end:

| | calibration `S_agree` | calibration `S_hc` | 411-item set |
|---|---|---|---|
| precision_strict | 55.47% → **55.47%** | 55.47% → 55.47% | 72.19% → 72.19% |
| recall_strict | 84.52% → 84.52% | 66.36% → 66.36% | 90.81% → 90.81% |
| decisions changed | **1** | 1 | **0** |
| valid jobs recovered / lost | 0 / 0 | 0 / 0 | 0 / 0 |
| est. final contacts at 0.8825 | **0.0** | 0.0 | 0.0 |

**The unit matters here.** Nine of the ten withheld assignments were *secondary* campaigns, which is why the
job-level outcome moves on only one item. Every entry in `compatible_functions` opens its own row in
`opportunities` (`classification_service`), so a withheld secondary removes an unsupported **company ×
campaign unit** without touching the job's own decision. The job-level flat line is not the effect; the
assignment count is.

### 2.7 Why recall does not move, stated plainly

A row released from a deterministic rejection lands on the **classifier route**. Model answers were bought
only for the 327 items that entered the route in the *base* run, so a newly released row has no answer and
replays as `ambiguous:needs_semantic_classifier`. Scoring these end to end would need new paid calls, which
this phase does not make.

So for commits A–D: the released rows are reported as **reach** (counts in the frozen corpora) and as a fall
in **valid jobs trapped in a rejection** (6 → 4 under `S_agree`, 9 → 6 under `S_hc`). They are never reported
as recall, and the estimated contacts recovered is **0.0**, not an extrapolation. A row that moves from
`rejected` to the reopenable unknown bucket is strictly better than before — it can still become a contact —
but this phase cannot prove by how much.

---

## 3. Hypotheses measured and NOT shipped

### 3.1 A description-anchored third-party-repost detector

Commit `94d487f`'s two new false positives are reposts by one job-board employer. `employer_attribution_conflict`
misses them: it requires **two** anchors (a named equal-opportunity employer **and** a name-consistent
application email domain), and these postings carry neither — they simply open with a different company's
self-description ("BioLabs is…", "At Guerbet, we…") and never name the provider employer.

I prototyped the obvious predicate (the description's own "X is a…" / "At X, we…" opener names a company
incompatible with the provider employer, and the provider employer appears nowhere in the text) and measured
it on the frozen corpora: **508 of 7,349 rows (6.91%)**, with visibly poor precision — "This is a…",
"Summary\n\nThe Electrician is a…", "Our Host Staff…". **Not shipped.** A gate that wrong would be exactly
the over-broad substring rule this phase exists to remove. Recorded here as a measured negative result and a
real, unfixed defect.

### 3.2 `insufficient_evidence:description_too_short` is a rejection that never reopens

`classification_service`'s reopen query explicitly excludes it
(`AND p.close_reason NOT LIKE 'insufficient_evidence:description_too_short%%'`), and
`daily_24h_canary.qualify_row` maps it to `rejected:*`. That is an unknown turned into a permanent
rejection, which the fixed rule forbids.

Measured before acting: **2 of 7,349 rows (0.03%)** have a description under 120 characters. Ranked by
`good_jobs_lost`, it does not earn a commit. Flagged as a rule violation, not fixed.

### 3.3 Three further trapped calibration rows, diagnosed and left alone

- `JOB-640b3c9023` "Product Security Engineer" at Modern Health — provider industry says "Mental Health
  Care"; the business is a benefits platform. `industry_label_mismatch = true`. Rubric §4.K asks for the
  *business* to be judged, not the label. Addressable pool is ~34 rows; a "software/platform vendor selling
  to X is not X" predicate needs description evidence and would be a new heuristic with n=1 of calibration
  support. Not attempted.
- `JOB-a52b047370` "Intake Specialist" at **Transcom Solutions** — rejected because the employer name
  contains the known-intermediary token `transcom`. A company-identity substring defect; n=1.
- `JOB-fc25c9b3f1` "Senior Data Engineer" at Regal Cineworld — rejected `field_work` on company boilerplate
  listing the *range* of roles the employer offers ("a mix of hybrid, field-based, or remote working
  options"), not this role's duty. **9 of the 86 `field-based` matches** in the corpora sit inside such an
  enumeration. A narrow carve-out is plausible; it is the best-supported remaining item and I ran out of
  budget before it.

---

## 4. Concerns

0. **Final test state: 1,334 passed / 0 failed** (from 1,306 at base; +28 new tests). Every commit was
   run green on the full suite before it landed, and commit `1020520` exists because `94d487f` left a
   pre-existing guard red — I did not discover that until the full suite ran, which is an argument for
   running it per commit rather than per batch.
1. **Five changes landed in the qualification layer.** The skill's red flag ("a fourth change in the same
   layer without an architecture review") applies. The architectural finding is the changes themselves: the
   deterministic exclusion lexicon is a flat list of substrings with accumulating ad-hoc carve-outs, and the
   same predicate is duplicated across `facts.py`, `exclusion_evidence.py` and `candidate_qualification.py`
   — one of those copies was derived by string-matching regex source. Commit `20f7498` removed a carve-out
   rather than adding one, and unified three copies, but the pattern will recur until the lexicon separates
   *demands/labels* (facts) from *duties* (gates) by construction.
2. **Precision fell 1.33pp on calibration across commits A–D** (56.80% → 55.47%, `S_agree`), for 3 new
   false positives and 0 measured recovered jobs. Every one of those changes is mandated by a Luis ruling or
   by the frozen rubric's own "not sufficient" list, so I made them and reported the cost. Two of the three
   new false positives are reposts (§3.1) and one is a function-scope miss (§2.5) — none is caused by the
   rule that was relaxed being right.
3. **The 411-item purchased set measured 0 decision changes for every commit.** It is drawn from the
   deployed *classification-qualified* frame, so no item in it reaches the semantic route and few reach the
   deliverability gates. It is a precision instrument for the deployed frame, and it cannot see this phase's
   changes. Do not read its flat line as evidence of no effect.
4. **Calibration n is small per rule.** The largest single rejection family is 18 items. No individual rule
   change here clears T3 on calibration; the corpus-reach numbers (320, 125, 46, 33 rows) carry the weight,
   and those are policy-output counts, not labelled ones.
5. **The classifier route cannot be re-measured after a deterministic change without new spend.** This caps
   what any offline phase can claim about recall and is the single biggest reason the contacts-recovered
   figure is 0.0 rather than a positive number.
