# READY v1.4.4 — Counterfactual Recall Recovery

This cumulative release supersedes the unshipped v1.4.3 package and applies directly to the merged v1.4.1 base.

## Controlled recall changes

1. **Greenhouse active/unknown age review**
   - A direct, identity-verified Greenhouse posting with no `first_published` date may enter review only when its official board record was updated within the 30-day recovery window.
   - Expired, old, unverified, malformed, or non-direct records still reject.

2. **Structured worldwide roles**
   - `Anywhere in the World` or equivalent worldwide scope is treated as including the US only for structured public-feed or ATS records.
   - Explicit foreign-only restrictions still reject first.
   - Unstructured JSearch/aggregator records do not receive this recovery.

3. **Structured employer-identity conflicts**
   - A trusted structured provider record with a description/company mismatch moves to review instead of being discarded.
   - Unstructured syndicated conflicts remain hard rejects.

4. **ATS canonical identity restoration**
   - Placeholder employer names can be repaired only from a separately retained, verified ATS board company identity.
   - Unverified board values cannot repair identity.

5. **Generic sentence-starter false conflicts**
   - Pronouns and phrases such as `This`, `This role`, and `The position` can no longer be interpreted as company names.

## Safety boundary

Recovered records do not bypass CRM exclusion, Job Gate, Role Gate, Account Gate, Contact Gate, Email Gate, human approval, or pre-send source revalidation. Review-lane records are explicitly marked in the shadow report.

## Evidence

- Full repository suite: 457 tests plus 175 catalog subtests.
- Focused v1.4.4 suite: 15 tests.
- Captured real-corpus comparison: 1,058 raw / 983 unique postings.
- v1.4.3 survivors: 69 postings / 66 companies.
- v1.4.4 survivors: 93 postings / 90 companies.
- Recovered: 24 postings.
- Lost: 0 postings.
- Synthetic counterfactual: 3 intended recoveries, 0 losses, explicit foreign-only case remained rejected.
- Live external calls during validation: 0.

The exact 1,988-posting shadow raw archive was not present in the supplied artifacts. `run_counterfactual_recall_replay.py` performs that comparison offline against the archive stored in Railway and lists every recovered/lost posting.

## Release freeze decision

After this release, do not tune the pipeline merely because the raw-to-prefilter ratio looks low. Raw inventory includes stale, foreign-only, staffing, aggregator, non-full-time, restricted, physical, and role-mismatched postings, so that ratio is not a quality target.

Freeze the code at v1.4.4 unless a later controlled production review shows a repeated, auditable failure family across multiple real postings. A single unusual listing or an external provider/API format change is handled operationally first; it does not justify another broad filter rewrite.

Recommended operating mode:

- Run production into Airtable review.
- Keep Instantly enrollment behind explicit approval and pre-send source revalidation.
- Judge success by valid FINAL_PASS yield and reviewer acceptance rate, not by the percentage of raw postings retained.
