# Outbound Wave 1 — Control A vs Challenger B

Wave 1 is an account-level A/B test across the nine live Instantly campaigns.

* **Control A** is the live Instantly campaign copy. Nothing in this repository
  renders, rewrites or normalises it. A record assigned to A produces the exact
  enrollment payload production sends today.
* **Challenger B** is a deterministic messaging policy resolved from the
  opportunity record: signal → scope → proof → offer → render → QA. It is
  rendered locally, so the Instantly Challenger campaign template is just
  `{{rendered_email_N}}` plus the account signature.

## Where it plugs in

The outbound path is unchanged:

```
run_approved.py
  -> airtable_client.select_eligible_approved()
  -> instantly_client.enroll_record(record)
       -> instantly_client.airtable_record_to_lead(record, probe=False)
            -> instantly_client.wave1_enrollment_overlay(record)   <-- the only new hook
       -> POST /leads
```

`wave1_enrollment_overlay` returns `("", {})` — i.e. changes nothing — unless
**all four** of these hold:

1. `OUTBOUND_WAVE1_ENABLED` is on;
2. `OUTBOUND_WAVE1_MIN_RECORD_CREATED_AT` parses as an instant;
3. the record is Wave 1 eligible AND its account hashes into arm B;
4. a Challenger campaign id is configured for that role bucket.

Any exception inside the overlay is swallowed and the enrollment is unchanged.

## Segmentation: new leads only, configured buckets only

Two gates run in the same pre-randomisation phase as every other eligibility
check, so a record either of them stops is arm `NONE` — not arm A.

**`OUTBOUND_WAVE1_MIN_RECORD_CREATED_AT`** is the experiment start watermark and
is **required**. Only Airtable rows whose `record.createdTime` is at or after it
may take part. Without it there is nothing separating a genuinely new lead from
an Approved row created months ago for a person the live Control campaigns may
already have emailed — delivering that person into a Challenger campaign would
both double-touch them and contaminate the comparison. A blank or unparseable
watermark is a misconfiguration, not "no restriction": the overlay logs and
leaves every record on Control A. A row with no `createdTime` at all is
suppressed too, because an unknown creation instant cannot be proven to be new.

**`OUTBOUND_WAVE1_CHALLENGER_CAMPAIGNS_JSON`** doubles as the rollout scope. A
record whose bucket has no Challenger campaign is delivered on Control A no
matter what its hash says, so labelling it `B` would put a control-delivered row
in the treatment arm. It is suppressed instead, which keeps the delivered payload
and the measurement frame in agreement. Starting small therefore means mapping
one or two buckets at a 50/50 split — never lowering the split.

Both gates are opt-in at the resolver level (`min_created_at`,
`configured_buckets` default to `None`), so `run_wave1_dryrun.py` still previews
the whole population unless it is asked not to.

## Eligibility comes before randomisation

A record that cannot render the challenger safely is **suppressed from the
experiment**, not moved to Control A:

```
wave1_eligible   = false
experiment_arm   = NONE          <- never A
qa_pass          = false
suppression_reason = "<gate>;<gate>"
```

The order matters. `resolve_wave1` renders and QA-checks the challenger FIRST,
without reference to any arm, and only then randomises the records that passed.
So a copy failure can never enrich the control group, and the two arms are drawn
from one identical eligible population. `outbound_wave1.measurement` excludes
suppressed rows from both denominators.

A suppressed record keeps whatever the pipeline did before Wave 1 existed. That
is the absence of an experiment, not a reassignment, and it is logged as such.

## Modules

