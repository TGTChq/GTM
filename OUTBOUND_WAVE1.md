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
2. the account hashes into arm B at the configured split;
3. every QA gate passes for the rendered copy;
4. a Challenger campaign id is configured for that role bucket.

Any exception inside the overlay is swallowed and the record stays on Control A.

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
randomisable and stays on Control A.

## The claim registry

`data/wave1_claims.json` enumerates all 118 canonical roles with an empty `url`
and `economics_available: false`. That is intentional:

* `role_page_match` is true only for an exact canonical-role entry **with a URL**
  whose display role is that same role verbatim (so "Senior Financial Analyst"
  never inherits "Financial Analyst"'s page);
* economics renders only when that same entry also carries
  `economics_available` and a `claim_source`.

Until an operator fills in real published URLs, FINANCE / CUSTOMER EXPERIENCE /
MARKETING & CREATIVE degrade to `testing_overview` and no price is ever stated.

## Dry run

```bash
python run_wave1_dryrun.py --limit 108 --out reports/wave1_dryrun.json
```

Reads local run artifacts only — no Airtable, no Instantly, no provider calls,
no writes. `--as-of <ISO date>` re-derives job age so the T2 window can be
reviewed against the same real records. `--claims <path>` previews the economics
path against an alternate registry.

## Going live

1. Create the nine Challenger campaigns in Instantly. Each step body is
   `{{rendered_email_N}}` + `{{accountSignature}}`; E1 carries the subject,
   E2–E4 reply on the same thread with an empty subject. Sequence delays are
   3 / 4 / 5 days (Day 1 → 4 → 8 → 13) on the existing business-day schedule.
2. Set `OUTBOUND_WAVE1_CHALLENGER_CAMPAIGNS_JSON` to the bucket → campaign map.
3. Populate `data/wave1_claims.json` for any role whose economics you want to
   quote, or leave it as shipped and run Wave 1 without economics.
4. Set `OUTBOUND_WAVE1_ENABLED=1` and `OUTBOUND_WAVE1_B_SPLIT_PCT=50`.
5. Deploy, then activate the Challenger campaigns.

Steps 2, 4 and 5 are production environment/deploy actions and are deliberately
not performed by this branch.
