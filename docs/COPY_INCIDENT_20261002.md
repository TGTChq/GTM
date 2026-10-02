# Challenger copy incident — ledger (opened 2026-10-02)

Resumable log. Each step records what was VERIFIED first-hand, with the receipt.
No secrets in this file. Append only; never rewrite a recorded fact.

## Step 0 — environment pinned (2026-10-02T16:15Z)

| Fact | Value | How verified |
| --- | --- | --- |
| Railway project | `tgtc-daily-pipeline` / `production` | `railway status` |
| Core service | `GTM Core Canary 1000` (`f83cd97a…`) | `railway status` |
| Effective deployment | `fd5fe3de…` SUCCESS, 2026-09-26T00:05:32Z | `deployments` GraphQL `meta` |
| Effective commit | `0ebd7f5` on `feat/rebuild-core` | same `meta.commitHash` |
| Core cron | `0 3 * * *`, next run ~2026-10-03T03:00Z | `railway status` |
| Run in flight | NO — last core run `Completed` | `railway status` |
| Other crons | GTM Replies `15 * * * *`; GTM Weekly Report `0,20,40 13-20 * * *` | `railway status` |
| Incident branch | `claude/instantly-challenger-copy-incident-709fff` reset to `0ebd7f5` | `git log -1` |

Campaign scope: nine Challenger campaigns, ten function keys (CUSTOMER EXPERIENCE
serves `customer_success` + `customer_support` through one campaign). IDs are the
allow-list in `tgtc_core/policy/campaigns.py` (`KNOWN_CHALLENGER_CAMPAIGN_IDS`),
not secrets.

## Step log

- [x] S0 Pin environment, branch, deployment, run lock. DONE above.
- [ ] S1 Verify configured campaign ids == allow-list; pause the nine; confirm paused.
- [ ] S2 Audit actually-sent messages since 2026-09-14; establish real onset + cohort.
- [ ] S3 Review + adapt + apply the patch on this base.
- [ ] S4 Tests (focused + full suite on PostgreSQL); image contains renderer + claims.
- [ ] S5 Deploy; verify effective commit.
- [ ] S6 Repair existing leads + pending outbox payloads from approved facts; read-back each.
- [ ] S7 Internal test sends; verify received subject/body/personalisation.
- [ ] S8 Resume campaigns only after S6+S7 verified. Record restoration.

## Step 1 — containment VERIFIED (2026-10-02T16:2xZ)

Configured campaign ids were read from the live core service and compared to
`KNOWN_CHALLENGER_CAMPAIGN_IDS`: nine distinct ids, set equality **True**, none
missing, none extra. `TGTC_EXHAUSTIVE_NINE_CAMPAIGNS=1`, `TGTC_INSTANTLY_ROTATION_ENABLED=1`,
`TGTC_APPROVED_TARGET_PER_RUN=1000`, OOO follow-up campaign `f0665173…`.

