# Country compliance gates — implementer report (`tgtc-compliance/1`)

Branch `audit/phase2-offline-fixes`, base `d47c16e`, tip `5be8c0e`.
Specification: `funnel_audit_20260919/COMPLIANCE_MATRIX.md`.

**Status: complete.** Nothing deployed, nothing pushed, no outreach, no Airtable
or Instantly write, no Apollo spend, no provider call of any kind. The frozen
labels and the holdout were not read and not touched.

| Artifact | Path |
|---|---|
| Exact diff | `.superpowers/sdd/phase2/task-compliance-diff.patch` (`git diff d47c16e..HEAD`) |
| Dry-run counts | `.superpowers/sdd/phase2/compliance-dryrun.json` |
| Dry-run harness | `rebuild/audit_compliance_corpus.py` |

## Commits

| SHA | What |
|---|---|
| `3898e7b` | the matrix as data plus four independent pure gates (`tgtc_core/policy/compliance.py`) |
| `6453537` | migration 011 — jurisdiction stored, additive, no backfill, mirrored into `schema.sql` |
| `ebfbe54` | the gates wired into the live pipeline; fail closed for sending, open for capacity |
| `003004d` | the isolated offline dry run over the purchased corpus |
| `5be8c0e` | self-review: report every rule version, drop two harness leftovers |

Tests: `python rebuild/run_offline_tests.py tests_core -q` → **1,306 passed, 0
failed** (entering state 1,094 collected; this task adds 212, all passing, and
the 2 failures the brief predicted did not occur in this environment — the
`anthropic` SDK is installed here).

---

## A. Four independent gates

`tgtc_core/policy/compliance.py`. Data (`COUNTRY_PERMISSIONS`, one
`CountryPermissions` row per country with the matrix's own labels, notes and
source URLs) plus four functions:

`job_acquisition_allowed` · `aggregate_capacity_modelling_allowed` ·
`person_enrichment_allowed` · `cold_email_allowed`

Each takes the country it decides on as its **first positional argument** and
returns a `GateDecision` carrying `status`, `allowed`, `reason`, the matrix
label and the rule version. Nothing in the module reads a second country field
when the first is missing. A test asserts no generic
`geography_allowed` / `country_allowed` helper exists, and 20 parametrised tests
enumerate the 4 × 5 matrix cells from the table rather than from the
implementation.

Statuses are four-valued, not boolean: `allowed`, `enabled_conditional`,
`disabled`, `unknown_jurisdiction`. `allowed` is true only for the first. That
distinction is what lets the live enrichment gate block a `disabled` country
without also halting on every row whose country was never observed — see the
asymmetries below.

**Share a predicate, never copy it.** Opt-out honoured, unsubscribe available
and suppression available are ONE `Condition` object each, referenced by both
`US_SEND_CONDITIONS` and `UK_SEND_CONDITIONS`; a test asserts the US set is a
subset of the UK set by object identity. The three UK processing conditions are
referenced by both the enrichment gate and the cold-email gate.

## B. Jurisdiction stored, never inferred

Migration `011_country_compliance_jurisdiction.sql` — additive,
`ADD COLUMN IF NOT EXISTS`, nullable, no default, no backfill, no
`DROP`/`UPDATE`/`DELETE`/`NOT NULL`. A test asserts all of that from the file's
own text, and a second asserts every column is mirrored into `schema.sql`
(which `apply_schema()` runs before any migration, so a fresh install never
replays the migration).

Three levels, because three different things are observed at three different
times and must not overwrite each other:

- `employers` — `company_country`, `employer_legal_entity_type`,
  `corporate_subscriber_status`
- `people` — `contact_country`, `opt_out_status`
- `approvals` — all twelve fields the matrix names, as the per-lead snapshot
  plus the decision

Before this, the core had no country column at all outside `postings.countries`
— the provider's derived list for the **job**. Every jurisdiction question
therefore had exactly one answer available: the one the matrix forbids using for
a person.

