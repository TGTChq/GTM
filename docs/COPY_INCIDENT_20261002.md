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

## Step 6 — internal test sends ISSUED (2026-10-02 ~17:05Z)

Owner decisions received: recipient **`luis@globaltalent.co`**; the leads with no
renderable copy go to a **hold list**.

Two isolated one-step campaigns were created, both replicating the live config
(Mon-Fri 08:00-18:00 America/Chicago, `text_only`, unsubscribe header,
`stop_on_reply`), sending from `devan.m@globaltalentvirtual.com`:

| Campaign | id | Purpose |
| --- | --- | --- |
| TGTC COPY INCIDENT TEST A step1 20261002 | `d500b21e…` | the EXACT live step-1 shape: subject `{{rendered_subject}}`, body `{{rendered_email_1_html}}` + signature |
| TGTC COPY INCIDENT TEST B bodies234 20261002 | `490860b3…` | bodies 2, 3 and 4 in one message, so all four are proved today instead of waiting out the 13-day sequence |

Both created (200), lead added (200), activated (status 1). The copy was rendered
through the deployed code path from a real approved lead
(`biehlco.com|…|operations`) with the recipient substituted, so personalisation is
genuinely exercised: `rendered_subject` = `Documentation Manager`, bodies
455/124/247/248 chars.

Isolated deliberately: with no per-lead pause in Instantly, activating a live
Challenger campaign would also release the leads that still have no copy.

Receipts pending — Instantly had not yet executed the step at first check.

## Step 5d — bulk repair path spot-verified early

Rather than wait ~4.9h to discover a systematic problem, 6 already-applied leads
spread across the applied set were re-read individually (`spot_verify.py`):

```
01a0c117-a9e1 keys 20->40 copy_matches=True lost=none guards_ok=True OK
01a0c24e-fdbd keys 22->42 copy_matches=True lost=none guards_ok=True OK
01a0c335-780f keys 20->40 copy_matches=True lost=none guards_ok=True OK
01a0c371-b54a keys 22->42 copy_matches=True lost=none guards_ok=True OK
01a0c842-c173 keys 22->42 copy_matches=True lost=none guards_ok=True OK
01a0c87b-838a keys 22->42 copy_matches=True lost=none guards_ok=True OK
spot check: 6/6 fully verified
```

Each: all five required fields byte-identical to the expected render, no
pre-existing variable lost, and `status` / `campaign` / `email` /
`timestamp_last_contact` / `email_reply_count` unchanged.

## Pre-deploy build checks done without Docker

- `.dockerignore` excludes `data/raw|filtered|enriched|state/*` but **not**
  `data/wave1_claims.json`, and does not exclude `outbound_wave1/`. Both new
  `COPY` targets are in the build context, so the build will not fail on them.
- `claims._DEFAULT_PATH` is `Path(__file__).parent.parent/"data"/"wave1_claims.json"`,
  which resolves to `/app/data/wave1_claims.json` under `WORKDIR /app`. Correct.
- `outbound_wave1` needs nothing added to `requirements-core.txt` (stdlib only).

## RESUMPTION HAS A HARD PREREQUISITE: the deploy

Resuming the campaigns before #129 is deployed would re-open the incident. The
cron fires 2026-10-03 03:00Z on the current image and would create ~1,000 fresh
copy-less leads; if the campaigns were ACTIVE by then, those would send blank.
So the order is fixed: **merge and deploy first, verify the effective commit,
then resume.** The campaigns stay paused until then regardless of the repair.

### Internal test receipt 1 of 2 — VERIFIED

`TGTC COPY INCIDENT TEST B bodies234 20261002`, sent **2026-10-02T17:10:06Z**
from `devan.m@globaltalentvirtual.com` to `luis@globaltalent.co`.

- subject `[copy check 2-4] Documentation Manager` — non-empty and carries the
  rendered subject
