# Complete-flow volume audit — local release verified; production unfinished

Read alongside `COMPANY_HOLD_REVIEW_2026-09-07.md`, which contains the original
artifact's direct identity evidence. This audit adds future-run corrections.

Objective: at least 1,000 distinct new approved leads per production run, with a
separate daily union. No smaller ceiling, weakened qualification, budget increase,
paid experiment or production resumption is authorized by this audit. Production
publication remains blocked for this integration; independent local work continues.

Base on origin/main: ecfff20. Existing local corrections: 9126dbc (company identity,
display safety and custody duplicates). Read those findings separately; this audit
is not a claim that prior fixes or passing tests establish production volume.

Review coverage and findings are updated as executions prove them:

- Entry point -> lanes -> adapter queries/pages -> custody -> orchestrator dedupe.
- Historical suppression -> pending loader -> real precontact qualification.
- Employer/function grouping -> Apollo searches/cache/reroute -> contact/email gates.
- Real Airtable mapping/receipts -> identity suppression -> run/day target.
- Interruption, cleanup, reporting and deployment controls.

## Executed findings (local, not deployed)

All provider calls in these reproductions are deterministic substitutes. The real
decision, candidate ranking, cache, recovery and qualification paths execute with
`env -i` and `-p ci_no_network`. No credits or external delivery are used.

1. Candidate IDs were recorded as attempted BEFORE match funding was available.
   Zero match calls created one attempted ID and excluded it on the next run.
   Funding is now checked first; the later funded run can find that candidate.
2. A cached empty domain search also suppressed an organization-ID alternative
   that had never run. Negative keys now include available search alternatives
   and page depth; existing history is not cleared.
3. First-page organization-ID transport errors returned an empty list and were
   cached as absence. Errors now remain errors; the following run retries instead
   of inheriting a false no-match result.
4. Old fallback metadata applied fallback funding limits to current primary-domain
   results. Funding now follows the current search path.
5. Repeated paid pages and head-boundary tails escaped billing counters: two
   100-row responses reported 100 billed; a 100-row head response reported only
   21. Billing now counts the full response before exits, separately from cursor
   progress. Repeated response IDs do not move the cursor past unseen inventory.
6. Duplicate candidate IDs used both reroute slots: [bad, bad, good] matched bad
   twice. Ranking now suppresses repeated identities; the same two-slot allowance
   reaches [bad, good], preserving the actual contact gates.
7. Precontact REJECT rows never produced lead rows, so terminal recovery cleanup
   omitted them. A real qualification/recovery integration kept all 3 rejects in
   custody indefinitely. Explicit terminal posting IDs now retire these rejects
   without fabricating leads or retiring deferred decisions.
8. Corrupt suppression history loaded as empty and could be overwritten. Reads now
   stop before acquisition and preserve the damaged file for recovery.

9. Paid pages had no durable handoff before the next request. Both offset and
   sliced watermark paths now hold each paid page before persisting its cursor.
   Interruption after page one preserves 100 postings and resumes at offset 100.
10. A large fresh head advanced beyond an unseen middle. Twenty fresh jobs with
    an eight-row grant lost twelve indefinitely. A pinned head continuation now
    retrieves all twenty across bounded runs; no grant was raised.
11. A completed function disappeared when another function in the same company
    exhausted the provider budget. Exact-workload custody now preserves raw paid
    replies and completed function outcomes before continuing. Resume acquires
    only the missing reply. Nonterminal checkpoints are re-evaluated.
12. The Fantastic acquisition grant suppressed configured independent sources;
    free ATS rows also inflated its bill. Lane decisions and billing are separate.
    Healthy lane work survives another lane's failure, while the run stays failed.
13. Corrupt pending, approval and acquisition cursor files silently reset. They now
    fail closed and preserve the damaged evidence. Custody failure stops progress.
14. Non-Apollo enrichment interruption was ignored by topup and erased in its final
    report. The controller now stops once, preserves the remainder and reports
    INCOMPLETE rather than repeatedly replaying a partial batch.
15. People Search authorization errors and malformed responses became misses.
    Auth/circuit failures propagate; incomplete pages do not become negative cache.
16. Capacity invented paid calls from lead count and projected from incomparable
    runs. Missing calls/approval receipts remain unknown; no daily forecast is
    supplied without comparable cohorts. The target remains 1,000, not a ceiling.
17. Recovery counted multiple posting outcomes for the same employer/function as
    multiple opportunities. Numerator and denominator now use distinct reconciled
    employer/function identities; missing identities remain unknown. An outcome
    is explicitly not proof that an Apollo search occurred.

18. Monthly acquisition ledger compaction recalculated spend from only retained
    details: 350 paid rows became 250. Monotone per-run totals now survive detail
    compaction and smaller/incomplete final summaries. Each returned page commits
    spend after payload custody and before advancing or requesting another page.
    Corrupt spend state grants zero acquisition; independent work can still drain.
19. Retention still pruned when custody adoption/protection reads failed. Both
    failures now retain originals; two executed cleanup regressions pin this.
