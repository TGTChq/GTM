# "Only Apollo can promote an email" — verified, and what it would take to change

Asked because `email_unverified` is the largest single hiring-manager outcome on the
2026-09-04 run (**740 of 2,410**), and a bottleneck on one refusing provider is worth
knowing precisely rather than approximately.

## The claim is true. Here is the exact implementation.

`email_gate.py::EmailGate.evaluate` is the only place an email reaches `PASS`, and it
has exactly one such branch:

```python
apollo_status = str(person.email_status or "").strip().lower()
...
if apollo_status == "verified":
    return GateDecision("email", GateState.PASS, "EMAIL_PASS", ...)
```

`person` is a `PersonMatch` returned by `apollo_client.match_person`, so
`apollo_status` is Apollo's own `email_status` field and nothing else can set it.

**The policy dependency, stated as the code states it:** Hunter is optional
corroboration that *fails open*. It can send an address to `REROUTE` when it returns
`invalid` or `disposable`, and it is otherwise ignored — the module's own comment is
explicit that it "can NEVER promote a non-verified email to verified". Every
non-Apollo-verified company-domain address ends at `NEEDS_CHECK`, which never counts
toward FINAL_PASS.

So the dependency is not incidental or a configuration accident. It is a deliberate
single-authority design, and it is enforced structurally rather than by convention.

## What `email_unverified` actually contains

The gate produces five distinct non-PASS outcomes, and the stage's single
`email_unverified` total does not separate them:

| situation | gate result | owner |
|---|---|---|
| no address at all | `UNVERIFIED` / `UNVERIFIED_EMAIL`, retryable | Apollo coverage |
| generic mailbox (`info@`, `careers@`, …) | `REROUTE`, `generic_mailbox` | **our policy** |
| address not on a company domain | `REROUTE`, identity mismatch | **our policy** |
| second opinion says `invalid`/`disposable` | `REROUTE`, deliverability | second provider |
| address present, Apollo did not verify it (`unverified`, `extrapolated`, `likely_to_engage`, `unavailable`) | `NEEDS_CHECK` | **Apollo verification** |

Only the last of those is a genuine single-provider bottleneck. Two of the five are
our own rules. `outcome_forensics` decomposes the run's corpus along exactly these
lines; whatever it cannot separate is reported as not decomposable rather than
apportioned.

**One configuration fact that changes the picture:** `VERIFY_WITH_HUNTER` defaults to
`True` but is set **per service**, and the two services differ. Where it is off, there
is no second opinion at all — not even the reroute — so `hunter_status` is empty and
every non-Apollo-verified address falls to `NEEDS_CHECK` unexamined. The forensics
reports `hunter_status` per lead precisely so "was there a second opinion" is answered
from evidence rather than from the flag's default.

## MEASURED — and it reverses the proposal below

Decomposed 2026-09-06T18:58Z from the 09-04 run's own enriched corpus by streaming
scan. **Per file, not summed** — the run writes the same leads into
`enrichment_progress.json` and `jobs_enriched_2026-09-04.json`, so adding the two
reported 2,068 for 1,034 leads counted twice.

    apollo_email_status     verified       1,034
                            extrapolated      14

    primary_reason (gate outcomes, same corpus)
      email_pass                            1,034
      unverified_no_valid_contact             532
      unverified_email                        109
      reroute_email_identity_mismatch          96
      unverified_organization                 112
      unverified_official_source              424

    hunter_status           (absent)          452     <- no second opinion ran

**The bottleneck is Apollo COVERAGE, not Apollo VERIFICATION.** Of the addresses the
run actually obtained, Apollo verified **1,034** and left only **14** as
`extrapolated`. There is no large population of "addresses Apollo gave us but would
not verify" — that category is a rounding error. The mass sits at
`unverified_no_valid_contact` (532): no usable contact was found at all.

