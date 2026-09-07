# Company identity, resolved generally

Base `e97fc3c` (PR #116). This replaces five hand-written company equivalences with
rules that derive a stable identifier from a company's own published name, and
deletes six of the seven manual entries because the rules now reach them.

## The conclusion this corrects

The 2026-09-07 06:29Z run stopped because its 200-call Apollo authorization was
consumed. **That explains the stop and nothing else.** It does not show that the
code was finished, and it cannot: the run executed at `43ee7ba`, so it predates
both PR #116 and this change and validates neither. What it did do is expose
defects — the imputed per-source credit, the stale per-run approved goal, and the
company holds this document is about.

## What was actually wrong

Five companies were held as `linkedin_slug_domain_disagreement` or
`selected_name_not_corroborated_by_identity`. The resolver's test for "these two
identifiers name one company" was: is the published name literally CONTAINED in
both anchor strings? That holds only for decorations around an unchanged brand, so
every other honest way a company writes its own domain read as a conflict:

| production record | slug | domain | why it read as two companies |
|---|---|---|---|
| BS&B Safety Systems | `bsbsafetysystems` | `bsbsystems.com` | the domain drops an interior word |
| Zwicker & Associates, P.C. | `zwickerassociatespc` | `zwickerpc.com` | the domain keeps first and last words |
| Kai | `kaisecurity` | `kai.security` | the domain was split at its first dot, discarding half the brand |
| Nash | *(none)* | `usenash.com` | a registrar vanity prefix |
| EndeavorB2B / Endeavor Business Media | `endeavorb2b` | `endeavorbusinessmedia.com` | a rebrand: two names, one per anchor |

Fixing those five by name fixed those five. A sixth company with the same shape was
still held, and would be held today.

**How many company holds there were in total is not established.** Those five, plus
Minth North America, are every DISTINCT company recoverable from the deployed
maintenance pass -- and that pass was capped twice over: it examined the first 200
retained rows and kept at most 25 per-contact details. Within those 25 details lie
six distinct companies. The run's own delivery receipt recorded 27 contacts withheld
before creation, and the capped recomputation counted 24 company holds among a
different, truncated population. Those are three different numbers over three
different populations and none of them is the others. The corpus below therefore
covers every company hold the EVIDENCE shows, not every company hold the run had.

## The replacement

`company_anchor_evidence.py` answers one narrow question: **can this anchor be
constructed from the words of this published name?** Every rule is an equality
between the anchor and some concatenation of the name's own words — never a
similarity, an edit distance or a token-overlap ratio.

* `domain_full_label_key` — the whole domain including its TLD spells the name.
  A `.com` domain simply gains `com` and matches nothing, so no TLD allowlist is
  needed; a brand TLD becomes legible.
* `vanity_prefix_stripped` — a closed, short list of registrar prefixes, and the
  remainder must equal the name EXACTLY. `the`, `go` and `hi` are excluded because
  they open ordinary brands: with `the` this would read `theresa.com` as "Resa".
  Graded as weaker than the others, so it can never reach high confidence.
* `token_subsequence` — the anchor is an ordered subset of the name's words,
  beginning at the first word, whole words only. Two words minimum, six characters
  minimum.
* rebrand — two published names on ONE record, each exactly corroborated by a
  DIFFERENT identifier, where one name's leading token (six characters or more)
  opens the other's key. The LinkedIn slug names the company today, so the
  slug-corroborated name is displayed and the other recorded as superseded.

There is deliberately **no initialism rule**. Three- and four-letter initialisms
collide across unrelated companies, so a general rule for them would merge
organizations rather than recognise one. That is the single manual entry that
survives (`rmcweb.com` for Resource Management Concepts), and it now records why.

**Nothing here compares two companies.** Every rule relates one published name to
one anchor from one provider record. Two organizations never appear in one record's
organization fields, so no rule in this file can merge them.

## Before and after, one corpus

`acceptance/company_identity_corpus.py` — run it against a checkout of the deployed
resolver for "before" and against this branch for "after". Measured, not asserted:

```
holds corrected: 6 | still held: 12 | newly held: 0 | unchanged: 4
```

The six: the five production companies **plus Northwind Logistics Group**, which
appears in no list anywhere and is the point — the rules reach a company never seen.

The twelve still held, each for a reason a general rule must respect:

| held | why it must stay held |
|---|---|
| Minth North America on `minthgroup.com` | a subsidiary under its parent group's domain |
| Instagram on `meta.com` | a brand under its parent's domain |
| two employers on `applicantpro.com` | a shared applicant-tracking host is not an employer |
| Resource Management Concepts on `rmcweb.com` | an initialism |
| Hex Technologies on `hexagon.com` | a shared opening fragment is not a shared identity |
| Diamond Jo Casino & Hotel on `boydgaming.com` | a property under an operator's domain |
| Mass. DDS on `mass.gov` | a portal domain shared by every agency on it |
| "Resa" on `theresa.com` | `the` is not a vanity prefix |
| Acme with slug `globex` | the name is built into neither identifier |
| a malformed label, and a record with no identifier | not a company name; no identity |

Each hold now carries `missing_evidence.need` naming what would settle it, so a
hold is a request for evidence rather than an anonymous refusal.

## One known gap, stated rather than hidden

A published name that **opens** a longer slug is read as the same brand decorated —
this is the pre-existing `prefix` tier, and it is what lets `clark`/`clarkaudit`
through. It cannot distinguish that from a homonym whose brand happens to open
another organization's slug (`Apple` with slug `applebank` on `apple.com`). Verified
to behave identically on the deployed resolver, so it is a limitation of the system
rather than a regression here; narrowing it would re-hold Clark and Carpe. It is
carried in the corpus as `homonym_prefix` with its expectation set to what the
system actually does, so it is neither reported as a pass nor buried among failures.

## The trace: what each stage uses as company identity

Both new and recovered postings take the same path -- work resumed from custody
re-enters the ordinary enrichment loop, so nothing below is specific to either.

| stage | identity it uses | verdict |
|---|---|---|
| acquisition dedupe | `posting_identity` -- the posting, not the company | correct; a different unit |
| grouping for enrichment | `company_key_for_job`: safe domain, else normalized name | domain-first, slug-blind -- see below |
| opportunity collapse | union-find over `org_linkedin_slug` AND the safe domain | already unions both; ATS hosts already refused |
| display cache | `linkedin:<slug>` / `domain:<host>` | **had no version or age check on read** -- FIXED |
| outbound name | `resolve_company_display` | **each identifier read one way only** -- FIXED |
| Airtable suppression | `domain:` + `name:` | **ignored the stable key it stores** -- FIXED |
| approval / send-safe | `_outbound_company_hold` | unchanged; still gates every row |

**Why the grouping key was left alone.** It is slug-blind, so two postings for one
organization under two domains would be enriched as two companies. The collapse
stage already unions exactly those two anchors ahead of it, so the case is covered;
and changing the grouping key changes how Apollo calls are BATCHED, which is a
spend-affecting change with no production evidence behind it. It is recorded here
rather than altered.

## Two further identity defects this exposed

**The display cache had no version or age check on read.** A decision computed once
was reused forever, so the first answer a company ever got was the answer it kept —
and every later correction reached only companies never resolved before. Entries now
carry `resolver_version` and `resolved_at`, and either a version change or
`CACHE_TTL_DAYS` retires one. An entry with no recorded age is treated as stale, not
fresh. Human-reviewed overrides are exempt: they are a decision, not a cached
computation, and are revised by editing the overrides file.

**Airtable stores the resolver's stable key and suppression ignored it.**
`Outbound Company Identity` holds `linkedin:<slug>` or `domain:<host>` on every row
the writer created, while `_company_identity_keys_from_fields` read only `Website`
and `Company`. One organization posting under two domains with two labels produced
two disjoint key sets and two active rows for one company × function. The key is now
honoured. It can only ADD matches, and only between rows that already agree on a
LinkedIn organization; a `domain:` identity on an applicant-tracking host is still
refused, and a held row carries no key and is unaffected.

## Cost: known zero is not unknown

`orchestrator/source_cost.py` gives three states instead of two. A free lane — the
145 direct ATS boards, the public free feeds — has a cost of **zero as a fact about
how it works**, not a missing bill. Only a PAID provider that returned no billing
total, or a source nobody has classified, is unknown. Under the previous rule,
enabling the free ATS lane would have blanked the run's whole cost figure and looked
like a reporting regression.

## Gates

Regressions were run against the DEPLOYED version of each file they cover:

| reverted to `origin/main` | result |
|---|---|
| `company_display_resolver.py` | the six corrected rows hold again (measured by the corpus script) |
| `airtable_client.py` | both suppression regressions fail |

The second-batch suppression fixture deliberately gives the two rows different
domains AND different labels: with a shared label the legacy `name:` key would have
caught it anyway and the test would have proved nothing.

## Still open, and blocked

**The 27 withheld contacts are not yet individually explained.** The scan that would
do it is uncapped as of PR #116, but reading it needs a maintenance pass, and
triggering one requires a temporary cron change that is refused in this session
(`serviceInstanceUpdate` is classifier-blocked). The concrete missing action:

1. GTM service `3a41d0d7-cd66-4f53-baa6-886266ddbbed`, environment
   `bae427bd-64a6-4f4e-8f56-fbd406985434`: set `cronSchedule` to a few minutes ahead.
2. Do not push during the window — a push redeploys and kills the container.
3. `railway logs <deployment-id-current-at-capture-time> -n 5000`. Read the
   `send_safe_forensics_summary` line first (the original writer receipts), then the
   per-contact `send_safe_forensics_contact` lines.
4. Restore `cronSchedule` to `0 3 * * *`.

Nothing else is needed: `MAINTENANCE_ONLY=1`, `FANTASTIC_JOBS_ENABLED=false` and
`MAINTENANCE_QUALIFY_RUNS=20260907T062915Z-f79f4de1` are already the container's
effective values, read from its own printout at 07:02Z. The pass opens no socket to
a provider, writes no Airtable record and sends no message.

What the capped scan already showed, and why it is not the answer: it examined the
first 200 retained rows and found 28 send-safe against 172 withheld
(`missing_email` 126, `outbound_company_held_for_review` 24,
`no_actionable_final_decision` 21, `apollo_email_not_verified` 1). **That population
is not the writer's 27.** It is a recomputation over a truncated slice of retained
rows, and the run's own delivery receipt is the only record of which 27 the writer
withheld.

## What this does not establish

No live improvement. This is an offline correction with an offline corpus: no run
has executed since, so the number of rows it releases in production is unmeasured.
It does not reach 1,000 Approved contacts per run and makes no claim about that
target. Acquisition stays paused, maintenance active, the Apollo grant zero, billing
unchanged, and the spent grant `luis-20260907-newjobs-2045-stage1` is neither reset
nor reused.
