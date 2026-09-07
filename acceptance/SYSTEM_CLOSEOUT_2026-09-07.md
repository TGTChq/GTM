# Whole-path review: what was corrected, what is explained, what is open

Base `3a759d1`. Nothing here is evidence of live yield: no run has executed since,
acquisition stays paused, the Apollo grant stays at zero and billing is unchanged.

## 1. Identity: weak evidence proposes, it no longer decides

`Apple`/`applebank`/`apple.com` and `Clark`/`clarkaudit`/`getclark.com` stand in the
**same string relation** — the published name opens the longer identifier and leaves
a word behind (`bank`, `audit`). No rule reading only those strings can clear one and
hold the other. The rule in place cleared both, and so cleared the homonym.

The resolver now names how the two identifiers relate and treats a **prefix** relation
as contradictory rather than benign:

| relation | meaning | treatment |
|---|---|---|
| `identical` | some spelling of each is the same string | nothing to resolve |
| `prefix` | one contains the other and continues | **contradictory** — needs corroboration |
| `conflict` | neither contains the other | contradictory — needs corroboration |
| `single_identifier` | only one present | nothing to disagree with |

Alongside it, each match reports whether the name accounts for an identifier
**completely** or leaves a residue. Exactly one thing still resolves a contradiction
without corroboration: **one published name that accounts for both identifiers
entirely** — `BS&B Safety Systems` explains every letter of `bsbsafetysystems` and
`bsbsystems`, so those are two spellings of one name rather than a claim about two
things. Everything weaker now requires the organization's own declared website:

* a prefix relation — Clark, Carpe and the Apple homonym alike;
* **two names in one record.** A record can carry an incorrect association: a client
  named beside a staffing agency, a parent beside a subsidiary, two siblings under a
  group. The rebrand shape no longer settles anything by itself;
* **a shared brand inside both identifiers.** Demoted to a candidate signal that is
  recorded and settles nothing. Shared letters are not shared evidence.

No rule compares two companies, and none consults a list of names. The corroboration
is a field the provider already returns, so it works on companies never seen.

### Corpus: 30/30, with attestation as a column

`acceptance/company_identity_corpus.py`, empty overrides file, nothing passes by
being named. **12 resolve, 18 held.** The pairs that matter appear twice:

| row | corroborated? | outcome |
|---|---|---|
| `homonym_prefix` Apple/applebank/apple.com | no | **held** |
| `homonym_counter_attested` (declares `applebank.com`) | wrong target | **held** |
| `vanity_get_bare` Clark/clarkaudit/getclark.com | no | **held** |
| `vanity_get_attested` (declares `getclark.com`) | yes | resolves, medium |
| `endeavor_bare` two names, nothing else | no | **held** |
| `endeavor_attested` | yes | resolves, medium |
| `sibling_companies` Northwind Logistics / Northwind Capital | no | **held** |

`sibling_companies` passes every shape test a rebrand passes. Only corroboration
separates it from `endeavor_attested`, which is the point.

Still held for structural reasons, unchanged: subsidiary on a parent group domain,
two employers on one applicant-tracking host, a government portal, a franchise under
an operator's domain, an initialism, a shared opening fragment, a malformed label.

## 2. Suppression cannot be poisoned by a weak identity

`Outbound Company Identity` is read **only from rows that are not held and not
low-confidence**, so an inferred identity cannot become a permanent merge between two
companies. Suppression is permanent in effect — a match stops a row being written and
nobody revisits it — so the bar to contribute to it is higher than the bar to display
a name.

Fixing that exposed a second defect. The job-side key builder passed the identity
**without** the confidence that gates it, so every INCOMING job silently lost its key
while stored rows kept theirs — a suppression bug that would never have shown up as
an error. Both sides now read the same fields through one helper.

Verified across the dimensions that matter: two domains under one organization match;
two employers on one ATS host still do not; a held row contributes nothing; the key
stays function-aware (Acme+Marketing never suppresses Acme+Sales); a second batch in
a later run with a different domain **and** a different label is caught, which the
legacy `name:` key could not do.

## 3. Losses, with their units

`orchestrator/loss_units.py`. The flat `loss_reasons` map invites exactly the wrong
arithmetic, and on the real 2026-09-07 record it is wrong three separate ways.

**It spans four units that are not addable:** postings (`REJECT_*`, 325), company ×
function opportunities (`hiring_manager_not_found`), delivery rows (`no_contact`,
`send_safe_withheld`, 171) and dispositions (`unverified`, `needs_check`).

**Two labels are one population:** `unverified` and `email_unverified` are the same
155; `not_icp` and `rejected` the same 19. Summing double-counts 174.

**Containment is not established.** The previous claim of 11 email-verification
failures subtracted 144 delivery rows from 155 dispositions without linked identities
or email outcomes. It is withdrawn. Even establishing containment would not establish
the remaining records' reason for being unverified.

**Delivery arithmetic closes:** 199 submitted = 28 created + 171 skips, remainder
**0**. This does not establish approval status or the correctness of any skip.