`tgtc_core/domain/jurisdiction.py` is the only module that reads provider
shapes: `observe_job_country` (from `countries_derived`),
`observe_company_country` (from the employer's published addresses),
`observe_legal_entity_type`, `observe_contact_country` (from the person record).
Every one returns `""` when the evidence does not determine an answer, including
when it determines two different answers — a posting listed under two matrix
countries is unknown, not "whichever came first".

## C. The UK deterministic gate

Every condition in `UK_SEND_CONDITIONS`, each decisive on its own (14
parametrised failure tests):

| Condition | Block reason |
|---|---|
| incorporated company or qualifying corporate body, no sole trader or unincorporated partnership | `uk:not_a_verified_corporate_subscriber` |
| lawful-basis record present, with evidence | `uk:no_lawful_basis_record` |
| privacy-notice process configured | `uk:privacy_notice_process_not_configured` |
| privacy notice carries a due date | `uk:privacy_notice_not_due_dated` |
| not historically opted out | `outreach:historically_opted_out` (shared with US) |
| unsubscribe available | `outreach:unsubscribe_not_available` (shared with US) |
| suppression available | `outreach:suppression_not_available` (shared with US) |
| exact-domain verified work email | `uk:not_an_exact_domain_verified_work_email` |
| no personal or free email domain | `uk:personal_or_free_email_domain` |

**Unknown entity type is never corporate.** `classify_corporate_subscriber`
recognises only the *words of a legal form*. A provider's self-declared
ownership descriptor — "Privately Held", "Public Company", "Nonprofit",
"Educational", "Government Agency" — says who owns the body, not whether it has
separate legal personality, and classifies as `ENTITY_UNKNOWN`. LLPs are matched
before the excluded partnership forms, because a substring rule alone would
exclude every LLP the matrix admits. And the stored
`corporate_subscriber_status` is a cross-check, never the sole authority: both it
and the legal form must independently say corporate, so an operator-set flag
cannot override an unverifiable entity.

## D. Fail closed for sending, open for capacity

A blocked record is **not refused at approval**. It is approved, stored with a
named `outreach_block_reason`, and counted — refusing would delete exactly the
records the matrix says to retain. Both of its outbox items are created in the
outbox's own existing `blocked` state, so nothing can ever claim them. Only an
explicit `TRUE` sends: a legacy approval whose verdict is `NULL` is blocked as
unknown.

An opportunity blocked at the enrichment gate keeps its employer row, its
posting row and its opportunity row, and closes with a named
`compliance:person_enrichment_not_permitted:<COUNTRY>` reason that `ledger()`
reports as its own bucket.

## E. Counting

`ledger()` reports, separately and never summed into one another:

`outreach_eligible_contacts` · `compliance_blocked_contacts` ·
`compliance_blocked_by_reason` · `approved_by_contact_country` ·
`outreach_eligible_by_contact_country` · `compliance_blocked_by_contact_country`
· `opportunities_compliance_blocked_by_reason` (a different unit) ·
`compliance_rule_versions`

The two contact buckets are exact complements of `approved_distinct` — a
reconciliation test asserts it. The per-country splits read `contact_country`,
the only jurisdiction an outreach decision may be read against; a row whose
contact country was never observed is reported under `unknown`, never merged
into a country that happened to appear elsewhere on the record.

**DE, AE and SA can never reach a ready-to-send total.** Proven three ways in
`tests_core/test_compliance_live_wiring.py`: the approval is written
`outreach_eligible = False`; `outreach_eligible_contacts` is 0 and
`outreach_eligible_by_contact_country` is empty; and a real delivery drain on
both channels returns nothing and writes zero receipts.

---

## Reachability — what is live and what is not

Stated plainly, because this branch has already caught one false reachability
claim.

**Reachable by the live pipeline:**

| Where | Reached from | What it does |
|---|---|---|
| `services/identity_service.resolve_employer` | `Runner.work("resolve_identity")` | stores the employer's country, legal form and corporate-subscriber verdict |
| `services/opportunity.OpportunityService._enrichment_jurisdiction_gate` | `Runner.work("qualify_opportunity")` | refuses paid person enrichment for a `disabled` country, **before** any Apollo call |
| `services/opportunity.OpportunityService._upsert_person` | same | stores the enriched person's own country |
| `domain/approval.build_approved_lead` → `_commit_approval` | same | writes the twelve fields; creates both outbox items `blocked` when not eligible |
| `services/delivery.DeliveryService._precheck` | `Runner.deliver` → `drain` | refuses to send on both channels |
| `services/metrics.ledger` | reporting | the separate counts |

**Not reachable, deliberately:** `job_acquisition_allowed` and
`aggregate_capacity_modelling_allowed` are an API and a recorded field only.
Both are `yes` for all five researched geographies, so enforcing them could only
ever delete rows for a country the matrix does not cover — and "never delete it"
is the rule.

### Three asymmetries, each deliberate

1. **The enrichment gate blocks only a `disabled` verdict, never an unknown
   company country.** `employers.company_country` is `NULL` for every row
   predating migration 011, so failing closed there would halt the whole
   pipeline while protecting nothing — the send gate already fails closed on the
   contact's own unknown jurisdiction, which is where the matrix puts the
   requirement.
2. **The delivery compliance check sits after the R11 evidence checks**, which
   *revoke* an approval whose vacancy has gone or changed hands. Short-circuiting
   on compliance first would lose a data-integrity finding to a delivery
   decision. (This is why one existing test,
   `test_delivery_rechecks_legacy_approval_without_an_external_call`, still
   expects `employer_attribution_conflict` — it caught the wrong ordering on the
   first attempt.)
3. **`unsubscribe_available` / `suppression_available` are asserted true** by
   `OpportunityService`, because this core's send path provides them
   structurally — every Instantly campaign carries the unsubscribe link, and
   `delivery._precheck` runs `suppression_check` before every send on both
   channels. That is a cited structural fact, not a convenience default. The UK
   lawful basis and privacy-notice process are deployment facts no code can
   discover, so they come from `Settings` and default to absent, which blocks.

New settings, all fail-closed by default: `TGTC_OUTREACH_LEGAL_BASIS`,
`TGTC_OUTREACH_LEGAL_BASIS_EVIDENCE`,
`TGTC_OUTREACH_PRIVACY_NOTICE_CONFIGURED`, `TGTC_OUTREACH_PRIVACY_NOTICE_DAYS`
(default 30). `Settings.describe()` reports presence, never the value.

---

## Isolated dry run over the purchased corpus

`python rebuild/audit_compliance_corpus.py <records-dir> --json <out>` over
`funnel_audit_20260919/phase3_paid/records/` (64 files). In-process, offline, no
database, no provider call; a test asserts the module names no database or HTTP
client. It reuses the pipeline's own readers (`services.acquisition.org_block`,
`domain.jurisdiction`, `policy.compliance`) rather than restating them.