| File | Responsibility |
| --- | --- |
| `outbound_wave1/campaigns.py` | The nine campaigns and their frozen policy |
| `outbound_wave1/assignment.py` | Global, account-level, campaign-blind A/B |
| `outbound_wave1/evidence.py` | Reading Role Focus / Focus Evidence safely |
| `outbound_wave1/scope.py` | Deterministic `scope_combination` |
| `outbound_wave1/signals.py` | T1 → T2 → T3 resolution |
| `outbound_wave1/claims.py` | Static claim / role-page registry (no runtime lookup) |
| `outbound_wave1/render.py` | E1 per campaign/tier; E2–E4 frozen verbatim |
| `outbound_wave1/qa.py` | The hard gates |
| `outbound_wave1/timing.py` | Day 1 / 4 / 8 / 13, business days only |
| `outbound_wave1/measurement.py` | Randomisation frame and the primary metric |
| `outbound_wave1/resolver.py` | Orchestrates the above |
| `data/wave1_claims.json` | The static registry (economics unpopulated) |
| `run_wave1_dryrun.py` | Zero-write dry run over real stored opportunities |

## Assignment

`company_assignment_key` is taken from `Outbound Company Identity`, then
`Website`'s domain, then a normalised company name. The arm is
`sha256(experiment_id | salt | key) % 100 < B_SPLIT_PCT`.

The campaign is deliberately **not** an input, so one company can never be A in
one campaign and B in another. A record with no resolvable key is not
randomisable, so it is suppressed (arm `NONE`) rather than counted as control.

## One argument per campaign, not one argument nine times

There is no universal Challenger framework. Each campaign sells the reason ITS
buyer has to care, built on a VERIFIED fact about the TGTC offer. An earlier
revision drifted into a single argument with nine wordings — 8 of 9 campaigns
shipped the identical proof sentence and the nine CTAs used three nouns between
them — so `tests/test_outbound_wave1_integrity.py` now gates the differentiation
directly: nine distinct CTAs, nine distinct frictions, and no single proof
sentence on more than four campaigns.

### Voice

