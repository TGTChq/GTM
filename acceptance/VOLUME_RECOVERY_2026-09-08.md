# Approved-volume recovery: demonstrated defects, not a capacity forecast

Base: `c30178dde80f4620b4cb6a6ba6162d80b6a08e81`.
Production evidence: user-provided finalized log for
`20260908T030247Z-e711a350`, 2026-09-08 03:02:47-07:58:40 UTC.
This patch does not change deployed variables, billing, source selection, or
quality rules. It does not run a paid acquisition or enrichment experiment.

## 1. Acquisition defects reproduced offline

The date-slice grid was derived again from the moving feed horizon. A 37-second
change in the next cron's start shifted every six-hour slice key. Completed
ranges no longer matched, so the adapter bought their contents again and then
correctly suppressed the already-seen IDs. Deduplication was not the defect in
this reproduction; rebuilding requests for completed ranges was.

With the same moving-floor feed, existing state, and 180-row second-run budget:

| Measurement | Base | Patch |
| --- | ---: | ---: |
| Second-run billed rows | 180 | 180 |
| Adapter duplicates | 119 | 0 |
| Newly retained IDs | 61 | 180 |

The window now retains its slice origin, recovers the old grid from recorded
ranges/offsets when migrating, and recognizes completed coverage of a clipped
first slice. A stale slice or a stale drained flag cannot prove that the whole
current window drained. Existing custody-before-cursor persistence remains.

A separate defect renewed a source's allocation at every date slice. In the
regression, a source granted four rows consumed the entire 12-row run allowance.
The patched source consumes four, leaving eight available to other sources.
This respects the existing allocator; it adds no new spending ceiling.
The final stop reason now reports the actual budget stop rather than an earlier
slice's short page.

**Attribution limit:** these tests demonstrate concrete request-generation and
accounting defects, not that they explain all 4,505 duplicate events on September
8, or the historical 5,218 / 2,722 / 172 counts. Response-level historical IDs
were not provided. Genuine query/source overlap remains suppressed, including
in the new cross-source regression. No production dedupe state is cleared.

## 2. Contact and identity defects

- Domain normalization used an incomplete hand-written suffix list. Distinct
  employers such as `alpha.gov.in` and `beta.gov.in` collapsed to `gov.in`;
  `edu.ph` had the same problem. A pinned, bundled Public Suffix List now retains
  the registrable employer domain and rejects a bare public suffix. Its runtime
  HTTP fetch and writable cache are disabled. Existing intermediary/platform
  handling and domain validation remain. This cannot reconstruct a full hostname
  that an older run already discarded.
- With the alternate-candidate cascade already enabled and budget available,
  a verified email plus a contact-review outcome stopped at the first candidate.
  It now tries the next ranked candidate under the same gates and attempt caps.
  The regression changes one held candidate into a FINAL_PASS second candidate;
  if the second candidate is invalid, the original review remains. The patch does
  not enable the cascade or raise its budget.
- Trusted organization-ID recovery could discover a candidate but never enrich
  it: its legacy fallback-only allowance defaulted to zero even in authorized
  continuous production. An absent allowance now inherits continuous mode's
  normal authorization and durable call accounting. Explicit zero, positive
  fallback ceilings, overall ceilings, and bounded recovery still apply.
  This can make additional paid matches within already-authorized continuous
  operation; it is not a claim of free enrichment.
- A FINAL_PASS carrying an outbound-company hold was treated as a reusable final
  checkpoint. It is now re-evaluated. Decision fingerprints include domain and
  display-resolver versions plus relevant recovery policy. Paid reply custody is
  retained; already-safe final checkpoints remain reusable.

The contact tests exercise `process_company`, the strict candidate loop, email
and final-decision logic with mocked providers and selected account/contact gate
outcomes. They are not live Apollo responses or Airtable Approved receipts.

## 3. Source distinction and production limits

`fantastic_jobs_ats` is the paid Fantastic ATS feed. `ats` is the separate direct
board lane; it returned 4,609 postings in the supplied run. They are not two
names for the same acquisition. Neither lane is removed by this patch.

The supplied production arithmetic is:

- Paid Fantastic: 5,144 billed = 639 retained + 4,505 adapter duplicates.
- Combined lanes: 5,248 retained = 4,448 net new + 724 historical + 76 canonical
  duplicates. With 2,000 resumed postings, enrichment reviewed 6,448 postings.
- Contact coverage: 1,355 company-function buckets, 536 contacts found, 819 not
  found. The separate 903 rejection counter is not an interchangeable population.
- Email results: 536 contacts with email, 522 verified, 14 unverified. The 873
  UNVERIFIED decision rows cannot be relabeled as 873 failed email verifications.
- Delivery: 1,309 candidates = 448 created + 779 no-contact + 81 withheld + one
  company-function suppression. Created is not synonymous with Approved.
- Apollo refused with balance zero after 1,506 companies. Custody reported 6,815
  pending postings across five runs, not 6,815 distinct approval opportunities.

The 819 missing-contact buckets and 81 delivery holds still need row-level
attribution to quantify how many these fixes recover. Direct ATS zero-delivery
counts do not by themselves establish irrelevance: processing order, company
grouping, gates, and the interrupted portion need cohort-linked evidence.
Do not infer a credit-to-Approved conversion from request counts or forecast
1,000 Approved/day from an incomplete cohort. No low daily ceiling is established
either. The actual post-patch Approved gain remains unmeasured.

## 4. Verification

The same 29 new regression cases on a separate pristine `c30178d` worktree give
**17 failures and 12 passing controls**. This avoids reverting uncommitted work
and proves that the new assertions do not merely pass on both implementations.
Full-suite and integrity results are recorded in the pull request after the
final gate completes.

Local execution additionally denies IPv4/IPv6 sockets at the kernel boundary,
clears the environment and uses a fresh HOME, disables dotenv loading, and runs
the repository's `ci_no_network` plugin. This applies to subprocesses too.
No production access or paid request is needed to reproduce these regressions.