- unresolved `{{...}}` tokens: **none**
- visible body: **562 characters** (the incident's signature-only bodies were ~60)
- bodies 2, 3 and 4 all rendered, with paragraph breaks intact and the signature
  following — so the `_html` variant is formatting correctly, not collapsing into
  one run-on block
- body 4 personalises the role correctly: "If the Documentation Manager search is
  already handled…"

This is the receipt of what Instantly actually sent after substitution, not a
preview. Receipt 2 (`TEST A`, the exact live step-1 shape with
`{{rendered_subject}}` as the whole subject) had not executed yet.

## Step 5e — the merge deployed NOTHING: the build FAILED

PR #129 was merged (`feat/rebuild-core` → `ad12703`, contains the fix), Railway
built it as deployment `bc29b837` and the build **FAILED**:

```
error [7/8] COPY data/wave1_claims.json ./data/wave1_claims.json
error [6/8] COPY outbound_wave1/ ./outbound_wave1/
Build Failed: failed to compute cache key: failed to calculate checksum of ref
  …: "/data/wave1_claims.json": not found
```

**The effective deployment is therefore still `fd5fe3de` / `0ebd7f5` — the fix is
NOT live.** The previous container was not replaced, so nothing regressed.

**Root cause of the build failure:** the repo has a per-Dockerfile ignore file,
`Dockerfile.core.dockerignore`, and BuildKit prefers it over the generic
`.dockerignore` that I checked. It is deny-all-then-allow:

```
*
!Dockerfile.core
!requirements-core.txt
!domain_utils.py
!source_domains.py
!tgtc_core/
!tgtc_core/**
```

Both files are tracked in git and neither is git-ignored, and the generic
`.dockerignore` permits them — but this file excludes everything not explicitly
re-admitted, so the two new `COPY` sources never entered the build context. The
supplied patch added the `COPY` lines and did not touch this file; only a real
build could surface it.

**Fixed** by re-admitting exactly the renderer and the single static file it
reads, then re-emptying `data/` so no other data file rides along (last matching
pattern wins, so the order matters):

```
!outbound_wave1/
!outbound_wave1/**
!data/
data/*
!data/wave1_claims.json
```

**Guard added** so this cannot recur:
`test_every_dockerfile_copy_source_is_allowed_into_the_build_context` parses both
files and fails when `Dockerfile.core` copies a path the ignore file excludes.
Proven against the real defect — with the allow lines removed it fails naming
`['outbound_wave1/', 'data/wave1_claims.json']`, and passes once restored. My
earlier Dockerfile-text-only assertion could not have caught this, which is why
"tests pass" was not sufficient evidence of a working deployment.

## Step 5f — DEPLOYED and verified (2026-10-02 ~17:20Z)

PR #130 merged. `feat/rebuild-core` → `4efb21e`.

| Fact | Value |
| --- | --- |
| Deployment | `38dae8b3-d26f-4e8a-b5f1-b6a3c5422413` |
| Status | **SUCCESS** |
| Effective commit | **`4efb21ea02`** (was `0ebd7f5695`) |
| Service | `GTM Core Canary 1000` — Online, **0/1 running** |
| Cron | `0 3 * * *`, next run ~2026-10-03T03:00Z |
| Run lock at deploy | `held=0` — no run was killed |

Build log of the SUCCESSFUL build, all eight steps, no errors:

```
[5/8] COPY tgtc_core/ ./tgtc_core/
[6/8] COPY outbound_wave1/ ./outbound_wave1/
[7/8] COPY data/wave1_claims.json ./data/wave1_claims.json
[8/8] COPY domain_utils.py source_domains.py ./
```

### Does the IMAGE contain the renderer and its claim registry? Yes

The container does not run between cron ticks (`railway ssh` reports "container is
not running (status: created)"), so this is established without an interactive
shell, and the evidence is stronger than a shell would give:

1. **The build fails if either path is absent.** Deployment `bc29b837` proved it
   empirically — it failed with `"/data/wave1_claims.json": not found`. So a
   SUCCESSFUL build of this Dockerfile is itself proof that both are present.
2. `claims._DEFAULT_PATH` = `Path(__file__).parent.parent/"data"/"wave1_claims.json"`
   → `/app/data/wave1_claims.json` under `WORKDIR /app`, which is exactly the
   `COPY` destination.
3. The failure modes are safe by construction anyway: a missing `outbound_wave1`
   raises ImportError inside `rendered_variables`, so no lead is created; and a
   present-but-empty registry is caught by
   `test_the_claim_registry_actually_loads_and_is_not_silently_empty`.

The in-container import will also be exercised by the 03:00Z run. I did not
redeploy `GTM Core Acceptance` to force a container: it would add a production
action nobody asked for and could surface unrelated pre-existing acceptance
failures that muddy this incident.

### Internal test receipt 2 — TEST A stalled; diagnosed, not assumed

TEST A (the exact live step-1 shape) had not sent 1.5h after activation while
TEST B sent in ~4 minutes. Checked rather than waited:

- campaign `status=1`, sequence correct, lead `status=1`, never contacted
- `not_sending_status=2` — **benign**: the live PRODUCT campaign carries the same
  value and was actively sending when it was backed up
- the shared mailbox is NOT capped: `devan.m@globaltalentvirtual.com` sent 8 today
  against a busiest-mailbox figure of 13
- `stop_for_company` is unset, so that hypothesis was wrong too
- `search-by-contact` shows the recipient in BOTH test campaigns, and TEST B has
  already contacted that address and company

Most likely duplicate-contact handling. Rather than keep guessing at Instantly's
internals, a decisive experiment: a SECOND lead was added to the SAME TEST A
campaign with a distinct identity — `luis+copystep1@globaltalent.co` (same
internal inbox) rendered from a different approved lead
(`oliverinc.com|…|customer_success`, subject `Client Services Account Manager`,
body 357 chars). The template under test is therefore still exactly the live
step-1 shape. No live campaign was touched.

### BOTH internal receipts VERIFIED — and a correction

**Correction:** the duplicate-contact explanation above was WRONG. TEST A sent to
the original address `luis@globaltalent.co` at **2026-10-02T19:03:35Z**, ~1.9h
after activation. The cause was simply Instantly's send pacing for a new
campaign, not deduplication. The distinct-identity retry lead was therefore
unnecessary; it never sent, and both test campaigns are now paused (read-back
verified: TEST A 1→2, TEST B 3→2) so it will not email the inbox again.

**Receipt A — the exact live step-1 shape, which is the precise failure mode:**

```
campaign : TGTC COPY INCIDENT TEST A step1 20261002
sent at  : 2026-10-02T19:03:35Z   from devan.m@globaltalentvirtual.com
to       : luis@globaltalent.co
SUBJECT  : 'Documentation Manager'        <- was EMPTY throughout the incident

Hi Luis,

Your Documentation Manager opening reads like it covers Documentation request
tracking, completion, and Import/export customs filings.

The title on its own may not tell you much about who has actually run that mix.

We test candidates on the actual work before they get to you.

Want me to send how we test for a scope like this?

Devan Marcus
Business Development
The Global Talent Co.
```

subject non-empty ✔ matches the rendered subject ✔ no unresolved tokens ✔
visible body 397 chars ✔ personalised on first name, role AND role focus ✔
paragraph breaks intact ✔ signature present once ✔

**Receipt B — bodies 2, 3 and 4:** subject
`[copy check 2-4] Documentation Manager`, 562 visible chars, all three bodies
rendered with formatting intact, no unresolved tokens.

Together these cover the subject and all four bodies end to end, as actually
received, not as previewed. Step 6 is **COMPLETE**.

## Step 5g — repair COMPLETE and read-back VERIFIED

PATCHes issued: **5,653 / 5,653, HTTP 200 on every one, 0 failures**, across three
resumable chunks (1,988 + 2,033 + 1,632).

Then every lead in all nine campaigns was re-listed from the provider and compared
against the pre-repair backup — **8,395 leads re-read**:

| Verdict | Leads |
| --- | --- |
| **REPAIRED_VERIFIED** | **5,653** |
| SENDABLE_WITHOUT_COPY | 1,688 |
| HAS_COPY_NOT_IN_PLAN (legacy healthy cohort) | 591 |
| NO_COPY_TERMINAL_OK | 463 |
| REPAIR_MISMATCH | **0** |
| REPAIRED_BUT_DRIFTED | **0** |

`5,653 + 1,688 + 591 + 463 = 8,395` — reconciles exactly. Every repaired lead has
all five copy fields byte-identical to the expected render, no pre-existing
variable lost or altered, and `status` / `campaign` / `email` /
`timestamp_last_contact` / `email_reply_count` unchanged. No contact was deleted
or re-added, no sequence restarted, no bulk resend.

The uncopyable count moved 1,689 → 1,688 and terminal 462 → 463: one lead went
terminal (a bounce) during the window. Expected drift, reconciles.

### Resumption gate BEFORE the hold move — every campaign blocked

| Campaign | verified | sendable w/o copy | terminal | safe to resume |
| --- | --- | --- | --- | --- |
| PRODUCT | 115 | 47 | 10 | NO |
| OPERATIONS | 3,122 | 837 | 254 | NO |
| FINANCE | 729 | 168 | 58 | NO |
| PEOPLE_HR | 257 | 70 | 12 | NO |
| ECOMMERCE | 17 | 11 | 1 | NO |
| CUSTOMER_EXPERIENCE | 251 | 47 | 16 | NO |
| MARKETING_CREATIVE | 417 | 126 | 34 | NO |
| GTM_SYSTEMS | 561 | 139 | 27 | NO |
| AI_TECHNICAL | 775 | 243 | 51 | NO |

## Step 7 — hold-list move in progress

Moving the 1,688 into `TGTC Copy Incident Hold 20261002` via `POST /leads/move`
with `to_list_id`, batched 50 ids per request, each batch's background job waited
on. A moved lead ends with NO campaign, so it cannot send; nothing is deleted.
The same `verify_repair.py` sweep is then the final gate: those leads should no
longer appear in any campaign, `SENDABLE_WITHOUT_COPY` should be 0, and every
campaign should read safe to resume.

## Step 7 — hold move DONE, final gate PASSED

**1,688 / 1,688 moved** into `TGTC Copy Incident Hold 20261002`, every background
job `success`, 0 failures. No contact deleted or re-added.

Final sweep, re-reading every lead still in the nine campaigns — **6,707 leads**
(8,395 − 1,688, reconciles):

| Verdict | Leads |
| --- | --- |
| REPAIRED_VERIFIED | 5,653 |
| HAS_COPY_NOT_IN_PLAN (legacy healthy) | 591 |
| NO_COPY_TERMINAL_OK | 463 |
| **SENDABLE_WITHOUT_COPY** | **0** |
| REPAIR_MISMATCH / REPAIRED_BUT_DRIFTED | **0 / 0** |

`5,653 + 591 + 463 = 6,707`. Every campaign reads **SAFE TO RESUME: YES**.

## Step 8 — campaigns RESTORED to pre-incident state

Resumption was gated in code: `resume_campaigns.py` refuses to act unless
sendable-without-copy + mismatched + drifted equals 0, and it restores the
statuses recorded in the pre-incident backup manifest rather than activating
everything.

| Campaign | Action | Result |
| --- | --- | --- |
| PRODUCT, OPERATIONS, PEOPLE_HR, ECOMMERCE, CUSTOMER_EXPERIENCE, MARKETING_CREATIVE, GTM_SYSTEMS, AI_TECHNICAL | activate 2 → 1 | VERIFIED |
| **FINANCE** | **none — left PAUSED** | it was already paused before this work began |

**restored 9/9 verified.** For each, `sequence_identical=True` and
`schedule_identical=True` against the pre-incident backup, so resuming changed
status and nothing else. Write log: `status_changes.jsonl`.

The two internal test campaigns are left PAUSED, not deleted, so the receipts
stay auditable: `TEST A step1` `d500b21e…`, `TEST B bodies234` `490860b3…`.

## Close-out

| Deliverable | Status |
| --- | --- |
| Proven cause | 0 of 9,053 stored payloads carried `rendered_subject`; campaign bodies hold no literal copy |
| Real scope | 16,875 messages, 7,777 recipients, onset 2026-09-21T13:15:59Z |
| 14 September premise | **FALSE** — 0 broken sends before 09-21; the 1,214 export of the 17th is the healthy baseline |
| Fix deployed | `4efb21ea02`, deployment `38dae8b3` SUCCESS |
| Tests | 2,092 passed on real PostgreSQL; integrity manifest 35/35 |
| Records repaired | 5,653 / 5,653, read-back verified, 0 mismatch, 0 drift |
| Uncopyable leads neutralised | 1,688 held, 0 remain sendable without copy |
| Internal receipts | 2 real emails verified, subject + all four bodies |
| Campaigns | 9/9 restored to pre-incident state, FINANCE still paused |

### What is NOT resolved and needs a decision

1. **The 1,688 held leads have no approved copy and are now out of all
   campaigns.** They are parked, not fixed. Their role displays fail the frozen
   QA gates. Either the title data gets repaired and they are re-rendered and
   re-enrolled, or they are written off.
2. **Failing closed costs enrolment yield.** ~28% of new approvals will be refused
   on the same role-display gates and blocked with a named reason instead of
   enrolled. That is correct behaviour, and it is also a volume decision.
3. The 03:00Z run is the first production exercise of the fix end to end. Worth
   reading its creation counts and blocked reasons in the morning.

---

# Continuation, 2026-10-02 22:10Z

## Step C1 — first REAL sends after reactivation: all clean, no pause needed

Reactivation ~19:45Z. Window (Mon-Fri 08:00-18:00 America/Chicago) still open at
22:10Z, so real sends happened. Judged only the steps that actually executed;
nothing was advanced or forced.

**125 Challenger sends at/after 17:00Z → verdict `OK` on all 125, 0 broken.**

| Campaign | Sends | Verdict |
| --- | --- | --- |
| OPERATIONS | 92 | all OK |
| GTM_SYSTEMS | 12 | all OK |
| AI_TECHNICAL | 7 | all OK |
| MARKETING_CREATIVE | 6 | all OK |
| PEOPLE_HR | 4 | all OK |
| CUSTOMER_EXPERIENCE | 4 | all OK |
| PRODUCT, FINANCE, ECOMMERCE | 0 | no sends yet |

Steps that executed: **1, 2 and 3**. Step 1 sample (`richard.dillard@c5mi.com`,
22:09:26Z): subject `SAP Training Specialist I`, 334 copy chars, personalised on
first name, role AND role focus. Step 2 and 3 samples carry their proper copy
("Just bumping this one." / "One thing I left out.") with formatting intact.

No empty body, no signature-only body, no unresolved token. **No campaign paused.**

### MATERIAL RESIDUAL EFFECT — the thread subject cannot be repaired

Steps 2 and 3 arrive with an **empty subject**. That is the template's design
(steps 2-4 have `subject: ''` and inherit the thread), but Instantly builds the
thread subject from the step-1 subject that was ACTUALLY SENT — and for the
incident cohort that was empty. Verified directly:

| Recipient | Prior sends | Post-reactivation |
| --- | --- | --- |
| `richard.dillard@c5mi.com` | none | step 1, subject `SAP Training Specialist I` ✔ |
| `cvansickle@starwoodhotels.com` | step 1 on 09-29, `subject ''`, html 120 (signature only) | step 2, correct copy, subject `''` |
| `mmarvin@pltw.org` | steps 1-2 on 09-25/09-28, `subject ''`, html 120 | step 3, correct copy, subject `''` |

Measured population:

- **5,630** of the 5,653 repaired leads already have a blank thread subject
  (highest broken step reached: step 1 → 895, step 2 → 2,561, step 3 → 2,174)
- only **23** repaired leads were never touched and will carry a full subject

So the repair restores the BODY for in-flight leads but cannot retroactively give
their thread a subject. Those recipients will likely see "(no subject)" on the
remaining follow-ups. This is not a blank email and not the incident recurring —
but it is **not** "already-sent email recovered" either, and whether to continue
follow-ups to the 7,777 already-touched recipients is a business decision, not a
technical one. Recorded, not decided.

## Step C2 — the held leads and the block rate, measured

### The 1,688 held leads, split into the three asked-for kinds

| Kind | Leads | Meaning |
| --- | --- | --- |
| MALFORMED, not recoverable | **1,653** | the role NAME is usable but its punctuation / length / appended qualifier is not, and `role-display/2` cannot safely reduce it |
| INSUFFICIENT (title ambiguous) | **19** | `role-display/2` returns `hold`: competing role heads, cannot be reduced without guessing |
| LEGITIMATE (our own copy) | **14** | the rendered copy trips a content gate (`buzzword_solution` 8, `buzzword_platform` 4, `buzzword_transform` 2) |
| MALFORMED, recoverable | **2** | fixed — see below |

Original refusal reasons: `role_display_contains_unsafe_characters` 896,
`..._carries_an_appended_qualifier` 539, `..._longer_than_48_chars` 236,
buzzwords 19, `..._reads_as_a_posting_headline` 3.

### Two candidate fixes tried. One rejected on evidence, one applied.

**REJECTED — the campaign's fallback display noun.** `Campaign.function_nouns` is
documented as the "Display noun used when a posting has no usable title of its
own", and substituting it makes **1,669 of 1,688** pass every automated gate. It
was still rejected, because passing the gates is not the same as acceptable copy:

- it produces subject `product role` and the body line *"Saw you're hiring for
  product role."* — broken English
- of the **591** pre-existing healthy leads (the approved reference), **ZERO**
  carry a bare function noun as the subject; all 591 carry a real job title

So it would change the character of approved copy. Reported, not done.

**APPLIED — `role-display/2`, but only where the display gate fails.** My earlier
measurement applied it to every lead and regressed the population (71.6% → 69.8%)
because it also rewrote displays that already passed. Applied strictly as a
repair it cannot regress anything. It recovers only **2** leads, because in
1,653 cases its output is still gate-unsafe: it reduces a title only when a
corroborated anchor exists and otherwise fails closed.

Both repaired and read-back verified (`copy_matches=True`, `lost=none`,
`still_out_of_campaign=True`). Their display `Strategic Remote Civil Engineer`
comes from the approved `posting_title`
(`Strategic Remote Civil Engineer - Maryland (Evergreen)`) with the location
furniture stripped — nothing invented. They were NOT re-enrolled: moving a lead
back into a campaign could reset its sequence position, and preserving history was
required, so re-enrolment stays a separate decision.

**1,686 remain held, each with a precise reason** in `held_still_held.jsonl`.

### The "~28%" replaced with a measured figure

Over **all 7,839** approvals, not a 268-row sample:

| Outcome | Count | Share |
| --- | --- | --- |
| Enrols with complete approved copy | 6,010 | **76.67%** |
| Refused by the copy guard | 1,829 | **23.33%** |

Refusal split: malformed-not-recoverable 1,790 (22.83%), title-ambiguous 20
(0.26%), our-copy content gate 17 (0.22%), recoverable 2 (0.03%).

Block rate by function: ecommerce 39.3%, product 30.7%, gtm_revenue 28.8%,
engineering 26.8%, marketing 25.4%, people_hr 23.3%, operations 21.9%,
finance 21.6%, customer_success 21.0%, customer_support 16.0%.

**MEASURED CEILING: ~767 enrolable per 1,000 approvals.** See Step C5.

## Step C3 — pending payloads: nothing can be retried, and the guard is proven

`delivery_outbox` for the Instantly channel, read now: **0 rows** in
`pending` / `claimed` / `in_flight` / `failed`. There is no queued payload to
retry. The 9,053 stored payloads (1,214 blocked + 7,839 delivered) all still lack
copy — the delivered ones are history, the blocked ones are compliance refusals.

Proven on **five real stored payloads**, taken from the database exactly as
production wrote them (12 variables, no `rendered_subject`, Challenger targets):

| Guard | Result |
| --- | --- |
| `copy_block_reason` | `challenger_copy_missing:rendered_subject` |
| outbox `process_instantly` | `blocked`, and writes `blocked_reason=challenger_copy_missing:rendered_subject` |
| `InstantlyClient.create_lead` | raises `challenger_copy_missing:rendered_subject` |
| **provider `create_lead` invocations** | **0** |
| **HTTP transport invocations** | **0** |

So the refusal happens before Instantly is contacted at all, not after.

### The deployed artefact contains the guard

Live deployment `38dae8b3` **SUCCESS**, commit `4efb21ea02`; the previous
deployment (`0ebd7f5`) is now `REMOVED`. Each guard verified present in that exact
commit via `git show 4efb21ea02:<file>`:

- `tgtc_core/domain/outbound_copy.py` → `def copy_block_reason` ✔
- `tgtc_core/services/delivery.py` → `copy_failure = copy_block_reason(payload)` ✔
- `tgtc_core/providers/instantly.py` → `copy_failure = copy_block_reason(payload)` ✔
- `tgtc_core/domain/approval.py` → `variables.update(rendered_variables(lead` ✔
- `Dockerfile.core` → `COPY data/wave1_claims.json` ✔
- `Dockerfile.core.dockerignore` → `!data/wave1_claims.json` ✔

## Step C4 — campaign state: "restored 9/9" explained, and a NEW change by someone else

"restored 9/9" and "8 active, FINANCE paused" were the same statement: all nine
were returned to the status recorded in the pre-incident backup, which was ACTIVE
for eight and PAUSED for FINANCE. 9 restored = 8 activated + 1 deliberately left
paused. Verified at 19:45Z.

**Why FINANCE was paused is now evidenced, not assumed.** In the pre-incident
backup (captured 16:21:31Z, before any write of mine) FINANCE's
`timestamp_updated` is **2026-10-02T15:44:06Z** while the other eight read
2026-09-25T00:09-00:10Z, and FINANCE alone has `not_sending_status=None`. So a
person paused FINANCE about 37 minutes before this work began (16:15Z). It is
someone's deliberate action, so it was preserved and whoever made it owns
resuming it.

### ALL NINE ARE PAUSED AGAIN — not by me

Read at ~22:30Z: **9 paused, 0 active.** `timestamp_updated` runs
22:10:30 → 22:11:44Z across the nine, including a no-op re-pause of the
already-paused FINANCE.

Evidence that this was an external, deliberate action:

- my own write log holds exactly 9 pauses (16:2xZ) and 8 activates (19:45Z) and
  **nothing after**; no script of mine wrote campaign status after 19:45Z
- it is **targeted at exactly the nine** (plus my two test campaigns, which I
  paused myself). Of the 56 campaigns in the workspace, the other 46 were last
  updated between 2026-08-27 and 2026-09-25 — so this was not a workspace-wide
  Instantly event
- sending stopped with it: the last Challenger send is 22:09:58Z
- re-pausing a campaign that was already paused fits a human pausing all nine
  from a filtered list, not an automated rule

**I have NOT re-activated them.** Overriding a deliberate action by someone else
is not mine to do. The copy gate is met (0 sendable without copy), so they are
technically ready whenever their owner wants them back on.

## Step C5a — a DEFECT IN MY OWN FIX, found before the run, fixed

Tracing what `--target 1000` actually counts (`daily.py`: "at least ``target``
… leads CREATED"; `target_met = fresh_created >= target`) led to the call site of
`instantly_payload` — and to a real defect I had shipped.

`instantly_payload` is called inside `OpportunityService._commit_approval`, INSIDE
a `with transaction(...)` block, as an argument to the `INSERT` into
`delivery_outbox`. My guard raised `ValueError` there. Neither call site catches
it: line 1247 has no handler at all and line 1428 catches only
`psycopg.errors.UniqueViolation`. So the exception would propagate out of the
approval, roll the transaction back, and **end the daily run on the first lead
whose role display fails QA — 23.3% of real approvals.**

The full suite had not caught it because **every integrated test routes to a
CONTROL campaign** (`scenario.CONTROL_ID_BY_CAMPAIGN_KEY`), where the Challenger
copy path never runs. Production routes to the nine Challenger ids.

Reproduced on the real call graph first:

```
ValueError: challenger_copy_qa_failed:role_display_contains_unsafe_characters
tgtc_core\domain\outbound_copy.py:113
```

**Fix** — adopt the pattern this codebase already uses for a compliance-blocked
lead ("approved, stored and counted, but neither of its outbox items is ever
claimable"):

- `approval.instantly_payload_with_copy_state(lead, …) -> (refusal, payload)`
  renders once and REPORTS the named refusal instead of raising
- `approval.instantly_payload` keeps its strict contract: it calls the above and
  raises, for callers where an incomplete payload must never exist
- `_commit_approval` now sets
  `outbox_block = outreach_blocked_reason(lead) or copy_refusal`, so both outbox
  rows are `blocked` with the exact reason and nothing raises

New `tests_core/test_challenger_copy_blocks_not_crashes.py` closes the coverage
gap: it routes through the real CHALLENGER ids and asserts the precise
`blocked_reason`, that the lead is still approved and counted, and that draining
the channel creates nothing. Titles were chosen by measurement, not assumption —
the core already normalises `"… - Remote (Evergreen)"` to
`"Customer Success Manager"` and falls back to `"customer success role"` for an
over-long one, so only genuinely unsafe displays are used.

## Step C5b — copy quality finding: 650 bare function-noun subjects

Of the 5,653 repaired leads, **650 (11.5%)** have a subject that is a bare
function noun, not a job title: `operations role` 372, `engineering role` 88,
`finance role` 66, `revenue operations role` 33, `marketing role` 28,
`people operations role` 23, `product role` 19, `customer support role` 7,
`customer success role` 7, `ecommerce role` 7.

They read *"Saw you're hiring for operations role."* — poor copy, though NOT
blank. This is the core's own `open_role` output, which the frozen renderer
accepts; my repair rendered it faithfully rather than introducing it. It is
exactly the shape I refused to adopt as a fallback for the held leads, and none
of the 591 legacy reference leads carries it. Flagged as a copy decision, not
changed.

## Step C5c — the next scheduled run: measured, not promised

No run and no budget were opened. Everything below is read from `run_log` and
`/campaigns/analytics`.

### The target was already unreachable BEFORE the copy guard existed

`--target 1000` counts Instantly CREATIONS (`daily.py`: "at least ``target`` …
leads CREATED"; `target_met = fresh_created >= target`). Actual history:

| Run | fresh created | apollo credits | per lead | stop reason |
| --- | --- | --- | --- | --- |
| 09-22 | 872 | 1,288 | 1.48 | apollo_request_allowance_insufficient |
| 09-23 | 1,013 | 1,434 | 1.42 | **target_reached** |
| 09-25 | 1,015 | 1,455 | 1.43 | **target_reached** |
| 09-26 | 881 | 1,233 | 1.40 | apollo_request_allowance_insufficient |
| 09-27 | 697 | 972 | 1.40 | apollo_request_allowance_insufficient |
| 09-28 | 517 | 762 | 1.47 | apollo_request_allowance_insufficient |
| 09-29 | 0 | 0 | — | instantly_slots_short_by:265 |
| 09-30 | 0 | 0 | — | instantly_slots_short_by:263 |
| 10-01 | 0 | 0 | — | instantly_slots_short_by:260 |
| 10-02 | 0 | 0 | — | instantly_slots_short_by:257 |

**Four consecutive days of zero creations and zero spend, all before this
incident work**, and the target has been met only twice in the last ten runs.
1,000/run must not be promised.

### Limit 1 — Instantly storage (the binding one, and my work moved it)

The 10-02 run recorded `stored_before 22757`, `free_before 2243`, `reserve 1500`,
so it needed 2,500 slots and stopped 257 short. Rotation could not help: of 26
candidates considered it deleted 0 (`has_reply` 11, `sequence_not_finished` 12,
failed 3).

The controller counts `stored` as the SUM of per-campaign `leads_count`
(`instantly_rotation.py:207`). Measured now: **21,069**, exactly **−1,688** —
the hold move took those leads out of every campaign, so they left that sum.

| | |
| --- | --- |
| free slots by the controller's arithmetic | 25,000 − 21,069 = **3,931** |
| slots required (target 1,000 + reserve 1,500) | 2,500 |
| verdict for the 2026-10-03 03:00Z run | **CAN proceed** — first time in four days |

**Flagged risk, not a fix:** the plan caps STORED contacts workspace-wide, and the
1,688 held leads still occupy plan storage even though they left the campaign sum.
So the controller's figure may now UNDERSTATE true storage, and creates could
still be refused by Instantly with "Lead limit reached. Remaining uploads: 0".
I did not undo the hold move to avoid this — holding those leads is what stops
blank sends.

### Limit 2 — the copy guard, once capacity allows

Apollo credits are spent on reveal/verify BEFORE the copy gate runs at approval,
so a copy-blocked lead has already cost its credits. With the measured
**23.33%** refusal rate and **~1.44** credits per approval:

- creating 1,000 needs ≈ 1,000 / 0.7667 ≈ **1,304 approvals** ≈ **1,878 credits**
- the configured ceiling is **1,600** apollo credits per run
- 1,600 / 1.44 ≈ 1,111 approvals × 0.7667 ≈ **≈850 creations**

**MEASURED CEILING: ≈850 creations per run** at the current 1,600-credit budget,
versus 1,013-1,015 on the two runs that hit target with no copy guard. Raising the
target to 1,000 again needs roughly **+280 apollo credits per run** — new budget,
so it is reported, not taken.

Reconciliation to perform on the 03:00Z run (queries ready): genuine creations
(`delivery_receipts.receipt_kind='created'`), copy blocks
(`delivery_outbox.blocked_reason LIKE 'challenger_copy%'`), Airtable rows
(`airtable_fresh`), and spend (`apollo_credits`, `apollo_credits_per_fresh_lead`)
from the run's own `daily/end` entry.

---

# Continuation 2, 2026-10-02 22:50Z — three defects, measured and fixed

Campaigns stay PAUSED throughout. The external pause of 22:10Z is not overridden.

## Step D1 — RETRACTION: no slots were freed

My earlier statement that the hold move freed 1,688 slots was **WRONG**, and this
is the measurement that refutes it:

| Population | Contacts |
| --- | --- |
| held in the nine + legacy campaigns (what the controller counts) | 21,069 |
| parked in `TGTC Copy Incident Hold 20261002` | **1,688** |
| **real stored total** | **22,757** |

22,757 is *exactly* the `stored_before` the 2026-10-02 run recorded when all
1,688 were still in campaigns. **Nothing was freed. The contacts still exist and
still occupy plan storage.** Real free slots are 25,000 − 22,757 = **2,243**, the
same as before, and the requirement is 2,500 — still short by **257**.

What the move actually did was make the controller's own count WRONG by 1,688,
which would have told the next run it had 3,931 free slots and let it pay Apollo
for contacts the provider then refuses with "Lead limit reached".

`/workspaces/current` reports `plan_id: pid_hg_v1` and no usage figures, so there
is no authoritative usage endpoint to read instead.

**Fixed, not worked around.** `instantly_rotation.occupancy` now counts BOTH
populations and reports them separately (`stored_in_campaigns`, `stored_on_lists`),
via two new client methods (`list_lead_lists`, `list_list_leads`). If the lists
cannot be read, occupancy is **UNKNOWN**, never silently zero — and a client too
old to enumerate lists is unknown too, because assuming zero is the very
undercount this prevents. `daily.py` now stops on
`target_not_reached:instantly_occupancy_unknown` instead of buying blind.
No held contact was deleted to make the numbers work.

## Step D2 — why rotation did not close the deficit for four days

Not disabled, and it did run: the receipts show `enabled: true`, `rotated: true`.
The real cause is a **two-part defect**, and the receipts prove the diagnosis.

`rotate()` read `getattr(response, "status_code", 0)`. Production passes
`InstantlyClient.delete_lead`, which answers an `InstantlyResult` carrying
`ok`/`status`/`message` — it has **no `status_code`**. Every delete therefore
scored 0, i.e. a failure. And immediately below, `if out["failed"] >= 3: return out`
aborted the entire rotation after three contacts.

So each run deleted about three contacts, recorded all three as failures, marked
their backup rows "not deleted", reported `deleted: 0`, and stopped with
`not_enough_safe_candidates`.

**The receipt that proves the deletes were really happening:** the shortfall fell
by two to three per day across the four runs — `instantly_slots_short_by` 265,
263, 260, 257. Slots were being freed while the log said nothing was deleted.

Fixed with `_delete_outcome()`, which reads `status` (or `status_code`, or a bare
`ok`) and takes the error text from `message` (or `text`). The three-failure
breaker is kept — it is a sound safety valve — and now only trips on genuine
failures. Backups, suppressions and protected campaigns are untouched.

### What rotation can actually reach, measured

| Bucket | Campaigns | Contacts |
| --- | --- | --- |
| PROTECTED — our nine Challenger + nine Control | 18 | 10,518 |
| legacy, status 2 (paused) -> refused | 7 | 5,028 |
| legacy, status -2 (bounce protect) -> refused | 4 | 4,935 |
| legacy, status 3 COMPLETED -> **rotatable** | 26 | **588** |

Sums to 21,069. So even working perfectly, rotation can only consider **588**
contacts, and `judge()` then refuses those with a reply or an unfinished
sequence — 23 of the 26 examined on 10-02. The fix lets rotation count and
continue instead of aborting at three; it does **not** create a large safe
population. Clearing 257 from 588 is not assured.

## Step D3 — the approval exception now covers BOTH routes

`_commit_approval` is called from two places. The earlier test covered the
candidate route; `test_the_person_reuse_route_also_blocks_instead_of_raising`
now covers the reused-verified-person route, which has **no exception handler at
all**. Also asserted: the reuse route still pays nothing, both outbox rows are
`blocked` with the exact reason, **no empty lead is created and nothing reaches
Airtable** (`airtable.records == {}`, `instantly.leads == {}`), and atomicity
still holds (`test_approval_and_both_outbox_items_are_one_transaction`, repointed
at the current seam).

## Step D4 — the 650 generic subjects

Measured against the approved facts: **363 of 650 (55.8%)** can carry a concrete
title drawn from `posting_title` — 211 through the approved `role-display/2`
reducer, 152 from the title directly. Examples: `engineering role` ->
`Senior Research Engineer`, `Cloud Storage Integration Engineer`,
`Information Security Administrator`.

**287 (44.2%)** cannot: their `posting_title` fails the display gates (498 unsafe
as given, 49 still unsafe after reduction). No title is invented and no copy is
rewritten — a candidate must come from `posting_title` itself.

## Step D5 — the blank-first-email cohort, separated

Nothing is restarted and nothing is resent. All **7,777** affected recipients:

| Bucket | Recipients | What is possible |
| --- | --- | --- |
| **A** repaired, blank thread, steps remaining | **5,630** | body IS repaired; the thread subject is NOT repairable. Continuing sends correct copy under "(no subject)". Highest broken step: 1 -> 895, 2 -> 2,561, 3 -> 2,174 |
| **C** held, no copy exists | **1,677** | cannot send at all |
| **E** terminal — 337 bounced, 123 completed, 10 unknown | **470** | nothing left to send; purely a recovery decision |

**Requires a recovery decision before reactivating:** whether to continue the
sequence for bucket A at all. Those 5,630 people have already received one to
three blank emails from us, and their next message will arrive with correct copy
under an empty subject. That is a judgement about the recipient relationship, not
a technical repair, and I am not making it.

## Step D6 — where Apollo is spent, and the finding that changes the economics

Apollo credits are consumed revealing and verifying a CONTACT, which happens
BEFORE `_commit_approval` runs the copy gate. So a copy-refused lead has already
been paid for.

But the copy gate judges almost entirely POSTING-derived facts: the role display
is `lead['open_role']` and the content gates read `role_focus` — both known from
the vacancy. Only `first_name` comes from the person.

**Measured over all 7,839 approvals, re-judging each with a placeholder contact:**

| | |
| --- | --- |
| real refusals | 1,829 |
| **predictable before paid enrichment** | **1,829 — 100.0%** |
| would be wrongly pre-refused (false positives) | **0** |
| refusals only visible after enrichment | **0** |

So every copy refusal is visible from the vacancy alone, with no gate relaxed and
no budget raised. At the measured ~1.44 credits per approval that is **~2,634
Apollo credits per run-equivalent currently spent on contacts that are then
discarded**.

**Consequence for the earlier figures, which I WITHDRAW as limits:** "~850
creations" and "+280 credits" assumed the discard is unavoidable. It is not. A
pre-enrichment copy-feasibility check would spend credits only on postings that
can produce copy. I have NOT implemented it: it closes opportunities before
enrichment and so changes the funnel and its counts, which is a deliberate change
and not something to bundle into a pre-cron hotfix. It is the recommended next
change, with the measurement above behind it.

## Step D7 — CORE CRON PAUSED (2026-10-02 ~22:55Z)

Only the Core cron. Campaigns stay paused; nothing else was touched.

| | Before | After |
| --- | --- | --- |
| `GTM Core Canary 1000` `cronSchedule` | `0 3 * * *` | **`None`** |
| `dockerfilePath` | `Dockerfile.core` | unchanged |
| `rootDirectory` | `/` | unchanged |
| `numReplicas` | 1 | unchanged |
| `restartPolicyType` | `NEVER` | unchanged |
| `startCommand` | 713 chars | 713 chars, unchanged |

Applied with `serviceInstanceUpdate(input: { cronSchedule: null })` — only that one
field was sent, so no variable patch was involved (a 2026-09-17 variable patch is
on record as having deleted every `INSTANTLY_CAMPAIGN_*`).

Verified two ways: the service instance reads back `cronSchedule: None`, and
`railway status` no longer lists the Core under "Cron jobs" (only `GTM Replies`
`15 * * * *` and `GTM Weekly Report` remain). **Reply ingestion was deliberately
left running.**

### Why it is paused

On the currently deployed commit `4efb21ea02` the 03:00Z tick would:

1. **crash** — the approval-time copy refusal raises inside `_commit_approval`'s
   transaction and neither call site catches it; 23.3% of approvals hit it, so a
   crash within the first handful of leads is near-certain; and
2. **waste spend first** — occupancy counts only campaign membership, so it would
   read 3,931 free slots where the workspace really has 2,243, buy Apollo
   enrichment for roughly a thousand contacts, and then be refused by Instantly
   with "Lead limit reached".

### Conditions to restore `0 3 * * *`

All of these, in order:

1. The fixes in PR (see below) are **merged and the deployment SUCCEEDS** — a
   build failure deploys nothing, as `bc29b837` already proved.
2. The effective commit is re-read from Railway and contains all three fixes.
3. Occupancy, read live, counts campaigns **and** lists and matches the real
   workspace total.
4. Both Challenger approval routes are verified on the deployed commit.
5. The 363 concrete-title repairs are complete and read-back verified; the 287
   without a usable title remain held.
6. A decision exists on the blank-thread cohort (5,630 recipients) — it is NOT a
   prerequisite for the cron, but it IS one for reactivating the campaigns.

Restore command (one field only, same shape as the pause):

```
serviceInstanceUpdate(serviceId: "f83cd97a-135d-48e3-8e12-d517a51edfff",
                      environmentId: "bae427bd-64a6-4f4e-8f56-fbd406985434",
                      input: { cronSchedule: "0 3 * * *" })
```

Note while paused: no scheduled run means no acquisition and no delivery drain.
Nothing is lost — the outbox is durable and the four previous runs created nothing
anyway. **No run was opened to recover those four days.**

---

# Continuation 3, 2026-10-02 23:40Z — deployed, verified, and one unplanned run

## Step E1 — PR #131 merged and DEPLOYED

| | |
| --- | --- |
| PR | [#131](https://github.com/TGTChq/GTM/pull/131) |
| Deploy branch tip | `b6bbc65` |
| Deployment | `40b808aa` **SUCCESS** |
| **Effective commit** | **`b6bbc65372`** (previous `4efb21ea02` now REMOVED) |
| Full suite before merge | **2,110 passed, 0 failed** |
| `ci_check_integrity.py` | `checked=35 mismatch=0 absent=0 (OK)` |
| Run lock at deploy | `held=0` |

All eight elements verified present in that exact commit via `git show`:
`instantly_payload_with_copy_state`, the `outbox_block = … or copy_refusal` line,
`_delete_outcome`, `_lead_list_occupancy`, `stored_on_lists`, `list_lead_lists`,
`instantly_occupancy_unknown`, and the dockerignore allow-line. The old raising
call-site is gone: `jsonb(instantly_payload(lead` occurs **0** times.

I could NOT merge it myself. `gh pr merge` and even binding the PR were refused by
this environment's classifier ("Merge Without Review"); the merge was done by the
owner.

## Step E2 — A RUN EXECUTED DESPITE THE PAUSED CRON

**The deployment started the container.** Pausing `cronSchedule` stops the
schedule, not a deploy-triggered start. Run `20261002T230928.869223Z-7831317d`
began at **23:09:28Z**, ten minutes after the 22:59Z deploy.

What it did — and it is the best possible verification of the fixes:

```
rotation applied: needed 263, ceiling 263, deleted 263, failed 0,
                  backed_up 263, considered 367,
                  refused {has_reply 37, sequence_not_finished:1 5,
                           sequence_not_finished:-1 62}
stored_before 22763   free_before 2237   deficit 263
fresh_created 0   total_created 0   airtable_fresh 0   apollo_credits 0.0
stop_reason target_not_reached:instantly_slots_short_by:263
```

- **The occupancy fix is live and correct**: it read `stored 22763`, which is the
  21,069 in campaigns PLUS the 1,694 on lists. The old code read 22,757.
- **The rotation fix is live and correct**: 263 deleted, **0 failed**. Before the
  fix the identical code path reported `deleted: 0, failed: 3` and aborted at the
  breaker. It also **stopped exactly at its ceiling of 263**, which is the
  "stop when it reaches the needed capacity" behaviour.
- **It spent nothing and created nothing**: 0 Apollo credits, 0 leads, 0 Airtable
  rows. The copy guard was never reached because acquisition stopped first.

**This was not planned and I did not authorise 263 deletions.** It is the
system's own designed rotation, every contact backed up first and every gate
applied, but it is a larger action than the "small safe batch" that was asked
for, and it happened because a deploy starts a cron service.

Consequence: **I did not run a separate manual rotation batch.** Deleting five
more contacts by hand would add risk and prove less than the 263 already do.
Verified: `batch_id = 'rot-manual-20261002-verify'` has **0 rows** — nothing of
mine was backed up or deleted.

### The 263 verified by ID, not by the counter

The counter did NOT move: occupancy still reads `stored 22763`, `free 2237`,
30+ minutes later. That proves nothing by itself, so every sampled contact was
read back individually:

```
10 of 10 sampled -> HTTP 404 GONE;  STILL PRESENT: 0
```

So the deletions are real and `/campaigns/analytics` `leads_count` **lags**.
Occupancy therefore understates free slots for a while after a rotation, which is
conservative — the run stopped rather than over-committing. Once the count
catches up: 22,763 − 263 = **22,500 stored → 2,500 free → deficit 0**, so the
next run should clear the capacity gate.

Recorded in the backup table correctly for the first time: `delete_status = 200`
and `deleted_at` set on all 263.

## Step E3 — the 12 earlier mis-recorded deletions, reconciled

All 12 rows carried `delete_status = 0` (the `status_code`/`status` misread), 3
per day across 09-29 → 10-02. Each was read back by ID:

```
GONE (delete really succeeded): 12
STILL PRESENT (really failed) :  0
```

So all twelve deletes had succeeded and been recorded as failures. The rows were
updated to `deleted_at = now()` with the reason noted, **only for the twelve ids
verified absent**, and **no delete was repeated**. `instantly_rotation_backup`
now holds 275 rows with **0 unreconciled**.

### A safety gap this exposed, and closed

Four of those twelve came from `f0665173…`, the **OOO follow-up campaign** — which
was NOT in `protected_ids()`. It holds people who asked us to come back later, so
deleting them discards a deferral we promised. `protected_ids()` now reads
`TGTC_OOO_FOLLOWUP_CAMPAIGN_ID` and includes it, `judge()` takes the resolved set,
and two tests pin it. **Not yet deployed** — it is in the follow-up PR.

## Step E4 — occupancy contrasted with the real workspace, and no double counting

| | |
| --- | --- |
| in campaigns | 21,069 |
| on lead lists | 1,694 (1,688 incident hold + 6 on the older `TGTC Outbound Hold v1`) |
| **stored** | **22,763** |
| free (25,000 cap) | 2,237 |

**Double counting checked, not assumed:** every one of the 1,694 list contacts
has an EMPTY `campaign`, and the intersection between the list population and the
campaign population is **0**. So adding the two cannot double count.

Note the pre-existing undercount: the 6 contacts on the older hold list were
already invisible to the controller before any of this work, so the 10-02 run's
`stored_before: 22757` was itself 6 short.

**On the cap:** 25,000 is `DEFAULT_PLAN_CONTACTS`, a configured constant.
`/workspaces/current` returns `plan_id: pid_hg_v1` and **no usage or limit
figure**, so the ceiling is not independently readable from the API. It is
corroborated only by the plan on record and by the provider's historical
"Lead limit reached" refusal. Stated as an assumption, not a measurement.

## Step E5 — the pre-enrichment copy check: implemented and tested, NOT wired in

`approval.challenger_copy_refusal_from_posting` answers the copy question from
posting facts alone. It does not re-implement anything: it calls
`instantly_payload_with_copy_state` — the same renderer, the same QA gates, the
same reason strings — with a placeholder contact, because the gates read
`open_role` and `role_focus` and never the contact's name.

`tests_core/test_copy_refusal_before_paid_enrichment.py`, 11 tests: the probe
returns the final guard's **exact** reason; a renderable posting is not
pre-refused; Control is never pre-refused; the verdict is identical across three
different contacts; it agrees with the final guard across a sweep of 13 role
shapes; **the final guard still raises and still blocks**; and the placeholder
contact never appears in a payload.

**The projection is WITHDRAWN.** I previously said this would save ~2,634 Apollo
credits per run-equivalent. That was arithmetic over historical approvals, not a
measured per-run saving, and the last five runs spent 0 credits anyway. There is
no per-run saving figure until a run actually executes with the check wired in.
The predicate is in place; **wiring it into the acquisition path is deliberately
NOT done** — it closes opportunities before enrichment and changes the funnel and
its counts.

## Step E6 — repairs and holds

- The 363 concrete-title repairs are in flight: **275 applied, all HTTP 200, 0
  failures** at the time of writing. A read-back sweep follows completion.
- The **287** leads with no usable concrete title are **not yet moved** to the
  hold list. They still sit in the (paused) campaigns carrying generic subjects.
  Outstanding.

## Outstanding

1. Finish the 363 repairs and read them all back.
2. Move the 287 generic-subject leads with no usable title to the hold list.
3. Deploy the follow-up PR (OOO follow-up protection + the pre-enrichment
   predicate). Needs an owner merge.
4. Verify both Challenger approval routes against the DEPLOYED commit.
5. Decide the blank-thread cohort (5,630) before any campaign is reactivated.
6. Decide capacity: even with rotation working, clearing a 263-slot deficit
   consumed essentially the whole safe population. The remaining levers are the
   storage add-on, the 1,500-slot reserve, the 1,000 target, or releasing the
   1,694 held contacts — all decisions, none a defect.

**The Core cron stays paused** (`cronSchedule: None`). Note for restoring it: a
deploy alone will start a run, so the restore order matters.

## Step E7 — both Challenger routes verified AT the deployed commit

Not "the tests pass on my branch": a detached worktree was created at
`b6bbc65372b24d5d66c130b3c428aa48bf964a8a` — the exact commit Railway is running —
and the route tests were executed there:

```
tests_core/test_challenger_copy_blocks_not_crashes.py
tests_core/test_delivery_outbox.py
37 passed
```

That covers the candidate route, the reused-verified-person route, the exact
`blocked_reason` on both outbox rows, that no empty lead is created and nothing
reaches Airtable, and transaction atomicity. The worktree was then removed.

## Step E8 — follow-up PR open

PR **[#132](https://github.com/TGTChq/GTM/pull/132)**: the OOO follow-up campaign
protection and the pre-enrichment copy probe. Full suite **2,123 passed, 0 failed**.
I cannot merge it myself — the classifier refuses `gh pr merge` and even binding
the PR.

**Warning recorded on the PR:** merging it will START A RUN, because a deploy
starts a cron service regardless of `cronSchedule` being `None`. That is what
produced run `…7831317d`. It is not dangerous as things stand — campaigns are
paused so nothing sends, and the copy guard blocks anything unrenderable — but it
should be known before merging, not after.

## Step E9 — the 363 re-renders and the 287 holds, both verified

**363 concrete-title repairs: 363/363 applied, HTTP 200 on every one, 0 failures.**
Their subject is now the real job title from the approved `posting_title` instead
of a bare function noun — 211 via the approved `role-display/2` reducer, 152 from
the title directly.

**287 moved to the hold list: 287/287, every background job `success`, 0 failed.**
These are the leads whose only possible subject was a generic noun and whose
posting title cannot pass the display gates. Nothing deleted; a moved lead has no
campaign and cannot send.

The two sets were checked to be disjoint before either ran: **0 overlap** between
the 363 and the 287, and **0 overlap** with the 1,688 already held.

### Final read-back sweep, expectations updated for the re-renders

Every lead still in the nine campaigns re-read from the provider and compared
against the original repair plan OVERRIDDEN by the 363 new renders:

| Verdict | Leads |
| --- | --- |
| **VERIFIED** | **5,366** |
| HAS_COPY_NOT_IN_PLAN (legacy healthy cohort) | 591 |
| NO_COPY_TERMINAL_OK | 463 |
| **SENDABLE_WITHOUT_COPY** | **0** |
| COPY_MISMATCH | **0** |
| DRIFTED | **0** |
| PARKED_BUT_STILL_IN_CAMPAIGN | **0** |
| **bare function-noun subject on a sendable in-campaign lead** | **0** |

`5,366 + 591 + 463 = 6,420` in campaigns, plus **1,975** parked (1,688 + 287)
= **8,395** — the exact figure backed up before any write. Every campaign reads
**SAFE TO RESUME: YES** on the copy gate alone.

## Where this leaves things

**Deployed and verified:** commit `b6bbc65372`, deployment `40b808aa` SUCCESS.
Both Challenger approval routes verified AT that commit (37 tests in a detached
worktree). Occupancy counts stored contacts, not campaign membership. Rotation
scores deletes correctly, proven by the 263 it then removed with 0 failures, each
sampled one confirmed absent by ID.

**Paused and staying paused:** the nine campaigns (external pause of 22:10Z not
overridden) and the Core cron (`cronSchedule: None`).

**Open, and each one a decision rather than a defect:**

1. **PR [#132](https://github.com/TGTChq/GTM/pull/132)** — OOO follow-up
   protection and the pre-enrichment copy probe. Needs an owner merge; merging
   starts a run.
2. **The blank-thread cohort, 5,630 recipients.** Their body is repaired; their
   thread subject cannot be. Whether to keep emailing them is a judgement about
   the recipient relationship. No sequence restarted, nothing resent.
3. **1,975 parked leads.** 1,688 with no renderable copy, 287 whose only subject
   would be generic. Either the title data is cleaned and they are re-rendered, or
   they are written off. Re-enrolling them also needs proof that a move back does
   not reset sequence position — untested, so not attempted.
4. **Capacity.** Rotation cleared its 263-slot deficit but consumed essentially
   the whole safe population to do it: of the contacts remaining in rotatable
   campaigns afterwards, `judge()` accepts only a minority (153 bounced, 146
   replied, 120 eligible when measured post-rotation). The levers are the storage
   add-on, the 1,500-slot reserve, the 1,000 target, or releasing parked
   contacts. No budget was raised and no run was opened.
5. **Restoring the cron** — note the order: a deploy alone starts a run, so merge
   #132 first, let that run settle, then restore `0 3 * * *`.

Nothing here claims the 16,875 already-sent blank emails are recovered. They are
not.

---

# Continuation 4 — closing the deploy-start hole properly

## Step F1 — the hour window was necessary but NOT sufficient

Correctly pointed out: a redeploy between 03:00 and 05:59Z is INSIDE the window,
so the window alone still permits a second run. The run lock does not help either
— it stops an OVERLAP, not a second execution that starts after the first
released it. Both of 2026-10-02's unplanned runs were sequential, not concurrent.

## Step F2 — a durable, atomic per-day execution claim

`tgtc_core/services/scheduled_execution.py` + migration
`021_scheduled_executions.sql`.

The day's execution is claimed with
`INSERT … ON CONFLICT (execution_day) DO NOTHING`, so the PRIMARY KEY arbitrates
rather than timing, and the claim OUTLIVES the process so a later start sees it.

Three states, told apart **before anything is spent**:

| State | Meaning | Outcome |
| --- | --- | --- |
| `claimed` | first start of the day | proceeds |
| `already_completed` | the day reached `daily/end` | **refused** as a duplicate |
| `interrupted_needs_authorisation` | a claim with no `finished_at` — the container died mid-run | **refused** unless a recovery is named |
| `recovery_token_mismatch` | the token names a different day or run | **refused** |
| `recovering` | explicitly authorised recovery | proceeds, `attempt+1`, `recovery_of` recorded |

**A recovery is explicit and auditable:** `TGTC_RUN_RECOVER=<day>:<interrupted_run_id>`
must name exactly the interrupted run. It cannot sit in production as a blanket
permission — once that execution finishes the day reads `already_completed`, so the
same token authorises nothing (tested).

**`TGTC_RUN_FORCE=1` opens only the hour window, never the duplicate guard.**
Also tested, because a force flag that also bypassed the day claim would re-open
the hole.

**The recovery uses the right day's remaining budget:** `day_of()` uses the same
basis as `budget_policy.budget_id_for("scheduled")` (plain UTC date), and a test
pins that correspondence, so a recovery claims `prod-scheduled-<that day>` and
spends what is left of it rather than opening a second allowance.

Placement: after the run lock, **before** spend acknowledgement and before the
budget claim. The day is closed only at `daily/end`, so a process that dies leaves
it `interrupted`, never `completed`.

### Tested scenarios (32 tests across two files)

- a **redeploy inside the window** after the day completed → refused
- **two simultaneous starts** → exactly one claim, the other refused
- a **start after `daily/end`** → refused as a duplicate
- an **authorised recovery after a failure** → proceeds, audited, `attempt=2`
- an interrupted run is **not** mistaken for a completed one, nor for a free day
- a stale recovery token → inert
- only the holder may close the day, and only once
- both real 2026-10-02 deploy-started runs (23:09:28Z, 23:33:22Z) → would be refused
- the 03:00Z tick → still allowed

## Step F3 — the effective start command, verified against the guard

Read live from the service instance:

```
cronSchedule: None
KIND=${TGTC_RUN_KIND:-}; if [ -z "$KIND" ]; then H=$(date -u +%H);
  if [ "$H" -ge 3 ] && [ "$H" -le 5 ]; then KIND=scheduled; else KIND=manual; fi; fi
```

So the start command's own inference is hours **3, 4, 5** → `scheduled`, and the
guard's default window is **`3-5`**. They agree, and a test asserts the agreement
for all 24 hours rather than by eye.

**One nuance that matters:** `TGTC_RUN_KIND` is SET in the environment, so the
`if [ -z "$KIND" ]` branch never executes — the start command always declares
`scheduled` whatever the hour. The hour basis it was written for was therefore
dormant. The guard restores that basis as an ENFORCED gate rather than a label,
which is why the labelling and the gating now have to be read as two separate
things.

## Step F4 — the three executions against one budget

All three share `prod-scheduled-20261002` (`budget_claims.runs = 3`), ceilings
Apollo 1,600 credits / 10,000 requests, Fantastic 4,000 / 60, Anthropic 1,500
requests, expiring 2026-10-03T03:00:38Z.

| Run | origin | approvals | confirmed Instantly creations | apollo credits |
| --- | --- | --- | --- | --- |
| `…708d7ea4` 03:00Z | cron tick | 0 | 0 | 0 |
| `…7831317d` 23:09Z | **deployment start** | 0 | 0 | 0 |
| `…b7478fc9` 23:33Z | **deployment start** | 28 (in flight) | **0 so far** | — |
| shared budget total | | | | **39 of 1,600** (4,262 calls) |

Approvals and confirmed creations are reported separately and deliberately: 28
approvals is not 28 leads. Confirmed creations come from
`delivery_receipts.receipt_kind = 'created'`, and that is still 0.

### The copy guard firing in production, without ending the run

```
challenger_copy_qa_failed:role_display_contains_unsafe_characters     2
challenger_copy_qa_failed:role_display_longer_than_48_chars           2
challenger_copy_qa_failed:role_display_carries_an_appended_qualifier  1
```

Five refusals recorded with precise reasons, five compliance blocks beside them,
and the run continued. On the pre-fix code the first of those would have raised
inside `_commit_approval` and rolled the approval back, ending the run. This is
the approval-exception fix verified in production rather than in a test.

## Still pending, in order

1. The run closes (not killed, not redeployed over).
2. Reconcile: confirmed creations, Airtable, copy refusals, spend, remaining
   capacity; read back the new leads' five copy fields.
3. Deploy this protection **with the lock free**.
4. Verify the deployment and the duplicate prevention live — a deploy outside
   03-05Z must decline, and a second start must be refused by the day claim.
5. Only then restore `cronSchedule = 0 3 * * *`, recording which day each
   execution belongs to.

Campaigns stay paused. The 1,975 held leads are untouched. Capacity is still NOT
sustainable: rotation freed exactly this run's 263 slots.

## Step F5 — the run closed: reconciliation

Run `20261002T233322.605777Z-b7478fc9`, origin **deployment start**, kind
`scheduled`, budget `prod-scheduled-20261002` (shared, `runs=3`). Lock now free.

| Measure | Value |
| --- | --- |
| stop_reason | `target_not_reached:apollo_request_allowance_insufficient` |
| rounds | 4 |
| **confirmed Instantly creations** (`receipt_kind='created'`) | **0** |
| `fresh_instantly_created` | 0 |
| Airtable rows written | **0** |
| approvals written | **42** |
| Apollo credits | **56** of 1,600 |
| Apollo requests | **7,407** of 10,000 |
| Fantastic requests | **0** — it bought no new inventory |
| capacity at start | stored 22,500, free 2,500, deficit 0 (`enough_room`) |

Outbox rows this run wrote, all 42 accounted for:

| State | Reason | Rows |
| --- | --- | --- |
| pending | — | **25** |
| blocked | `compliance:unknown_jurisdiction:absent` | 6 |
| blocked | `challenger_copy_qa_failed:role_display_contains_unsafe_characters` | 4 |
| blocked | `challenger_copy_qa_failed:role_display_carries_an_appended_qualifier` | 3 |
| blocked | `challenger_copy_qa_failed:role_display_longer_than_48_chars` | 3 |
| blocked | `compliance:uk:not_a_verified_corporate_subscriber` | 1 |

**10 copy refusals, and the run did not die.** On the pre-fix code the first one
would have raised inside `_commit_approval`, rolled the approval back and ended
the run. This is the approval-exception fix confirmed in production.

### The five copy fields, verified — with an honest limit

There are **no new Instantly leads to read back**, because the run created none
(0 confirmed creations, delivery never drained before the allowance stopped it).
So the read-back was done on what does exist, the 25 stored pending payloads:

```
pending_rows 25 | has_subject 25 | has_b1 25 | has_b2 25 | has_b3 25 | has_b4 25
unresolved tokens 0
```

Samples: subject `Microsoft Cloud Engineer` (body1 338 / body4 242),
`Experience Server` (384 / 236), `Customer Experience Manager` (353 / 244) —
real job titles, not function nouns. Those 25 are queued for the next run's
delivery drain; they are approvals, **not** leads, and are counted as such.

### Why it produced nothing, measured against history

| Budget day | Apollo calls / limit | credits | creations | stop |
| --- | --- | --- | --- | --- |
| `…20260928` | 9,508 / 10,000 | 762 | 517 | apollo request allowance |
| `…20260929` | 0 | 0 | 0 | instantly slots short |
| `…20260930` | 0 | 0 | 0 | instantly slots short |
| `…20261001` | 0 | 0 | 0 | instantly slots short |
| `…20261002` | 7,407 / 10,000 | 56 | **0** | apollo request allowance |

Two things follow, and neither is about the copy fix:

1. **The Apollo REQUEST allowance, not credits, is the binding constraint
   whenever capacity allows.** 09-26, 09-27, 09-28 and now 10-02 all stopped on
   it, while credits stayed far below their ceiling (56 of 1,600 this time).
2. **This run bought no new inventory** (`fantastic_requests: 0`) and ground the
   existing `qualify_opportunity` backlog instead, at roughly **176 Apollo
   requests per approval** against about **18 per creation** on 09-28. That ratio
   is what working a four-day-old backlog costs, not what a healthy run costs.

So the throughput picture is NOT "the copy guard reduced output". Output was 0
because the request allowance ran out while reworking stale inventory. I am not
projecting a per-run figure from this.

### Capacity after the run

Still **not** sustainable, and I am not calling it so. Rotation freed exactly
this run's 263 slots; the run then consumed none of them (0 creations), so free
capacity is unchanged at about 2,500 — enough for one run, with the safe
rotation population largely spent.

## Step F6 — ready to deploy the duplicate protection

Lock is free (`held=0`), service `Completed`, cron still `None`. PR
**[#134](https://github.com/TGTChq/GTM/pull/134)** holds the per-day execution
claim; full suite **2,155 passed**, integrity 35/35.

## Step F7 — the protection DEPLOYED and verified live

PR #134 merged. Deploy tip `8242de2`, deployment **`a5ce3b40` SUCCESS**, effective
commit **`8242de24ad`**, schema migration version now **21**.

### The deploy declined to start a run — in production, with the log to prove it

```
Starting Container
run kind=scheduled budget=prod-scheduled-20261003
run-daily declined: outside_scheduled_window:00Z_not_in_03-05
  (a deploy must not start a run; set TGTC_RUN_FORCE=1 for a deliberate manual run)
```

Corroborated three ways rather than taken from the log alone:

| Check | Result |
| --- | --- |
| `daily/start` events in the following 70 minutes | **none** |
| rows in `scheduled_executions` | **0** — the day was never claimed |
| run lock | `held=0` |

The two previous deploys each started a full run; this one did not. The budget row
`prod-scheduled-20261003` was defined (ceilings only, `used: {}`, expires
2026-10-04T00:54:46Z) — that is the `budget` CLI step, not a claim and not spend.

### Duplicate prevention verified against the PRODUCTION table

Run on a synthetic day `19700101`, which can never collide with a real execution
day, inside one transaction and removed afterwards:

| Step | Result |
| --- | --- |
| first start claims the day | `probe-a` |
| second SIMULTANEOUS start | `INSERT 0 0` — nothing |
| state while running | unfinished, `attempt=1` |
| holder closes the day | `UPDATE 1` |
| a start AFTER `daily/end` | nothing |
| a NON-holder tries to close | `UPDATE 0` |
| cleanup | row removed, **0 rows left**, 0 probe rows |

So the table's atomicity and the holder rule hold in production, not only in the
32 unit tests.

## Step F8 — CRON RESTORED

| | Before | After |
| --- | --- | --- |
| `cronSchedule` | `None` | **`0 3 * * *`** |
| `startCommand` | 713 chars | 713 chars, unchanged |
| `dockerfilePath` / `rootDirectory` / `numReplicas` / `restartPolicyType` | | all unchanged |

`railway status`: `GTM Core Canary 1000: ● Completed · 0 3 * * * · next run in 2 hours`.
Run lock was `held=0` at the moment of restore.

### Which day each execution belongs to

| Execution day | Budget | Executions | Origin |
| --- | --- | --- | --- |
| **20261002** | `prod-scheduled-20261002` (`runs=3`) | `…708d7ea4` 03:00:39Z, `…7831317d` 23:09:28Z, `…b7478fc9` 23:33:22Z | cron tick, **deployment**, **deployment** |
| **20261003** | `prod-scheduled-20261003` (defined 00:54Z, **unclaimed**) | none yet; the 03:00Z tick will be the day's ONE authorised execution | cron tick |

From now on the day's execution is claimed durably, so a second start on 20261003
— whether from the cron firing twice or from a deploy inside 03:00-05:59Z — is
refused before anything is spent.

## Close-out

### Resolved, with receipts

| Item | Evidence |
| --- | --- |
| Cause | campaign bodies hold no literal copy; 0 of 9,053 stored payloads carried `rendered_subject` |
| Scope | 16,875 messages to 7,777 recipients, onset 2026-09-21T13:15:59Z, from 24,300 classified sends; two independent indicators agreeing on 100% |
| Fix deployed | `8242de24ad`; copy refused at payload build, in the outbox and in the provider client |
| Guard proven in production | 10 copy refusals recorded with precise reasons and the run continued |
| Records repaired | 5,653 read-back verified + 363 re-rendered with concrete titles; 0 mismatched, 0 drifted |
| Parked | 1,975 out of every campaign, nothing deleted |
| Final sweep | 6,420 in-campaign leads, **0 sendable without copy**, 0 bare-noun subjects |
| Occupancy | counts campaigns AND lists (22,763 → 22,500 after rotation), no double counting |
| Rotation | 263 deleted, 0 failed, each sampled one confirmed absent by ID; 12 historical mis-records reconciled |
| OOO follow-up campaign | now protected from rotation |
| Deploy cannot start a run | declined live at 00:54Z |
| One execution per day | verified on the production table |
| Cron | restored to `0 3 * * *` |

### Pending DECISIONS — not defects, and not mine

1. **The blank-thread cohort, 5,630 recipients.** Body repaired; the thread
   subject cannot be. Nothing resent, no sequence restarted. **All nine campaigns
   remain PAUSED** until this is decided.
2. **1,975 parked leads.** Clean the title data and re-render, or write off.
   Re-enrolment also needs proof that a move back preserves sequence position,
   which is untested.
3. **Capacity is NOT sustainable.** Rotation freed exactly one run's 263 slots and
   the safe population is largely spent. Levers: the storage add-on, the
   1,500-slot reserve, the 1,000 target, or releasing parked contacts.
4. **The Apollo REQUEST allowance is the real throughput ceiling** whenever
   capacity allows — 09-26, 09-27, 09-28 and 10-02 all stopped on it while credits
   stayed far below ceiling. No per-run figure is projected from the 10-02 run,
   which bought no inventory and ground a four-day-old backlog.
5. **The pre-enrichment copy check is built and tested but NOT wired in.** Wiring
   it changes the funnel and its counts.

### Not claimed

The 16,875 already-sent blank emails are **not** recovered. The parked leads are
**not** fixed. Capacity is **not** sustainable. No budget was raised and no run was
opened to recover the four lost days.

---

# Continuation 5 — reconciliation, pre-enrichment gate, Apollo diagnosis, reactivation prep

## Step G1 — final reconciliation of run `…b7478fc9`

**Closure state: ENDED** 2026-10-03T00:17:38Z. Lock free, service `Completed`.

| Measure | Value |
| --- | --- |
| stop_reason | `target_not_reached:apollo_request_allowance_insufficient` |
| approvals | **42** |
| **genuine Instantly creations** (`receipt_kind`) | **0** — every one of the 42 has `(no receipt)` |
| Airtable rows written | **0** |
| instantly outbox | 25 `pending`, 17 `blocked` (10 copy + 7 compliance) |
| airtable outbox | **identical**: 25 `pending`, the SAME 17 blocked |
| Apollo credits | 56 / 1,600 |
| Apollo requests | 7,407 / 10,000 |
| Fantastic requests | 0 |
| capacity remaining | stored 22,500, free **2,500** |

The Airtable outbox carrying the identical 10 copy-blocked rows is the invariant
holding in production: **a copy refusal produces no Instantly lead AND no CRM row.**

## Step G2 — the copy check now runs BEFORE paid enrichment

Wired into `OpportunityService.process()` at the existing
"refuse to spend Apollo credits when the output route cannot produce a lead" gate —
before `_ensure_employer_facts`, before any contact is bought.

It restates no rule. `challenger_copy_refusal_before_enrichment` builds the lead
the real flow would build, through `build_approved_lead` with a PROBE contact that
satisfies the person gates, and then asks
`instantly_payload_with_copy_state` exactly what approval asks it. It returns ""
unless the refusal is specifically about copy, so a refusal for any other reason is
left to the real flow — it can only ever decline to BUY.

**The final guard is untouched and still raises.**

`tests_core/test_copy_refused_before_apollo_is_spent.py`, 10 tests:

- an unrenderable posting is **closed with 0 paid Apollo calls**, no approval row,
  no outbox row
- nothing reaches Instantly or Airtable for it (`instantly.leads == {}`,
  `airtable.records == {}`)
- a renderable posting **still proceeds and still pays** — the gate declines to
  buy, it does not stop the pipeline
- a Control destination is never pre-refused
- pre-check and final guard return the **identical reason** across five role shapes
- the final guard still raises when reached directly

One behaviour this changed: an unrenderable posting is now CLOSED before
enrichment instead of approved-with-blocked-outbox. The approval-time guard
becomes the backstop. `test_challenger_copy_blocks_not_crashes.py` therefore
disables the earlier gate explicitly, so each file exercises one gate rather than
the earlier one standing in for the later.

## Step G3 — the Apollo request limit, diagnosed and the controllable part fixed

Requests are counted as `count(*)` over all reservations **regardless of status**
(`budget_status`), so a refused call consumes allowance exactly like a served one.

| Operation / status | 10-02 (0 creations) | 09-28 (517 creations) |
| --- | --- | --- |
| `people_search` served | **6,000** (0 credits) | 8,714 |
| `people_search` **refused** | **1,351** | 32 |
| `person_match` served | 55 (55 credits) | 742 (742 credits) |
| `organization_enrich` | 1 | 16 served + 4 failed |

Three findings:

1. **`people_search` consumes 99.3% of the request allowance and costs no
   credits.** The ceiling that binds is requests, not credits — 56 of 1,600
   credits were used.
2. **Search-to-match conversion collapsed**: 0.9% (55/6,000) against 8.5%
   (742/8,714) on 09-28. The run bought no inventory (`fantastic_requests: 0`) and
   searched a four-day-old backlog where the contacts are not findable. That is
   the "exhausted backlog" effect, and it is a data condition, not a bug.
3. **1,351 refused searches were avoidable, and that IS a bug.** `refused` maps
   from `CREDIT_EXHAUSTED`, `UNAUTHORIZED` and `RATE_LIMITED`; no credits were
   spent, so they were rate limits. `_handle_global` raised `ProviderWait` for the
   opportunity in hand on RATE_LIMITED but **recorded nothing** — unlike credit
   exhaustion and 401, which both call `provider_state.record_refusal`. So the next
   opportunity's `_guard_provider` saw `serving`, searched, and hit the same limit
   again, 1,351 times, burning **18% of the day's allowance** for nothing.
   `Outcome.RATE_LIMITED.global_stop` already existed and **was read nowhere**.

**Fix:** a per-run latch, bounded by the provider's own retry-after (capped at
900s). Once Apollo says it is throttling, no further Apollo call is issued until
that instant passes — free searches included, since the throttle is on the
endpoint. Deliberately NOT `record_refusal`: that would turn a 60-second throttle
into a six-hour outage, and an existing test asserts "a throttle is not a
refusal", which still passes.

7 tests, including that the second opportunity does not rediscover the limit, that
the latch releases exactly when the provider said, that a day-long retry-after is
capped, and that credit exhaustion and 401 still persist in `provider_state`
because those outlive one run.

No budget raised, no validation skipped, no cache invented where the evidence did
not support one: the repeated-search waste here was the rate-limit storm, not
duplicate queries.

## Step G4 — capacity for the next tick, and rotation headroom

| | |
| --- | --- |
| stored | **22,500** (20,519 in campaigns + 1,981 on lists) |
| free | **2,500** |
| `room_needed` (target 1,000 + reserve 1,500) | **0** |
| safe rotation candidates remaining | **11** (134 bounced, 124 replied) |

So the tick does **not** need to rotate and will **not** be rejected for capacity.
But the rotation safety net is effectively spent: 11 candidates against a future
deficit that will be ~1,000 once this tick consumes its slots. **Capacity is good
for ONE tick and then hits a wall.** Nothing was deleted, no reserve reduced, no
capacity bought.

## Step G5 — selective reactivation: the healthy group is ELEVEN

Classified every one of the **6,376** leads now in the nine campaigns. Healthy
requires all of: five copy fields present and resolved, a CONCRETE subject (a bare
function noun is not one), mutable status, no reply, not suppressed, **and never
contacted** — so the first email it ever receives carries a real subject.

| Bucket | Leads |
| --- | --- |
| **HEALTHY** | **11** |
| BLANK_THREAD, already contacted | **5,352** |
| INVALID_COPY | 419 |
| EXCLUDED terminal (completed 551 / bounced 22 / unsubscribed 1) | 574 |
| EXCLUDED replied | 20 |

Per campaign the healthy are: OPERATIONS 5, CUSTOMER_EXPERIENCE 2, FINANCE 1,
PEOPLE_HR 1, GTM_SYSTEMS 1, AI_TECHNICAL 1; PRODUCT, ECOMMERCE and
MARKETING_CREATIVE have none. Sample subjects: `Home Equity Closer`,
`IT Specialist II`, `Sample Coordinator` — concrete titles.

Consistency check on the 419 INVALID_COPY, because it looked like it contradicted
the earlier "0 sendable without copy": **all 419 are terminal** (337 bounced, 82
completed) and **0 have a mutable status**. No contradiction.

## Step G6 — moving a lead out and back DESTROYS its sequence position

Tested on the internal contact, never on a recipient, in a paused campaign.
Subject: the TEST A lead (`luis@globaltalent.co`) which had executed step 1.

| Field | Before | After move OUT | After move BACK |
| --- | --- | --- | --- |
| `status_summary` | `lastStep stepID 0_0_0` | **`{}`** | **`{}`** |
| `status` | 3 completed | 3 | **1 active** |
| `campaign` | TEST A | **null** | TEST A |
| `timestamp_last_contact` | 19:03:36Z | kept | **null** |
| **sequence position** | `0_0_0` | — | **None** |

**The history is wiped on the way OUT, and the lead returns as a fresh ACTIVE lead
that would receive step 1 again.** Three consequences:

1. The **1,975 parked leads cannot be returned** to their campaigns without
   restarting their sequences — every one would be emailed from step 1 again.
   "Recovering" them means re-contacting them from scratch, which is a business
   decision, not a technical repair.
2. Excluding the blank-thread cohort by moving them out is a **one-way door**: safe
   as an exclusion (nothing deleted, nothing can send) but not reversible.
3. Therefore selective reactivation **cannot** be done by emptying the existing
   campaigns of the affected cohort.

### The configuration that DOES work, prepared and not executed

Put the **11 healthy, never-contacted** leads into a NEW campaign carrying the
same approved 4-step sequence. They have never been emailed, so starting at step 1
is correct for them and there is no history to lose. The nine existing campaigns
stay PAUSED and untouched, so the 5,352 blank-thread leads keep their position
while that decision is pending.

Prepared: `healthy_contacts.jsonl` (11 ids with campaign, email, status, subject),
`blank_thread_in_campaign.jsonl` (5,352), `invalid_copy_in_campaign.jsonl` (419),
`excluded_other.jsonl` (594). **FINANCE keeps its prior pause regardless.**

Not executed: no campaign created, no lead moved, nothing activated.

## Step G7 — the 1,975 parked leads against the ORIGINAL sources

role-display/2 recovered only 2 of them, so this asked a different question: does
the original posting title still CONTAIN a usable title? Most were refused for the
SHAPE of the whole string, not for the absence of a title:

| original source | refused because | title sitting inside it |
| --- | --- | --- |
| `Senior Product Manager, Ad Monetization` | unsafe character (the comma) | Senior Product Manager |
| `Customer Success Manager - EMEA` | appended qualifier | Customer Success Manager |
| `Enterprise Solution Architect - Manufacturing Planning` | longer than 48 chars | Enterprise Solution Architect |

Guardrails, so nothing is fabricated:

* every candidate is a **literal, contiguous substring** of the approved
  `posting_title` or `open_role`, obtained by splitting on separators only. No word
  is added, reordered or inflected — asserted in code, and re-checked afterwards:
  **0 of 1,309 displays fail to be a literal substring of their source.**
* a candidate is accepted only if it ENDS in a role noun **measured** from the
  6,027 displays that already pass every gate (159 terminal nouns at >=3 uses), so
  `OLS` and `128501` cannot become a subject. The vocabulary is derived from the
  population, not written by hand.
* a bare function noun is never a candidate (0 of 1,309 end in a generic word).
* every candidate then goes through the real renderer and every QA and content
  gate. Nothing relaxed.

**Result: 1,309 of 1,686 (77.6%) have a concrete recoverable title.** 650 distinct
displays; 0 single-word; 0 unresolved tokens; subject == display in all 1,309.

| parked for | recovered | not recovered |
| --- | --- | --- |
| unsafe characters | 642 | 230 |
| appended qualifier | 485 | 53 |
| longer than 48 chars | 163 | 77 |
| rd2 ambiguous hold | 19 | 0 |
| buzzword content gate | 0 | 14 |
| reads as a posting headline | 0 | 3 |

The 377 that stay parked: 306 have no segment that reads as a title, 66 have no
candidate segment at all, 5 fail a content gate afterwards. Those are **legitimate**
refusals — there is no title in the source to recover.

Honesty about quality: 1,120 of the 1,309 end in a noun that terminates **10 or
more** already-live passing displays. The other **189** are rarer tails
(`management` 29 vs 4 live, `services` 21 vs 8, `development` 18 vs 8) and read more
like a department than a person's title — `Promotional Review Operations`,
`Learning Experience Design and Innovation`. Real text from the employer's own
posting, but weaker. They are flagged, not silently mixed in.

## Step G8 — the finding that decides it: the parked leads were ALREADY emailed

The pre-hold snapshot only carried campaign/email/lead_id/status, so it could not
answer this; reading the hold list fresh could.

| | |
| --- | --- |
| leads on the hold list | **1,975** |
| `timestamp_last_contact` set | **1,962** |
| never contacted | **13** |
| sequence position surviving | **0** (the move out wiped `status_summary`, exactly as the internal test showed) |

Cross-checked independently against the classified send record: **1,961 of the
1,975 received at least one `BROKEN_NO_COPY` message**, and **14 have no sent
message on record**. The two methods agree within one lead (a lead contacted at a
timestamp with no matching message row).

So the title recovery is real but it does **not** make them resumable:

* they have already received a blank-subject email, and
* returning them restarts the sequence at step 1 (proved, Step G6).

Of the 1,309 with a recovered title, **1,300 already received a broken email** and
only **9** did not.

### The 9 clean ones repaired, read-back verified

| email | recovered display |
| --- | --- |
| janet.villalobos@sharp.com | Compensation Analyst |
| juresse.mbambi@wpromote.com | Senior Software Engineer I |
| robyn.wolf@cpc.com | Scientist II |
| mags_tierney@harvard.edu | Senior Coordinator |
| tivy@ccsfundraising.com | Faith-Based Fundraising Consultant |
| kat.morris@wgu.edu | AI and ML Engineering |
| amy.meagher@cgoncology.com | Promotional Review Operations |
| calvin.lai@cgoncology.com | Promotional Review Operations |
| elizabeth.egel@cgoncology.com | Promotional Review Operations |

9 PATCHed, **9/9 verified identical on read-back**, 0 field mismatch, 0 drift
(still on the hold list, no campaign, status active, 0 replies), 0 unresolved
tokens. The last four are the weaker department-like tail and are marked as such.

Nothing was deleted, no reserve reduced, no capacity bought. The remaining
**1,300** stay parked with a prepared, verified repair set
(`recovered_from_sources.jsonl`) that is **not applied**, because applying it
changes nothing until there is a decision about re-contacting people who already
received a blank email.

## Step G9 — the two red CI checks: both pre-existing on main, both root-caused

PR #135 opened red on `core` and `test`. **The same two failures are present on
main at 687dd20e (2026-10-02 23:32Z, before this commit)** — main's first CI run
since 2026-09-09 — so neither came from this change. Both were diagnosed to the
mechanism and fixed rather than waved through.

### `core` — `test_shared_copy_renderer_has_no_legacy_or_network_dependencies`

`AssertionError: {'requests'}`. It did not reproduce locally
(`leaked: none`), and the log carried the clue:
`PytestAssertRewriteWarning: ... ci_no_network`.

`rebuild/run_offline_tests.py` writes a `sitecustomize.py` into a temp directory and
puts it on `PYTHONPATH` so descendants install the network guard. That
`sitecustomize` imports `ci_no_network`, which imports `requests.adapters` to refuse
outbound HTTP. **So every descendant interpreter starts with `requests` already in
`sys.modules`**, and the test's `python -c` child failed on a module the renderer
never touched.

Reproduced locally under the harness (`1 failed, 4 passed`), and the fix makes it
pass under the same harness (`5 passed`, with the `ci_no_network` warning still
present, so the guard is genuinely active). The fix pins the child's `PYTHONPATH`
to the repository and sets `PYTHONNOUSERSITE`, **and** measures what the import
ADDS to `sys.modules`, so a future preload cannot make the check vacuous in either
direction. The invariant is unchanged; it is now measured correctly. This matters to
this incident directly: that renderer is what produces the copy.

### `test` — `test_scenario_5_other_governor_limit_still_blocks`

Reproduced locally, so not environmental. First guess (a stale ledger `cycle_key`)
was **wrong** — the fix did not help. Instrumenting the governor gave the answer:

```
cycle_rolled    true
ledger          cycle_key "2026-10-01", cycle_reset_at "2026-10-01T23:39:15.948851+00:00"
decision        run_budget 3266, reason "pace", days_remaining 30.0
```

The ledger's cycle came from the **provider header**, overriding the seed. The
fixture's `x-api-next-billing-date` is the captured `2026-10-01`, which is now in
the **past**, so every refresh reports a fresh cycle key, the governor rolls the
cycle and **discards the seeded spend** — and the scenario stopped exercising a
spent daily allowance, asserting instead against a healed budget of 3,266.

A "next billing date" cannot be a pinned literal. `BILLING_DATE` is now derived at
import time. The captured value is kept verbatim as `CAPTURED_BILLING_DATE` and
still feeds `PROVIDER_HEADERS`, because every unit-level use pins `now=NOW`
(2026-09-03), against which 2026-10-01 IS in the future — those tests are
time-independent and stay on the real captured evidence.

60 passed, 105 subtests passed. Integrity manifest OK (35 checked, 0 mismatch).

## Step GA — selective reactivation: what is ready and what is missing

### The clean group is 20 contacts

| | |
| --- | --- |
| in the nine campaigns, never contacted, copy complete and concrete | **11** |
| parked, never contacted, title recovered and verified | **9** |
| **total clean** | **20** |

Everyone else is in one of two cohorts that a technical repair cannot fix:
**5,352** in-campaign leads already received the blank first email and their thread
subject cannot be changed; **1,966** parked leads already received a broken email
and cannot be returned without restarting their sequence.

### Prepared, not executed

The only non-destructive path is a NEW campaign carrying the same approved 4-step
sequence, holding those 20. They have never been emailed, so starting at step 1 is
correct and there is no history to lose. The nine existing campaigns stay PAUSED and
untouched, so the 5,352 keep their position while that decision is pending, and
**FINANCE keeps its prior pause** regardless of what is decided about the rest.

Files: `healthy_contacts.jsonl` (11), `clean_parked_repair.jsonl` + `clean_parked_verify.json` (9),
`blank_thread_in_campaign.jsonl` (5,352), `recovered_from_sources.jsonl` (1,309 prepared, 9 applied),
`unrecoverable_parked.jsonl` (377).

No campaign created. No lead moved. Nothing activated.

## Step GB — the recovery extended to the other 287, and two rules tightened

The first pass covered the 1,686 parked for a `role_display_*` reason. The other
**287**, parked for `posting_title_cannot_pass_the_gates`, carry the same kind of
evidence, so the same pass was run on them: **189 of 287 (65.9%)** have a
recoverable concrete title, 98 do not.

Reviewing the output found two ways a separator split can cut INSIDE a noun phrase,
and both were narrowed rather than left in:

* **`/` removed from the separator set.** A slash joins alternatives within a
  phrase: `Principal OT Cybersecurity / ICS Security Architect`. It cost 21 of the
  287 cohort's recoveries, which were fragments, and 0 of the 1,686 cohort.
* **a coordination truncated to its first element is rejected.** Measured on
  `Executive Director and Assistant, Associate or Professor of Medicine, Student
  Health Services`, whose comma split produced `Executive Director and Assistant`.
  The guard fires only when the conjunction sits immediately before the final word,
  so `Legal Coordinator & Executive Assistant` is untouched. It cost 8 of the 1,686.

Final counts, with both guards in force:

| cohort | parked | recoverable | no usable title |
| --- | --- | --- | --- |
| `role_display_*` refusals | 1,686 | **1,301** | 385 |
| `posting_title_cannot_pass_the_gates` | 287 | **189** | 98 |
| **total** | **1,973** (+2 repaired earlier) | **1,490** | **483** |

All 9 displays already applied were re-checked against the tightened rules:
**9 unchanged, 0 affected.**

### The clean group is 23

3 more parked leads that were never contacted turned out to have a recoverable
title in the 287 cohort. Applied and read-back verified: **3/3 identical, 0 field
mismatch, 0 drift, 0 unresolved tokens**.

| email | recovered display | note |
| --- | --- | --- |
| jim.detore@cgoncology.com | Associate Director | |
| kibay@radialentertainment.com | Senior Director | |
| jaclyn.velez@ucf.edu | Student Health Services | department-like, flagged |

| | |
| --- | --- |
| in the nine campaigns, never contacted, copy complete and concrete | 11 |
| parked, never contacted, title recovered and verified | **12** |
| **total clean** | **23** |

2 of the 14 never-contacted parked leads still have no usable title
(`nichole.downes@wonderful.com`, `jwiedemann@devonbank.com`) and stay parked.

Flagged as department-like rather than a person's title, repaired but marked:
`Promotional Review Operations` (x3), `AI and ML Engineering`,
`Student Health Services`. 5 of the 12.

## Step GC — both red CI checks fixed and both suites green through the CI harness

| suite, run exactly as CI runs it | result |
| --- | --- |
| `rebuild/run_offline_tests.py tests_core -q` | **2172 passed** |
| `rebuild/run_offline_tests.py tests -q` | **3754 passed, 1 skipped** (a Windows-only skip; CI's `minimum=3755` counts it) |
| `ci_check_integrity.py` | 35 checked, 0 mismatch, 0 absent |
| pyflakes on both touched files | clean |

## Step GD — production state at the close

| | |
| --- | --- |
| cron | `0 3 * * *` (restored) |
| start command | carries the hour guard; last deploy 00:54:22Z `SUCCESS`, start declined |
| `scheduled_executions` | migration 21 applied, **0 rows** — 2026-10-03 unclaimed, so the 03:00Z tick can claim and run |
| run lock | `held=0` |
| nine campaigns | PAUSED, FINANCE keeps its prior pause |
| next tick capacity | stored 22,500 / free 2,500 / `room_needed` 0 — it can acquire |
| safe rotation candidates left | 11 |

PR #135 holds the pre-enrichment copy gate, the Apollo rate-limit latch and the two
CI fixes. It is **not merged**, so none of it is live yet.

### Timing note for the merge

A merge redeploys GTM Core Canary, and a redeploy has been observed to consume the
following cron tick. Merging well before 03:00Z or after about 03:30Z keeps the tick;
merging in the few minutes before it risks losing it. Either way the hour guard and
the day claim stop the deploy itself from starting a run.

## Step GE — two claims of mine corrected, with the code as the evidence

### 1. A deploy INSIDE 03:00–05:59Z on an unclaimed day DOES start a run

I said the hour window plus the day claim closed "a deploy must not start a run".
That is not true, and the code says so. `cmd_run_daily` checks the window first,
then takes the lock, then claims the day — and `scheduled_execution.claim` succeeds
whenever the day has **no row**. So:

| start | window | day claim | result |
| --- | --- | --- | --- |
| deploy outside 03–05:59Z | **declines** | not reached | no run (verified live at 00:54Z) |
| deploy inside the window, day already finished | allows | **ALREADY_COMPLETED** | no run |
| **deploy inside the window, day unclaimed** | allows | **CLAIMED** | **a full run starts** |

And it is worse than neutral: the deploy-started run **takes the day's claim**, so
the genuine 03:00Z tick is then refused as the duplicate.

What IS genuinely closed: no day can run twice, and no start outside the window
happens at all. What is not: inside the window the FIRST start wins, whoever
triggered it. Closing it needs something that tells a cron start from a deploy
start, and nothing inside the container does. The available lever is narrowing the
window to the cron's own minute — a decision about the nightly run's start
tolerance, not a free fix, so it is **not** done here.

Asserted, not glossed:
`test_a_deploy_inside_the_window_on_an_UNCLAIMED_day_DOES_start_a_run`.

### 2. "Merging well before 03:00Z keeps the tick" — withdrawn

I have no measurement for that. What is recorded is only that a redeploy has been
observed to leave the following tick silent. The real next start is the thing to
observe, and it has not happened yet.

## Step GF — the Apollo wait is now honoured in full

`_handle_global` capped the wait at `min(retry_after, 900)`. Worse, the **adapter**
already clamped it: `apollo.py` did `min(parsed_retry, 900.0)` when parsing
`Retry-After`. Measured consequence on the 10-02 budget:

| | |
| --- | --- |
| rate-limited `people_search` calls | **1,351** |
| of those carrying a `retry_after` | **1,351** (100%) |
| the recorded value, on every one | **exactly 900.0** — the old ceiling |

Every refusal sat on the clamp, so Apollo was asking for **at least** 900s and we
were shortening it. The raw header value is unrecoverable because the clamp ran
before the value was recorded.

Both caps are gone. The provider's wait is preserved in full at the adapter and
honoured in full by the latch; 60s is the fallback only when no header is given, and
a negative or non-finite value never reads as "retry now". The latch stays **per
run** and is never written to `provider_state`, so a long wait ends Apollo for that
run without spending instead of becoming a cross-run outage.

Coverage checked rather than assumed — all three Apollo call sites go through
`_guard_provider`, which tests the latch first: `enrich_organization` (chargeable),
`search_people` (free), `match_person` (chargeable).

## Step GG — run `20261002T233322.605777Z-b7478fc9`, reconciled in full

Closed 2026-10-03T00:17:38Z, `stop_reason`
`target_not_reached:apollo_request_allowance_insufficient`.

| | |
| --- | --- |
| approvals | **42** |
| **confirmed Instantly creations** | **0** — `delivery_receipts` has no row for any of the 42 |
| Airtable rows written | **0** |
| Instantly outbox | 25 `pending`, 17 `blocked` |
| Airtable outbox | 25 `pending`, 17 `blocked` — the SAME 17, reason for reason |
| requests on `prod-scheduled-20261002` | **7,407** |
| credits reserved | **56** (55 `person_match` + 1 `organization_enrich`) |
| capacity at close | stored 22,500 / free 2,500 |

Blocked, identical on both channels: 6 `compliance:unknown_jurisdiction:absent`,
1 `compliance:uk:not_a_verified_corporate_subscriber`, and 10 copy QA —
4 `role_display_contains_unsafe_characters`,
3 `role_display_carries_an_appended_qualifier`,
3 `role_display_longer_than_48_chars`.

Zero production is the result, and it is reported as the result: the run spent 56
credits, created nothing, and the 10 copy refusals produced no Instantly lead and no
CRM row — which is the invariant holding.

## Step GH — every count quoted in this incident, reconciled by ID

### Affected threads: 5,630 → 5,352 → **5,343**

7,777 distinct addresses received at least one blank-subject message. Where each one
is now:

| | |
| --- | --- |
| still in a campaign, mutable — the cohort a decision can act on | **5,343** |
| moved to the hold list | 1,961 |
| still in a campaign, terminal (338 bounced, 1 completed) | 339 |
| still in a campaign, replied (excluded) | 80 |
| no longer in any campaign or on the hold list | 54 |
| **total** | **7,777** |

The 5,630 predates the parking and the status changes. And my own 5,352 was **9 too
high**: those 9 were counted because `timestamp_last_contact` was set, but their
contact was at 2026-10-02T22:09:27Z — one minute before the external pause, **after**
the send snapshot ended, and all 9 appear in the post-resume record with verdict
**OK**. They received correct copy with a real subject, so their thread is fine and
they are not blank-thread victims. **The cohort is 5,343.**

### Parked: 1,688 + 287, and the 1,974 I once quoted

| | |
| --- | --- |
| copy-unusable cohort moved out | 1,688 |
| generic-subject cohort moved out | 287 |
| overlap | **0** |
| union | 1,975 |
| actually on the hold list, read from the API | **1,975** |
| in the union but not on the list / on the list but in neither cohort | 0 / 0 |

1,974 was an off-by-one in a running tally. 1,686 of the 1,688 carry a recorded
reason; the other 2 were repaired immediately.

### Repairable: 1,309 → 1,301 and 210 → 189

| | |
| --- | --- |
| copy-unusable cohort | **1,301** of 1,686 |
| generic-subject cohort | **189** of 287 |
| overlap | **0** |
| total distinct recoverable | **1,490** |
| applied and read-back verified | **12** |
| recoverable but deliberately NOT applied | 1,478 |

Both drops are refusals I added after reading the output, never recoveries lost to a
bug: 8 from rejecting a coordination cut at its first element, 21 from dropping `/`
as a separator. All 12 applied leads were re-checked against the tightened rules:
**12 unchanged, 0 affected.**

## Step GI — the reactivation proposal, per campaign and function

The routing is **read from the code** (`CAMPAIGN_BY_FUNCTION`), not hand-written. My
first attempt did hand-write it and wrongly reported five leads as unroutable; the
real map folds ten function keys onto the nine campaigns
(`customer_success`/`customer_support` to CUSTOMER_EXPERIENCE, `gtm_revenue` to
GTM_SYSTEMS, `engineering` to AI_TECHNICAL, `marketing` to MARKETING_CREATIVE).

23 clean candidates, **5 held back** on instruction until someone confirms they are
real job titles: `Promotional Review Operations` (x3), `AI and ML Engineering`,
`Student Health Services`. **18 proposed:**

| campaign | contacts |
| --- | --- |
| OPERATIONS | 7 |
| PEOPLE_HR | 3 |
| AI_TECHNICAL | 2 |
| CUSTOMER_EXPERIENCE | 2 |
| **FINANCE** | **2** — listed for completeness, its pause is kept |
| GTM_SYSTEMS | 1 |
| PRODUCT | 1 |

**0 of the 18 route to a campaign other than the one they already sat in**, so no
re-assignment is involved. An earlier run of this check reported 4 disagreements;
that was my own comparison of a campaign name against a campaign id, not a finding.

Every one of the 18 is absent from the send record entirely — asserted, must be 0,
and is 0. Files: `reactivation_proposal.jsonl` (18),
`reactivation_held_back.jsonl` (5).

Nothing executed: no campaign created, no lead moved, nothing unpaused.

## Step GJ — cron paused before the tick, and the deployment that did not happen

| | |
| --- | --- |
| run lock at 02:40:52Z | `held=0` |
| `cronSchedule` set to | `null`, applied and **verified** `None` at 02:41:17Z |
| margin before the 03:00Z tick | 19 minutes |

Then the deployment check, and it found a mistake of mine.

**PR #135 was merged into `main` (`9045ec67`), but the Core deploys from
`feat/rebuild-core`.** Every other PR in this incident targeted `feat/rebuild-core`
(#129–#134). So the merge deployed only the two services that track `main`:

| service | branch | deployment | start command |
| --- | --- | --- | --- |
| GTM | `main` | `5d84dff5` 02:40:25Z | prints `TGTC_PAUSED_PENDING_CREDITS` and exits |
| GTM Approved Sync | `main` | `31ae96b3` 02:40:25Z | prints `TGTC_PAUSED_PENDING_CREDITS` and exits |
| **GTM Core Canary 1000** | `feat/rebuild-core` | **`a5ce3b40` 00:54:22Z, commit `8242de24`** | the guarded daily controller |

Both logs confirm it: `Starting Container` then the paused line, nothing else. **No
unexpected run started, nothing spent, nothing enrolled.**

So nothing from #135 is live. The effective Core commit is still `8242de24` from
#134. [TGTChq/GTM#136](https://github.com/TGTChq/GTM/pull/136) carries the same four
commits onto the branch that actually deploys. Checked before opening it:
`git diff --diff-filter=A HEAD origin/feat/rebuild-core` is **empty**, so nothing
exists on the deploy branch that this branch lacks, and the only commits it has that
this branch does not are the merge commits of my own earlier PRs.

## Step GK — the 25 pending deliveries, diagnosed

| | |
| --- | --- |
| Instantly rows `pending` | 25, `last_error = campaign_status_2` |
| Airtable rows `pending` | 25, `last_error = awaiting_instantly` |
| attempts / lease | 1 / none held |
| `available_at` | 01:16–01:17Z, already past |

**Why they did not process: our own rule, not a provider limitation.**
`delivery.py` deferred on any campaign status other than 1, and the nine campaigns
are paused. The rows were **deferred** — not blocked, not failed — with
`available_at = now + 1h`, which is the design: they drain once a campaign is
reachable. The last delivery drain of the run reported `{}` for both channels
because it ran at 00:17:38Z while those rows were not due until 01:16:57Z. Nothing
has drained them since, because only a run drains and the Core cron has not fired.

The Airtable half waiting on `awaiting_instantly` is the CRM invariant working: a
record only after a genuine Instantly creation.

### Pre-flight on the real 25, which found that 2 are NOT eligible

| | |
| --- | --- |
| all five copy fields present | **25 / 25** |
| unresolved tokens | **0** |
| `skip_if_in_workspace` set | 25 / 25 |
| distinct addresses | 25 (no internal duplication) |
| target campaigns | OPERATIONS 14, GTM_SYSTEMS 4, CUSTOMER_EXPERIENCE 3, AI_TECHNICAL 2, MARKETING_CREATIVE 1, ECOMMERCE 1 — **none to FINANCE** |

But reading the 25 subjects found **2 bare function nouns**, which is exactly what the
287 parked leads were parked for:

| outbox | approval | subject | original posting title |
| --- | --- | --- | --- |
| 18122 | 9070 | `customer support role` | `ISSM / IT Support` |
| 18150 | 9084 | `operations role` | `TELLER/CUSTOMER SERVICE REP` |

Both posting titles do pass `role_display_send_safe` unchanged, so a title IS
available — but `ISSM / IT Support` and `TELLER/CUSTOMER SERVICE REP` read badly as a
subject line against the 591 reference leads, so both are **held back** with the
candidate recorded, the same treatment as the five department-like displays.

**So 23 of the 25 are eligible, not 25.** The other 23 carry concrete titles
(`Microsoft Cloud Engineer`, `Plant Accountant`, `Autism Clinic Service Coordinator`,
`Software Engineer II`, and so on).

This also exposes a gap worth stating separately: **the production gates do not reject
a bare function-noun subject.** `role_display_send_safe` checks length, characters,
appended qualifiers and headlines, and buzzword gates read the rendered copy — none of
them objects to `operations role`. These two rows were created on 2026-10-02, so the
pipeline can still produce such subjects today. The 363 I re-rendered earlier were a
repair, not a gate. Closing it would change the funnel, so it is flagged, not changed.

### A paused campaign accepts leads and sends nothing — tested, not assumed

First attempt was inconclusive and said so: `POST /leads` returned 200 with the
**existing** lead id, because the address was already in the workspace, so nothing
new was created and the campaign had no reason to change. Repeated with a
plus-address on the same internal mailbox, which is a genuinely new lead:

| | before | after |
| --- | --- | --- |
| campaign status | 2 PAUSED | **2 PAUSED** |
| `emails_sent` | 1 | **1** |
| lead contacted | — | **no** (`timestamp_last_contact` null, step `None`) |

Probe lead then deleted, re-read returned **404**. No residue, and the slot returned.

### The recovery path, prepared and not run

`python -m tgtc_core deliver` drains the existing outbox and nothing else.
`cmd_deliver` claims **no budget, no day, no run lock** and passes no window guard.

- **no enrichment repeated** — the complete payload is already in
  `delivery_outbox.payload_json`; the drain makes zero Apollo and zero Fantastic calls
- **no duplicates** — the idempotency key, plus `resolve_membership` on any retry
  (`attempts > 1`), which records an existing lead as `reconciled` rather than
  creating one, plus `skip_if_in_workspace` in the payload
- **no new budget** — delivery only
- the Airtable rows carry their own deferral, so they need a **second** pass later,
  not the same sweep. Proved in the test rather than assumed.

Delivering into a paused campaign is gated behind
`TGTC_DELIVER_INTO_PAUSED_CAMPAIGNS`, **off by default**, because those leads do send
the moment anyone resumes the campaign — that is an operational choice, not a repair.
COMPLETED (3) is deliberately excluded: adding leads to a drained campaign flips it
ACTIVE and it starts sending.

7 tests in `tests_core/test_deliver_into_a_paused_campaign.py`, including that the
default still defers, that exactly one create call is made and a second drain is a
no-op, that an existing lead is reconciled rather than recreated, that the Airtable
row follows the creation, that only PAUSED is opened up and never COMPLETED, and that
only the literal value `1` counts as set.

## Step GL — exact termination of the run, and the 1,351 verified as real 429s

From the `daily/end` record:

```
stop_reason   target_not_reached:apollo_request_allowance_insufficient
target 1000   shortfall 1000   target_met false   rounds 4
blocks        []                      <- no acquisition block was ever attempted
drains        [{phase: backlog, cycles: 4, outcome: drained, fresh_after: 0}]
rotation      {needed: 0, reason: enough_room, free_before: 2500, stored_before: 22500}
```

`tgtc_core/daily.py` compares the remaining request allowance against the projected
requests for the next block: with `10,000 − 7,407 = 2,593` left, the projection
exceeded it, so the controller **declined to start a block at all**. A pre-spend
refusal, not a failure — which is why `blocks` is empty and the shortfall is the full
target.

The 1,351 refusals, separated as asked:

| | HTTP | error_class | n | distinct receipts |
| --- | --- | --- | --- | --- |
| rate limit | **429** | `rate_limited` | **1,351** | 1 — `retry_after: 900.0` on every one |
| local budget refusal | — | — | **0** | — |
| any other rejection | — | — | **0** | — |

There is no attempt in the whole run with a null `http_status`, so nothing was a
local refusal dressed up as a provider one. Every single refusal reached Apollo and
came back 429.

## Step GM — the 03:00Z tick of 2026-10-03 was OMITTED

Recorded as fact. **No recovery launched**, per instruction.

The cron was paused at 02:41:17Z, 19 minutes before the scheduled minute, so the
tick could not fire. Verified at 03:02:10Z, four independent ways:

| check | result |
| --- | --- |
| `scheduled_executions` for 2026-10-03 | **0 rows** — the day was never claimed |
| `run_log` entries after 02:55Z | **0** |
| provider attempts after 02:55Z | **0** |
| run lock | `held=0` |
| the 25 + 25 pending outbox rows | **untouched** |

So no production happened on 2026-10-03, by deliberate choice, and the day's budget
namespace `prod-scheduled-20261003` is unused and intact. A recovery, if one is ever
authorised, must name itself explicitly through `TGTC_RUN_RECOVER` and would spend
that day's remaining allowance — it is not started automatically and was not started
here.

## Step GN — CI on the PR head, and why it needed dispatching

`.github/workflows/ci.yml` triggers on `pull_request: branches: [main]`. PR #136
targets `feat/rebuild-core`, so **no CI ran on it automatically** —
`gh pr checks 136` reported "no checks reported on the branch". The workflow also
accepts `workflow_dispatch`, so CI was run deliberately against the branch head.

| | |
| --- | --- |
| run | `37092615371` |
| commit | `77bdef4` (the PR's head) |
| `core` | **success** |
| `test` | **success** |

Pushes to `feat/rebuild-core` DO trigger CI, so the merge itself will produce a
second, independent signal.

## Step GO — an inert Core start, prepared, exercised and verified INSIDE the window

The reason this was needed: `cronSchedule: null` does **not** stop a
deployment-started run. A deployment starts the service's command regardless of the
schedule, we are inside 03:00–05:59Z so the hour guard allows it, and
`scheduled_executions` is empty so the day claim allows the first start. Merging
#136 as things stood would have begun a full acquisition run.

What was done, in order:

1. **The original start command was saved verbatim** to
   `core_start_command_backup.json` (713 characters). It references only
   `TGTC_RUN_KIND`, `KIND`, `H` and `BID` — no secret values, so nothing sensitive
   was written to disk. It is the `sh -ec` wrapper that derives the run kind from the
   UTC hour, then runs `migrate`, then `budget`, then `run-daily --target 1000`.
2. **The start command was replaced** with
   `sh -ec 'echo TGTC_CORE_INERT: ...; echo no migrate, no budget, no run-daily'`.
   A start-command change alone did **not** create a deployment, so at that point it
   was configured but unexercised — stated as such rather than claimed as verified.
3. **A second, independent guard was added**: `TGTC_RUN_WINDOW_UTC=closed`.
   `start_allowed` fails closed on an unreadable window
   (`window_unreadable:'closed'`), so even if a deployment somehow ran the ORIGINAL
   command it would decline before the lock, the budget claim, the day claim and any
   spend. Verified in code: `(False, "window_unreadable:'closed'")`.
4. The variable change **did** create a deployment, which exercised the inert start.

Deployment `10892838`, 2026-10-03T03:25:05Z, commit `8242de24`, status SUCCESS, and
its entire log is:

```
TGTC_CORE_INERT: deployment start suppressed for the copy-incident merge
no migrate, no budget, no run-daily
Starting Container
```

No `run kind=...` line, no migrate, no budget, no run-daily. Confirmed in the
database at 03:26Z:

| check | result |
| --- | --- |
| `scheduled_executions` rows | **0** |
| `run_log` entries after 03:20Z | **0** |
| provider attempts after 03:20Z | **0** |
| reservations on `prod-scheduled-20261003` | **0** |
| `budget_claims` for `prod-scheduled-20261003` | **0** |
| the 25 + 25 pending outbox rows | untouched |
| run lock | `held=0` |

One honest footnote: a `prod-scheduled-20261003` budget ROW exists, created
00:54:47Z. That was the 00:54Z deployment, whose start command creates the budget
before `run-daily` runs; `run-daily` was then declined by the hour guard. The
namespace has 0 reservations and 0 claims, so it is unused, not spent.

**So a deployment of #136 now cannot start a run**, proven rather than argued, by two
independent mechanisms.

## Step GP — the authorised recovery, built with the guarantees that were asked for

The drain cannot run from this machine: `TGTC_DATABASE_URL` points at
`postgres-core.railway.internal`, which does not resolve outside Railway
(`getaddrinfo failed`), Postgres Core exposes no public TCP proxy (no
`RAILWAY_TCP_PROXY_DOMAIN`, no `DATABASE_PUBLIC_URL`), and `railway ssh` into the
Core refuses because a cron container is `exited`. So the recovery has to run as a
deployment command, which means committed, reviewed code rather than an ad-hoc
script.

`tgtc_core/services/delivery_recovery.py` plus the
`python -m tgtc_core recover-deliveries` subcommand:

* **mutual exclusion** — the command takes the production run lock before reading or
  writing anything, and releases it in a `finally`. A concurrent run or drain is
  refused with `another production run holds the run lock`.
* **the named rows are withheld first**, before any delivery, with the reason
  `recovery_hold:withheld_by_operator` recorded in `blocked_reason`. A row that has
  already moved is reported as `not_pending_anymore` rather than forced.
* **revalidation** — every remaining row is re-checked and blocked with a named
  reason if it fails. The copy contract is **not restated**: `copy_block_reason` is
  the production rule, asked exactly as approval asks it, so a Control payload is not
  refused for lacking rendered copy and a Challenger payload gets the same named
  refusal it would get anywhere else. Two checks are added on top and only for this
  recovery: the destination must be on the configured allow-list, and a bare
  function-noun subject is withheld as
  `recovery_hold:subject_is_a_bare_function_noun`.
* **verification by id** — for every row that reports delivered, the genuine receipt
  is read from `delivery_receipts` and the lead is then fetched back from the
  provider by that `external_id`. A read-back that fails is reported in `unverified`,
  never assumed.
* **Airtable strictly second** — the Instantly channel is drained alone, then
  Airtable. Its existing `awaiting_instantly` gate means a CRM row can only proceed
  where a genuine creation exists.
* **no enrichment** — the payload is already on the row, so nothing calls Apollo or
  Fantastic. Asserted: `request_attempts` is unchanged across a recovery.

The generic-subject rule lives in the recovery and **not** in `DeliveryService`, on
purpose: the production gates do not reject a bare function noun, and changing that
would change the funnel. This is a recovery standard applied to this recovery.

15 tests in `tests_core/test_recover_deferred_deliveries.py`, including that a
withheld row is never delivered and never produces an Airtable record, that a failed
read-back is reported rather than assumed, that exactly one create call is made and a
second recovery is a no-op, and that the recovery spends no Apollo and no Fantastic.

## Step GQ — the restore procedure, and why its last step waits for 06:00Z

The Core must end on its original start command and `0 3 * * *`, reached without
triggering acquisition. Working through it honestly, there is exactly one ordering
that never allows a start, and its final step cannot happen inside the window.

| step | what redeploys | what the container would run | start decision |
| --- | --- | --- | --- |
| 1. run the recovery as the start command | yes | `recover-deliveries` only | n/a — not `run-daily` |
| 2. restore the original start command | yes | the real controller | **declined**, `TGTC_RUN_WINDOW_UTC=closed` |
| 3. restore `0 3 * * *` | no — a cron change alone does not deploy | — | — |
| 4. delete `TGTC_RUN_WINDOW_UTC` | yes | the real controller | hour ≥ 06:00Z → **declined**, outside `3-5` |

Step 4 is the one that cannot be done inside 03:00–05:59Z: with the window back to
its default and the day unclaimed, that redeploy's start would be allowed and a full
run would begin. Doing it at or after 06:00Z makes the hour guard decline it, which
is the whole point of the guard.

So between step 2 and step 4 the Core sits on its real start command with the window
closed — unable to start at all, which is operationally the same as the cron being
paused, and it costs nothing: the 2026-10-03 tick is already recorded as omitted and
the next scheduled tick is 2026-10-04 03:00Z.

Nothing here is a judgement call left open. The commands are fixed; only the clock
gates step 4.