**Units.** 5,886 provider records → **5,285 unique jobs** → **3,556 unique
companies**. Contacts: **0** — this corpus contains none.

### What the corpus can answer

| | US | UK | DE | AE | SA | unknown |
|---|---|---|---|---|---|---|
| unique jobs by **job** country | 2,361 | 1,110 | 1,043 | 407 | 343 | 21 |
| unique jobs by **company** country | 2,282 | 785 | 882 | 240 | 175 | 921 |
| unique companies | 1,620 | 535 | 528 | 152 | 110 | 619 |
| `job_acquisition_allowed` | ✔ | ✔ | ✔ | ✔ | ✔ | blocked (21) |
| `aggregate_capacity_modelling_allowed` | ✔ | ✔ | ✔ | ✔ | ✔ | blocked (21) |
| `person_enrichment_allowed` (on company country) | allowed 2,282 | conditional 785 | **disabled 882** | **disabled 240** | **disabled 175** | unknown 921 |

1,297 unique jobs at DE/AE/SA companies would be refused paid person enrichment
before any Apollo call.

### What the corpus cannot answer, and does not pretend to

The purchased records are **job** records. They carry no contact, so
`contact_country` — the field the outreach gate decides on — is unknown for
every row, and the **measured** cold-email verdict is
`unknown_jurisdiction` for all 5,285:

| Measured category (unique jobs) | Count |
|---|---|
| `OUTREACH_ELIGIBLE` | 0 |
| `COMPLIANCE_BLOCKED_UNKNOWN_JURISDICTION` | 5,285 |
| `COMPLIANCE_BLOCKED_CAPACITY` | 0 |
| `COMPLIANCE_BLOCKED_ENTITY_UNKNOWN` | 0 |