Backup before any write: all nine full campaign configs saved with SHA-256 to
`C:\TGTC\copy_incident_private\backup_campaigns\` + `MANIFEST.json`.
Each campaign = 1 sequence, 4 steps, 4 variants.

**Pre-incident status (must be restored exactly):**

| Campaign | Pre-incident status |
| --- | --- |
| PRODUCT, OPERATIONS, PEOPLE_HR, ECOMMERCE, CUSTOMER_EXPERIENCE, MARKETING_CREATIVE, GTM_SYSTEMS, AI_TECHNICAL | 1 = ACTIVE |
| FINANCE | 2 = **ALREADY PAUSED** — must NOT be activated on restore |

Pause result (read-back verified, not trusting the 200): 9/9 at status 2.
FINANCE needed no write. Re-read vs backup: `sequences_identical=True`,
`schedule_identical=True`, all other fields identical, **configuration drift 0**.
Write log: `C:\TGTC\copy_incident_private\status_changes.jsonl`.

### Template evidence — the impact mechanism, proven

Every one of the nine campaigns has the identical 4-step shape:

- step 1 subject `{{rendered_subject}}` (FINANCE wraps it in zero-width spaces)
- steps 2,3,4 subject `''` — **intentionally empty, same-thread follow-ups**
- step N body is *only* the variable plus the signature, e.g. PRODUCT step 1:
  `<div>{{rendered_email_1_html}}</div><div><br /></div><div>{{accountSignature}} </div>`

So the body carries no literal copy at all. A lead missing
`rendered_email_N_html` therefore receives a **signature-only body**, and at step 1
an **empty subject** as well. An empty subject on steps 2-4 is by design and is
NOT evidence of this defect.

## Step 2 — PROVEN cause and REAL scope

### Proven cause (two defects, one visible symptom)

1. `tgtc_core/domain/approval.py::instantly_payload` built only the 14
   Control-shaped custom variables and hard-asserted
   `set(variables) <= set(_CUSTOM_VARIABLE_NAMES)` — an allow-list containing no
   `rendered_*` name. So the rebuilt core *could not* emit Challenger copy.
2. The nine live campaign bodies contain **no literal copy**: each step body is
   only `{{rendered_email_N_html}}` + `{{accountSignature}}`, step 1's subject is
   `{{rendered_subject}}`. Missing variables therefore produce an **empty subject
   and a signature-only body**, not a degraded email.

Database proof (not inference): of **9,053** stored `delivery_outbox` Instantly
payloads, `rendered_subject` present = **0**, `rendered_email_1_html` present =
**0**. The only variable keys ever sent are exactly the 14 Control ones.

### Real scope, measured from 24,300 actually-sent messages (2026-08-31 → 2026-10-02)

| Fact | Value |
| --- | --- |
| **Onset of broken sends** | **2026-09-21T13:15:59Z** (MARKETING_CREATIVE, step 1) |
| Last broken send | 2026-10-02T16:22:03Z (minutes before the pause) |
| **Broken messages sent** | **16,875** |
| **Distinct recipients affected** | **7,777** |
| Sending mailboxes involved | 252 (all) |
| Healthy Challenger sends in window | 2,278 (legacy cohort, 591 recipients) |
| Unresolved `{{...}}` tokens delivered | **0** — Instantly drops unknown variables |
| Core-created Challenger leads (creation side) | 7,839 distinct, 2026-09-20 23:00Z → 2026-09-28 05:29Z |

### The 14 September premise is WRONG — and the export proves it

Broken sends between 2026-09-14 and 2026-09-19: **0**. No broken send exists
before 2026-09-21 at all. The 1,214-message export dated the 17th is a
**pre-incident healthy baseline**, not evidence of the failure: every one of
those messages predates the first core-created enrolment (2026-09-20 23:00Z).
The first core-created leads could not send until Monday 2026-09-21 because
19-20 September was a weekend.

Coincidence worth naming so it is not mistaken for corroboration: `delivery_outbox`
holds exactly **1,214** Instantly rows in state `blocked`. That is an unrelated
population (never sent) that happens to share the number.

### Distinctions the audit kept separate (as required)

- **Intentionally empty subjects**: steps 2-4 have `subject=''` in the template
  by design (same-thread follow-ups). Not a defect.
- **Observed**: Instantly derives the thread subject from step 1, so healthy
  steps 2-4 arrive as `Re: <rendered subject>`. All 2,278 healthy sends have a
  non-empty subject; all 16,875 broken sends have an empty one. Subject emptiness
  and signature-only body agree on **100%** of messages — two independent
  indicators, same cohort.
- **Signature-only bodies** are the defect's signature: body visible text is the
  `{{accountSignature}}` block alone.
- **Not judged as sends**: 8 records with no step are Instantly forwarding
  replies/OOO notices to the mailbox owner.
- Step 4 broken count is **0** only because the 13-day sequence had not reached
  it. The 2026-09-21 cohort was due its step 4 on **2026-10-03** — the pause
  prevented that wave.

## Step 3 — fix applied and adapted to this base

Base is the EFFECTIVE production commit `0ebd7f5` (`feat/rebuild-core`), not the
legacy `main` the incident branch was originally cut from.

Applied, adapted:

- `tgtc_core/domain/outbound_copy.py` (new) — the copy contract.
  `copy_block_reason` names a refusal for a missing, unresolved or
  visibly-empty required field; `rendered_variables` renders from the lead's own
  approved facts and returns a COMPLETE set or raises.
- `tgtc_core/domain/approval.py` — `instantly_payload` adds the rendered copy for
  a Challenger destination only. Control keeps its live static templates.
- `tgtc_core/providers/instantly.py` — `create_lead` refuses an incomplete
  Challenger payload before the transport is touched.
- `tgtc_core/services/delivery.py` — a stored outbox payload without copy is
  BLOCKED with a named reason instead of sent.
- `Dockerfile.core` — ships `outbound_wave1/` and `data/wave1_claims.json`.
- `tests_core/test_no_legacy_imports.py` — allows `outbound_wave1` and PROVES it
  is pure (stdlib-only imports; a fresh process confirms no `requests`/`config`/
  `http_utils`/`instantly_client` is pulled in).
- `tests_core/test_challenger_copy_contract.py` (new) — the regression.

Two deliberate departures from the supplied patch, both measured:

1. **Kept the "nothing undocumented reaches the provider" invariant.** The patch
   updated `variables` after the `_CUSTOM_VARIABLE_NAMES` assertion, which left
   ~17 new names undeclared. `outbound_copy.CHALLENGER_COPY_VARIABLE_NAMES` now
   declares them and `rendered_variables` refuses anything outside that set.
2. **Did NOT add `role_display_resolver`.** It looked like the obvious way to
   recover the 28% that fail the renderer's role QA. Measured on 268 real
   production approvals it makes things *worse*: 71.6% → 69.8% complete, because
   the core's `open_role` is frequently already a clean generic noun
   ("product role") that passes, while the raw posting title does not. Scope
   left unchanged on evidence rather than on expectation.

### Known, reported consequence of failing closed

On 268 real approvals the frozen renderer completes **71.6%** and refuses
**28.4%**, every refusal on a role-display QA gate
(`role_display_contains_unsafe_characters` 43, `..._carries_an_appended_qualifier`
18, `..._longer_than_48_chars` 13, `buzzword_solution` 3). Those leads are now
BLOCKED with a named reason rather than enrolled blank. That is the correct
outcome and matches what the legacy path did (it suppressed them), but it is a
real reduction in enrolment yield and is Brett's/Luis's call, not mine.

## Step 4 — tests and gates

| Gate | Result |
| --- | --- |
| Regression BEFORE the fix | 27 failed, 1 passed (reproduced) |
| Regression AFTER the fix | 32 passed |
| Focused approval/delivery/registry modules | 123 passed |
| **Full suite, real PostgreSQL (embedded pgserver)** | **2,092 passed, 0 failed, 0 errors, 226.7s** |
| `ci_check_integrity.py` | `checked=35 mismatch=0 absent=0 (OK)` |
| `ci_no_network.py` | clean |
| Run lock before deploying | `scripts/run_lock_status.sh` → `held=0`; `pg_locks` shows zero advisory locks |

Docker is not installed on this host, so the image was NOT built locally. The
renderer and claim registry are verified INSIDE the deployed container instead
(Step 5), which tests the artefact that actually runs.

## Step 5a — repair plan, and the provider behaviour that decides how to verify

Backed up every lead in the nine campaigns by provider id before any write:
**8,395** leads → `C:\TGTC\copy_incident_private\backup_leads\` (+ `_cursor.json`).

Joined to the 7,839 approvals that created them and rendered each one's copy from
its own approved facts with the deployed code path:

| Disposition | Leads | Lead status | Action |
| --- | --- | --- | --- |
| **REPAIRABLE** | **5,653** | all active | patch copy in, verify by read-back |
| **CANNOT_RENDER** | **1,689** | all active | NO copy exists; must not send. Needs a decision |
| ALREADY_HAS_COPY | 591 | 569 completed, 21 bounced, 1 unsubscribed | untouched (the legacy healthy cohort) |
| TERMINAL_STATUS_LEAVE_ALONE | 462 | 336 bounced, 126 completed | untouched |

`ALREADY_HAS_COPY = 591` independently matches the 591 healthy recipients found in
the send audit — two separate measurements agreeing.

`CANNOT_RENDER` reasons: `role_display_contains_unsafe_characters` 895,
`..._carries_an_appended_qualifier` 538, `..._longer_than_48_chars` 234, buzzword
gates 17, `..._reads_as_a_posting_headline` 3. Only statuses 1/2 are mutated, the
rule proven by the 2026-09 outbound correction.

### Canary, and a correction

One lead (`01a0c236…`, ECOMMERCE, the smallest cohort) was patched first.

The immediate read-back said FAILED: `PATCH` returned 200 and the lead still had
22 payload keys and no copy. That was a **false negative**. Instantly's
read-after-write is eventually consistent — the prior `role-display/2` migration
documents exactly this and polls instead of failing closed. Re-read afterwards:

- payload keys 22 → **42**, all five required fields present and correct
- `rendered_subject` = `Store Manager-Bal Harbour`, bodies 343/112/235/240 chars
- `status`, `campaign`, `email`, `timestamp_last_contact`, `email_reply_count`
  and `status_summary` all unchanged; **no pre-existing variable lost or altered**
- the campaign listing agrees independently

So: `PATCH /leads/{id}` with `custom_variables` set to the FULL merged payload is
the correct mechanism (the field is replaced wholesale, which is why the merge is
mandatory), and **a 200 is not proof** — verification must re-read after
propagation. The batch therefore verifies every lead in a separate sweep.

## Step 5b — repair running

`repair_leads.py` is patching the 5,653 repairable leads, paced to the provider's
20-requests-per-minute limit (~3.1s each, so ~4.9h end to end). It is resumable:
each applied lead id is appended to `repair_applied.jsonl`, so a re-run continues
rather than repeating. First 50: **HTTP 200 on all 50**, 40 payload keys sent.

Nothing is deleted, no contact is re-added, no sequence is restarted, no campaign
is modified. The merge base is the pre-repair backup payload, so no existing
variable is dropped, and `verify_repair.py` re-reads every lead afterwards and
compares against that backup key by key.

## Step 5c — DEPLOY IS BLOCKED, deliberately

PR **[#129](https://github.com/TGTChq/GTM/pull/129)** is open against
`feat/rebuild-core` (base verified still at `0ebd7f5`, so the branch sits exactly
on the effective production commit). Merging it was refused by this environment's
`Merge Without Review` guardrail. I did not route around it, and I did not push
to `feat/rebuild-core` directly, because that would reach the same outcome by
another path.

**So the fix is NOT deployed and the effective commit is still `0ebd7f5`.**
Consequences, in order of urgency:

1. The core cron `0 3 * * *` next fires **2026-10-03 03:00Z**. On the current
   image it will create roughly another 1,000 Challenger leads with no copy.
   They cannot SEND, because all nine campaigns are paused — the containment
   holds — but they consume Apollo credits and Instantly lead capacity (the plan
   ceiling is 25,000 stored contacts and the workspace has been at it before) and
   they enlarge the cohort that later needs repairing.
2. Verification that the IMAGE contains `outbound_wave1/` and
   `data/wave1_claims.json` is pending, because Docker is not installed on this
   host and the check is meant to run inside the deployed container.

Either merge #129 before 03:00Z, or hold the cron. I did not change the cron
schedule: a 2026-09-17 "park canary" variable patch is on record as having
deleted every `INSTANTLY_CAMPAIGN_*` variable, so I am not touching Railway
configuration that was not asked for.

## Step 6 — internal test sends: prepared, not sent

Resuming a campaign to test is not available: Instantly has no per-lead pause, so
activating any campaign would also release the leads that still have no copy. A
test therefore needs an isolated campaign holding only internal recipients.

Blocked on two things only the owners can give:

- **which internal addresses to use.** The only internal address anywhere in the
  repo is `luis@globaltalent.co`; the Railway account is `brett@globaltalent.co`.
  I will not guess recipients for real outbound email.
- **approval to create one isolated test campaign** replicating a Challenger
  4-step sequence, since that is a new object in the workspace.

## Step 7 — resumption gate: NOT met

A campaign may only resume when every sendable lead it holds is verified. **1,689
active leads cannot be given copy at all** — no approved copy exists for them,
because their role display fails the frozen QA gates. They are spread across the
campaigns, so no campaign currently passes the gate.

Three options, all of which are a business decision:

1. Repair the underlying role/title data so the renderer accepts them, then
   re-render. Highest effort, keeps the leads.
2. Remove them from the sending population. Deleting contacts was explicitly
   ruled out for this incident, and Instantly has no per-lead pause, so this
   means moving them (the 2026-09 correction used a hold list for exactly this).
3. Leave the affected campaigns paused.

**All nine campaigns remain PAUSED.** Restoration target, to be applied only once
the gate is met: eight to status 1 (ACTIVE) and **FINANCE to status 2 (PAUSED)**,
because FINANCE was already paused before this work began.
