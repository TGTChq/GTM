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