That is a real result, not a harness defect: no record in this corpus can be
sent to, because no record in it identifies a person.

### Fail-closed counts (requested explicitly)

| Fail-closed reason | Unique jobs |
|---|---|
| unknown **contact** jurisdiction | 5,285 (100%) |
| unknown **company** jurisdiction | 921 (17.4%) |
| unknown **job** jurisdiction | 21 (0.4%) |
| **unknown entity type** (never corporate) | 4,186 (79.2%) |
| entity type an **excluded** form (sole trader, partnership, trust…) | 341 (6.5%) |
| entity type a **verified corporate** form | 758 (14.3%) |

For the UK specifically — the only geography where entity type gates outreach —
785 unique jobs at 535 companies. Counted **per unique job** (the unit the
harness aggregates; a per-company figure was not computed and is not claimed):
**86 corporate**, 41 excluded, **658 unknown**. On today's evidence **83.8% of
UK jobs in this corpus are at employers whose legal form cannot be verified**,
and an unverifiable legal form is never a corporate subscriber, so they cannot
be emailed under the matrix.

### Projection, labelled as one

Because "all unknown" is not useful for planning, each row also carries what the
outreach gate *would* say if the contact turned out to be in the employer's own
country. This is a **counterfactual, not a measurement**, kept in its own field
and its own table and never added into a measured total:

`enabled_conditional` 3,067 (US 2,282 + UK 785) · `disabled` 1,297 (DE 882 +
AE 240 + SA 175) · `unknown_jurisdiction` 921.

Even under that projection the 3,067 are only *conditionally* enabled. The US
2,282 still need the send controls, and the UK 785 still need the lawful basis,
the privacy notice and — for 658 of them — an entity type nobody has
established.

Not measured by this corpus, and listed as such in the JSON rather than
estimated: qualified jobs, unique company × campaign units, contacts found,
correct verified contacts, suppression-adjusted net-new contacts.

---

## Concerns and open decisions for Luis

1. **UK paid enrichment currently proceeds without the UK groundwork.** The
   matrix makes UK person enrichment `ENABLED_CONDITIONAL`, and enrichment is
   processing personal data. Today the live gate blocks only the three `no`
   countries; a UK record is enriched and then blocked at the send. The argument
   for the current behaviour is requirement D, which asks for an explicit
   `outreach_block_reason` on records whose UK corporate status is unknown — and
   a record can only carry one if it becomes a contact. The argument against is
   that without a documented lawful basis, the enrichment itself has none. This
   is a one-line change at
   `OpportunityService._enrichment_jurisdiction_gate` (block when the country is
   known and the gate is not `allowed`, instead of only on `disabled`). **It is
   a policy call with a spend consequence, so I have not made it.**
2. **Three CAN-SPAM controls are not enforced by any gate**, because no stored
   field can decide them: accurate sender/header/subject, commercial
   identification, and a physical postal address. They live in the email
   template. No condition here claims to check them, and none defaults to true
   to paper over the gap — but that means a US send passing this gate is not the
   same as a CAN-SPAM-compliant send. Template and operations own the rest.
3. **`corporate_subscriber_status` is derived from a company NAME suffix and a
   provider's declared type, not from a company register.** That is why 79% of
   the corpus is unknown. Making UK outreach viable at volume needs a real
   registry lookup (Companies House), which is a new provider and a new spend
   decision.
4. **`opt_out_status` has no writer yet.** The column exists, the gate reads it,
   and the suppression check is a separate required condition that does run — but
   nothing yet writes an unsubscribe outcome event into it. Until something does,
   the opt-out condition is satisfied by absence and the real protection is the
   suppression check.
5. **`company_country` is unknown for 17% of the corpus and NULL for every
   employer predating migration 011.** No backfill was performed, by design. The
   enrichment gate therefore protects nothing on legacy rows until they are
   re-observed; the send gate is unaffected, because it reads the contact's own
   country.
6. **Two matrix rows are enforced only as recorded fields.** `job_acquisition`
   and `aggregate_capacity_modelling` are `yes` everywhere in scope, so there is
   nothing to enforce; if a sixth geography is ever acquired, that changes and
   the gates are already there to be wired.