The copy is written to read like a person typing, not like a sequence. Email 1
is 40-60 words, three or four thoughts, one CTA, contractions throughout. It does
not name the reader's own employer back at them -- "your Operations Manager
opening", never "the Operations Manager opening at <their company>", which is the
most recognisable mail-merge shape there is. Counts render as words ("two product
roles"), because a sentence opening with a digit reads as a merge field.

Only the genuinely dangerous patterns are encoded as gates: banned claims,
unsupported absolutes, and a short list of landing-page words. Everything else
about naturalness is editorial judgement and stays that way -- there is no
attempt to express "sounds human" as a phrase list. The gates judge what WE
wrote: the record's own company and role are removed before matching, so a
prospect called "CBX Solutions, LLC" can never fail our buzzword gate.

The three slots carry different burdens of proof, and a QA gate holds each to
its own: the **signal** is the real hiring event read off the record and stays
factual and unhedged; the **friction** is a hypothesis the reader can accept or
reject, so it is hedged (`can`, `may`, `often`, `usually`, `tends to`) or else
true of any hire rather than of this reader, and may never state a flat absolute
or assert something about the reader's own process; the **proof** is an approved
TGTC fact and nothing else.

| campaign | buyer's reason to care (friction) | verified fact sold (proof) | asks for |
| --- | --- | --- | --- |
| PRODUCT | getting several approved at once can be the harder half | headcount model | how the headcount side works |
| OPERATIONS | an ops title on its own may not say much about the actual mix | role-specific testing | how we test for a scope like this |
| FINANCE | a hire is also a payroll/tax/benefits obligation to administer | employment administration | what we carry on the employment side |
| PEOPLE & HR | hiring across functions concentrates the screening load | remote-readiness assessment | how we assess remote readiness |
| ECOMMERCE | ecommerce titles can cover very different work by store size | role-specific testing | how our testing works |
| CUSTOMER EXPERIENCE | people good at these roles are often good in an interview too | role-specific testing | what the testing covers |
| MARKETING & CREATIVE | a wide scope can mean one line asked to cover several jobs | headcount model | how an embedded hire works |
| GTM SYSTEMS | the parts are common; the combination tends to be rarer | role-specific testing | how we test the combination |
| AI & TECHNICAL | tooling this new has not existed long enough for long track records | role-specific testing | how the assessment works |

**ECOMMERCE is knowingly the weakest.** Every ecommerce-specific idea worth
selling — seasonal capacity, platform-specific proof — needs a capability that is
not on the verified list, and inventing one is what this whole design forbids. It
therefore shares the testing proof with an ecommerce-specific reason, and is
flagged rather than forced. Its live Control is `completed`, so it is out of
Pilot 1 anyway.

Two verified facts are deliberately NOT used in E1: *no payment until you select
someone* and *we keep sourcing if the shortlist is not right*. They are E3's
frozen payload, introduced there as "One thing I left out" — putting them in E1
would falsify that line.

## Fallback coherence

A degrade moves the whole triple. When a campaign cannot support economics it
does not merely swap the proof and the offer — the cost-framed friction goes
with them:

| | friction | proof | offer | offer noun |
| --- | --- | --- | --- | --- |
| economics available | `cost_comparison` | `economics` | `role_economics` | the numbers for this role |
| degraded (FINANCE) | `employment_obligation` | `employment_admin` | `employment_admin_overview` | what we carry on the employment side |

`qa.COHERENT_FRICTIONS` is the hard gate. `cost_comparison` may appear only with
`(economics, role_economics)`. Beyond that it enforces the shape of each
argument: a headcount proof answers an APPROVAL friction and an employment-admin
proof answers an ADMINISTRATIVE friction, so neither can be paired with an
evidence friction — "a CV cannot show you that" followed by "they are not on your
headcount" answers a question the reader did not ask.

FINANCE is the only campaign still on the economics path, kept for the day a
published number exists. CUSTOMER EXPERIENCE and MARKETING & CREATIVE were moved
off it: each has a verified angle stronger for its buyer than a price, so an
unreachable economics preference bought them nothing.

A friction whose wording reads off a signal is not rendered behind a different
one (`render._FRICTION_REQUIRES_SIGNAL`). PRODUCT's "getting them all approved"
needs a multi-opening count, so a single-opening record degrades to the generic
friction rather than asserting something the record does not support.

## Offer wording

The offer noun is resolved once, per campaign, and is the literal text the reader
sees in all four emails — E1 asks `Want me to send <noun>?` and E2/E3/E4 repeat
the same words. The nine nouns are in the table above and must stay distinct: a
QA gate fails any thread that contains another campaign's noun.

E3's frozen sentence is `<noun> is still there if you want it first`. A plural
noun takes `are`/`them`; the verb and pronoun are selected mechanically and no
word choice changes.


## Dry run

```bash
python run_wave1_dryrun.py --limit 108 --out reports/wave1_dryrun.json
```

Reads local run artifacts only — no Airtable, no Instantly, no provider calls,
no writes. The artifact carries explicit `denominators`, a `denominator_of` map
saying which population each breakdown is counted over, and a `reconciliation`
block; the script exits non-zero if any count fails to add up.

`--as-of <ISO date>` re-derives job age so the 45-60 day T2 window can be
exercised against the same real records. Such a run is stamped
`clock.mode = "simulated_future_date"` and its counts are test evidence, **not**
current production state. `--claims <path>` previews the economics path against
an alternate registry.

## Going live

1. Create the nine Challenger campaigns in Instantly. Each step body is
   `{{rendered_email_N}}` + `{{accountSignature}}`; E1 carries the subject,
   E2–E4 reply on the same thread with an empty subject. Sequence delays are
   3 / 4 / 5 days (Day 1 → 4 → 8 → 13) on the existing business-day schedule.
2. Set `OUTBOUND_WAVE1_CHALLENGER_CAMPAIGNS_JSON` to the bucket → campaign map,
   on **GTM Approved Sync** — `run_approved.py` is the only production caller of
   `airtable_record_to_lead`, so that is the only service the overlay runs in.
3. Populate `data/wave1_claims.json` for any role whose economics you want to
   quote, or leave it as shipped and run Wave 1 without economics.
4. Set `OUTBOUND_WAVE1_MIN_RECORD_CREATED_AT` to the moment the pilot starts,
   then `OUTBOUND_WAVE1_ENABLED=1` and `OUTBOUND_WAVE1_B_SPLIT_PCT=50`.
5. Deploy, then activate the Challenger campaigns.

Steps 2, 4 and 5 are production environment/deploy actions and are deliberately
not performed by this branch.
