# Experiments, evaluation sets and the change loop

## Campaign × geography experiments

- Measure each of the **9 campaigns × 5 geographies** separately and per provider source.
- Use zero-job-credit `-count` probes first. Nothing is bought until the count matrix is complete.

**Arms, per cell:**

| Arm | What it is |
|---|---|
| A | Primary taxonomy only (`ai_taxonomies_a_primary`) |
| B | Role/title-seed query only (`title_advanced`, broad seeds: seeds widen acquisition, they never decide qualification) |
| C | A + B |
| D | Broader taxonomy (`ai_taxonomies_a`, ANY tag) |
| E | Small broad control with no role filter, same geography and size, used to estimate recall |

**Language:**
- For Germany, measure English and German title seeds separately.
- For AE and SA, measure English first; add Arabic only if the counts show inventory.

**Taxonomy starting hypotheses (unverified):**

| Campaign | Hypothesis |
|---|---|
| Product | Product-related taxonomies + title seeds |
| Operations | Management & Leadership / Administrative / Logistics + title seeds |
| Finance | Finance & Accounting |
| People & HR | Human Resources |
| Ecommerce | Retail / Marketing / Sales + title seeds |
| Customer Experience | Customer Service & Support + title seeds |
| Marketing & Creative | Marketing, Creative & Media, Art & Design |
| GTM Systems & Revenue Automation | title seeds; the Sales taxonomy is mostly quota-carrying noise |
| AI & Technical Automation | Technology, Software, Data & Analytics, Engineering |

**Report per arm:**
- available inventory;
- rows returned and billed;
- upstream precision;
- recall versus arm E;
- approved jobs per 1k records;
- approved contacts per 1k records;
- duplicates;
- cost per approved job;
- cost per approved contact.

Rank configurations by net-new approved contacts per dollar. Report US separately from the other countries.

## Golden set and holdout (owned by the Independent Evaluator only)

**Building the sets:**
1. Stratify by: campaign (9 + out-of-scope), geography, provider/source, and decision (pass; reject by each major reason code; unknown; error).
2. Split **by company** into calibration and holdout. No company may appear in both.
3. Exclude every job and contact labelled in earlier sessions from the new holdout. They are spent.
4. Label blind to the pipeline decision. Use a rubric written before labelling.

**Freezing (`GOLDEN_SET_MANIFEST.json`, `HOLDOUT_MANIFEST.json`)** records:
- the item ids, the label file sha256 and the rubric sha256;
- metric definitions, denominators and thresholds;
- the date and the evaluator.

**Editing rules:**
- Implementation agents may read calibration labels only.
- Nobody but the evaluator touches the holdout.
- A disputed label goes to a blind re-review logged with before/after. It is never edited in place.

## Metrics (fixed before implementation)

- **Precision:** approved-and-labelled-correct / approved, on the holdout, with a Wilson 95% CI.
- **Recall:** approved-correct / labelled-valid, on the holdout and against the arm-E control.
- **`fn_rate(rule, campaign)`:** labelled-valid among that rule's rejects / labelled rejects.
- **`good_jobs_lost = rejected_count × fn_rate`.** Prioritize by this, never by `rejected_count` alone.
- **Counterfactual per rule:** change only that rule and replay the frozen rows. Report the Δ in qualified jobs, precision, recall and unknowns.

## Change loop (one hypothesis at a time)

For each hypothesis:
1. State the exact root-cause hypothesis.
2. Show the evidence.
3. Write a failing test (superpowers:test-driven-development).
4. Make one minimal change.
5. Run the offline replay.
6. Score on calibration.
7. Score on the frozen holdout.
8. Compare before and after: precision, recall, good jobs recovered, approved leads, duplicates, unknown/error rate, cost.
9. Keep or revert.
10. Append a row to `CHANGE_LEDGER.md`.

**Limits:**
- Never combine unrelated fixes.
- After **3 failed changes in one layer**, stop changing that layer and write an architecture review.

## Cost accounting

- Native units are always reported:
  - Fantastic records billed and requests;
  - Apollo search calls, match credits and org credits;
  - LLM input and output tokens.
- Dollars appear only with a cited price source (`usd_per_unit_source`). Without one, report native units.
- Cost per approved job and cost per approved contact are reported separately. Never divide job cost by contacts to make it look cheaper.