20. Historical backfill omitted the cursor on its FIRST request. The official
    contract then orders by date_posted DESC, but following pages with cursor use
    id ASC. A realistic 200-job feed retained only 100. Initial cursor=0 now keeps
    ID ordering throughout and retrieves all 200 without changing the budget.
    This capability remains disabled without its separate authorized row budget.
21. Paid replies stored only inside an exact run/workload did not survive later
    run cleanup or changed recovery metadata. Production wiring now uses private
    per-request files under provider_cache/paid_replies, outside run_artifacts.
    A replay removes the old run, changes metadata and reconstructs the contact
    with ONE total company acquisition and ONE total person match across both
    runs. Current gates still run. Positive evidence uses existing cache TTLs;
    unsuccessful evidence expires no later than the temporary reroute interval.
22. Negative-cache replays were labelled actual people searches. They now remain
    discovery entries but produce zero actual-search events. The regression checks
    the actual mocked search call count and the per-row diagnostic together.

## Verification and limitations

Every listed production-code change is LOCAL, not deployed. The full offline gate
before final review returned 3,432 passing tests / 1,001 subtests with one failure:
the fake delivery adapter lacked explicit approval receipts. Its offline response
now exposes the status actually requested; real approval acceptance stays strict.
Final complete gate: 3,437 passed / 1,001 subtests, 76.44 seconds, empty credentials
and ci_no_network. Undefined names: 0. Integrity: 28 checked / 0 mismatches /
0 absent. git diff --check clean. Original-row replay: unchanged SHA256, two
send-safe Approved mappings, 13 distinct corrected ATS employer groups. No paid
calls or external deliveries were performed.

The 1,000/1,200 throughput tests use controlled providers and receipts. They prove
batch continuation and counting, not 1,000 real approvals or provider coverage.
Production's original two-contact replay and shared-ATS employer identity tests
remain part of acceptance. Historical production capacity has no established lower
ceiling; ~2,000 remaining Apollo credits are user-reported, not a fresh balance.
An API request, a credit, a posting, an employer/function and an approved contact
are different units. In particular the old 10 paid calls/company and 8,300/day
floor are withdrawn; the old calibration counted free People API Search calls.

## Current official provider contracts checked 2026-09-07

- https://docs.apollo.io/reference/people-api-search — search consumes no credits;
  a domain match can include previous employment, so employer alignment remains.
- https://docs.apollo.io/reference/people-enrichment — enrichment billing varies
  with returned data/options. Do not equate a request with one credit or one lead.
- https://developer.fantastic.jobs/api/new-jobs — explicit cursor uses ID ascending,
  unlike default date_posted descending. Offset and cursor order must not be mixed.
  date_created and date_posted filters are different; requested windows stay pinned.

## Publication and remaining acceptance

Origin/main was fetched at ecfff20. Railway read-only status during final review:
GTM SUCCESS adc36f86-0212-4ea6-aaa1-4bb0b0ec69be, cron 0 3 * * *; Approved Sync
SUCCESS 90bef032-4113-46b4-b56c-6d795a0a6191, cron 0 0 * * *. Both unchanged.
Latest effective pause evidence remains maintenance1 / acquisition0 / Apollo grant0.
GitHub publishing already returned 403 Resource not accessible by integration.
Do not retry that denied integration or bypass it; the existing authorized git/gh
operator can apply the single reviewed release, run CI and deploy while paused.

After publication, bounded maintenance must verify the effective deployment and
real custody/report acceptance again. Known prior evidence: Sep6 recovery
226/226/226; Brett A/B accepted on nine ledger entries, not sent. Current physical
custody 5,595 rows is not a newly measured distinct denominator. Those production
acceptances cannot be replaced by the offline tests or inferred from log labels.

A later funded production run requires an explicit fresh durable grant, current
provider readiness and the established resumption prerequisites. It must reconcile
new Approved Airtable receipts to distinct contact identities and its exact run.
No purchase, budget increase, acquisition resumption or delivery is authorized by
this release. No projection, perfect-code claim or external-only loss claim is made.

## Exact stopping and capacity boundaries

The controlled `test_throughput_contract.py` runs demonstrate 1,000 distinct
approvals for THIS run despite 1,000 belonging to an earlier run; with continuation
authorization enabled, 1,200 inputs produce 1,200 controlled approvals. Pending batch
size 2,000 remains a processing slice, not a permanent daily or per-run output cap.

Enabled/default behavior is not production proof. Production has maintenance on,
so no pipeline executes. RUN_APPROVED_TARGET defaults to 1,000 and continuation
after target to true; the runtime flag remains part of deployment readiness.
Historical paid backfill, extra source lanes, org-ID fallback and broader title
search keep their existing activation and funding conditions. They are not claimed
as live. The new paid-reply custody is wired in the real entrypoint without a new
activation flag and remains under the selected state root, including offline modes.

The requested target needs a comparable production cohort and sufficient provider
credits/inventory. This patch removes demonstrated internal losses; it does not
prove all possible defects are absent or all remaining losses belong to Apollo.
Previous business-day counts (2,362–2,897 postings) are source/window observations,
not distinct daily approved contacts or a sustainable ceiling. No live yield or
credit cost per 1,000 approvals can be computed from the zero-approval calibration.