**Search observations retain their limits.** The summary counts 203 eligible lead
rows with a search diagnostic flag, 147 without a manager name and 56 with one.
The flag is set before a client call and may be replayed from a bucket checkpoint.
It does not prove physical requests or completed searches during this run. The
27 withheld writer rows belong to a separate delivery population. Nor do 199 writer
candidates establish 199 distinct opportunities with outcomes: the prior claim of
four interrupted opportunities from 203 minus 199 is withdrawn.

**A counter disagreement, surfaced not resolved.** `loss_reasons.hiring_manager_not_found`
says 169; the observability layer's `hm_not_found` says 147. They are computed from
the same rule over sets meant to be the same. A consumer taking the larger books 22
buckets as "nobody found" that the other counter says had somebody. Neither artifact
says which is right, so the decomposition reports the disagreement instead of
preferring one.

**Not established, and not attributed to Apollo or to budget:** whether a search that
returned nobody had a searchable domain, and whether any negative came from a cache
rather than a call.

## 4. Duplicates: the identity closes, the attribution stays open

For run `20260907T062915Z-f79f4de1`: `returned_billed 500 = unique_kept 328 +
duplicates 172`, exactly. This balances the counters but does not exclude repeated
purchases, discarded first sightings or duplicate accounting. Both pipeline-side
dedupe counters are zero, which locates the recorded loss at the adapter; the
underlying decisions require response-level evidence that was not retained.

`cross_source_duplicates: 0` means *not cross-source*; it does not mean *not
duplicated*. That counter fires only when `_first_seen` names a different source, and
`_first_seen` is written only for rows kept in this run, so a seeded id always
produces a duplicate with the counter at zero.

**Open, and deliberately not closed:** how many of the 172 came from
`window_acquired_ids` seeded at window reuse versus repeats inside the run; whether a
different cursor or cap would have avoided buying them; and the same split for the
5,218 of 2026-09-06 and the 2,722 ATS rows kept at zero. Production retains no
response-level id list — `postings.json` holds only kept rows and
`window_acquired_ids` only kept ids — so this needs evidence the runs did not keep.

## 5. Architecture: the batch is not the ceiling

Proved by running the **real orchestrator** offline with faked provider and Airtable
boundaries (`tests/test_run_capacity_offline.py`):

| what was run | result |
|---|---|
| 1,500 eligible, 250-row batch, target 1,000, continue-after | **1,500 distinct new Approved**, custody drained empty |
| 3,524 owed against a 2,000-row batch (production's numbers) | all 3,524 reached and approved in ONE run |
| 1,000 approved by a previous run | contributes **0**; this run still does its own 1,000 |
| Airtable creates rows but approves none | **0** approved, 1,500 rows created |
| every write fails | **0** approved |
| budget interruption after a delivered batch | the batch's 250 **are counted**; the rest stays in custody |
| the same leads recorded again | **0 added** |

The counted unit is a distinct NEW approved lead key read back from the store the run
wrote. Postings, contacts found, Pending rows, duplicates and previously approved
leads are excluded by construction.

**This is capacity of the code under simulated inputs. It is not commercial
performance, and it is not 1,000 Approved per day demonstrated.**

## 6. Cost units stay separate

A free lane's cost is **zero as a fact about how it works**, not a missing bill.
Only a paid provider that returned no total, or an unclassified source, is unknown.
Credits, physical requests, billed rows, postings, opportunities and contacts each
keep their own counter, and every rate names the population of its numerator and
denominator.

## Pending, concrete

1. **The 27 withheld contacts are still not individually explained.** The scan is
   uncapped and the original writer receipts are exposed, but reading them needs a
   maintenance pass, and the temporary cron change that triggers one is refused in
   this session. No permitted path reaches a stopped cron container. Required:
   GTM service `3a41d0d7-cd66-4f53-baa6-886266ddbbed`, environment
   `bae427bd-64a6-4f4e-8f56-fbd406985434`, set `cronSchedule` a few minutes ahead, do
   not push during the window, read
   `railway logs <deployment-current-at-capture> -n 5000`, restore `0 3 * * *`.
2. **The 169/147 disagreement** needs per-bucket evidence a run must retain.
3. **The duplicate attribution** needs response-level ids no run currently keeps.
4. **No live measurement of any of this.** The corpus corrections are test evidence:
   six holds cleared in a corpus is not six contacts released in production.

## Production state

Read from the container's own effective-flags printout on deployment
`243827b0-dcca-48c7-819b-ce28fd613692` at **2026-09-07T07:02Z** —
`FANTASTIC_JOBS_ENABLED=false`, `MAINTENANCE_ONLY=true`,
`APOLLO_RECOVERY_BUDGET_CALLS=0`. **That is a reading of that moment, not of the
current deployment**; a later change would not be visible in it, and no printout has
been produced since. The budget counter is keyed by authorization id and already
records 200 consumed under `luis-20260907-newjobs-2045-stage1`, so any new grant
needs a NEW id — raising the call count under the spent one would resume it.