A second verification authority would therefore address roughly **14 leads**, not 740.
**It is not worth a vendor, a contract or a code change**, and the proposal below is
recorded as considered-and-declined on evidence rather than left open as a plausible
idea. This is precisely why the decomposition had to come first.

Two things it did confirm are worth acting on:

* **`reroute_email_identity_mismatch` (96) is our own policy**, not provider coverage
  — an address rejected for not matching the company domain. Whether that rule is
  right is a business decision available today, at no cost, with no provider involved.
* **No second opinion ran at all** on the leads carrying the field: `hunter_status` is
  absent on all 452. `VERIFY_WITH_HUNTER` is set per service and the two services
  differ, so the reroute half of the gate was inert here. That is a configuration
  decision, not a purchase.

The counts above are FIELD OCCURRENCES from a streaming scan — an upper bound per
lead, and not the same unit as the hiring-manager stage's 740 leads. They are used to
answer "does this category exist in quantity", which they can, and not to compute a
rate, which they cannot.

## Removing the bottleneck without weakening verification

The constraint to respect: **never mark an unsupported address verified.** That rules
out the tempting shortcut of treating "no negative signal" as a pass — Hunter's
`accept_all`, `unknown` or absent result is not evidence of deliverability, and
promoting on it would convert a strong gate into a weak one while leaving every
counter looking the same.

What does not weaken it is admitting a **second positive authority**: a provider that
performs its own verification and returns a hard-positive result, recorded with its
own provenance.

### Proposed change — NOT enabled, and now NOT recommended

Kept for the record because the reasoning is reusable if coverage ever
improves and verification becomes the binding stage. On today's evidence it
is not.

*Design.* `EmailGate` gains an ordered list of verification authorities. Apollo stays
first and unchanged. A second authority may return `PASS` only on an explicit positive
verification (for Hunter, `status == "valid"`; for a dedicated verifier, a
`valid`/`deliverable` SMTP result), never on the absence of a negative. The evidence
bundle records `VERIFIED_CROSS_SOURCE` attributed to the provider that verified, so
provenance is never collapsed into "verified".

*Surface.* `email_gate.py` (the branch), one client module, and config flags:
`EMAIL_VERIFICATION_AUTHORITIES` (ordered, default `apollo` alone),
`EMAIL_VERIFICATION_SECOND_AUTHORITY_ENABLED` (default off), plus that provider's
credentials and per-run cap. Default behaviour byte-identical to today.

*Candidates.* Hunter is already integrated and already called where enabled, so
admitting `status == "valid"` is the smallest possible change — no new vendor, no new
billing relationship. A dedicated verifier (ZeroBounce, NeverBounce, Bouncer) is a
stronger signal and a new contract. **Both are billing decisions and neither is
enabled here.**

*What it would be worth.* Unknown until the 740 is decomposed. If most are `no
address`, a second verifier changes nothing — the gap is Apollo coverage, not
verification. If most are `address present, Apollo did not verify`, a second authority
addresses that share directly. **The decomposition should decide this, not the other
way round.**

### The cheaper thing to check first

`extrapolated` and `unverified` are Apollo statuses on addresses Apollo *has*. Some
may verify on a later match — the 09-04 run was interrupted mid-flight, and its
retries never ran. A bounded re-verification of held addresses costs Apollo credits
and no new vendor, and it is already covered by the recovery budget in
`orchestrator/apollo_budget.py`. It should be measured before any new provider is
contracted.

## Decisions this needs

1. **Apollo billing** (unchanged, still the critical path) — nothing below can be
   measured while it refuses.
2. **Whether to enable a second verification authority at all**, given the design
   above keeps the verification standard intact.
3. If yes: **Hunter's existing integration** (no new vendor) or **a dedicated
   verifier** (stronger, new contract).
4. **A per-service decision on `VERIFY_WITH_HUNTER`**, which currently differs between
   GTM and Approved Sync and means the two services do not apply the same evidence.

Nothing here is enabled, and no paid service is switched on.
