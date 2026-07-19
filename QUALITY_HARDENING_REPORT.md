# TGTC definitive quality and controlled-volume hardening

## Scope

This patch starts from the deployed paid-test quality-recovery version. It does
not change Airtable schemas, Instantly campaigns, Apollo/Hunter credentials, or
production data.

## Root causes addressed

1. **Provider metadata was treated as truth.** Employment type, location, and
   employer labels are now corroborated against title, description, structured
   fields, and safe domains.
2. **Lexical title matching ignored context.** Context guards separate sales
   Account Executives from PR/communications work, customer Product Support from
   inventory/catalog operations, and video editing from static graphic design.
3. **Non-standard programs resembled normal full-time roles.** SkillBridge,
   externships, apprenticeships, fellowships, returnships, internships, and
   similar programs are rejected before enrichment without matching EEO
   boilerplate accidentally.
4. **Government/cleared delivery leaked through normal role titles.** Clearance
   requirements and explicit federal-agency/program delivery are now rejected as
   a family.
5. **Intermediaries consumed credits.** Staffing, BPO/outsourcing businesses,
   roundup pages, generic employers, ATS wrappers, and corroborated nonprofit
   organizations are handled before Apollo/Hunter.
6. **Geography parsing confused words with state abbreviations.** Bare `OK` and
   `PR` are never inferred as states. Explicit city/state phrases, structured
   United States locations, clear US-remote language, safe US URL evidence, and
   delimiter-bounded `- US` titles remain eligible.
7. **Uniform page depth spent quota on low-yield inventory.** The system keeps
   one page for all roles and reserves up to 16 units for a one-week, diversified
   lookback only when fewer than 60 jobs survive all zero-credit gates.

## Validation completed

- Python compilation: passed.
- Complete unit suite: **159 tests passed**.
- Offline replay: **1,356 postings**.
- External calls during replay: **0** to JSearch, Apollo, Hunter, Airtable, or
  Instantly.
- Current-code baseline: **55 kept / 1,301 rejected**.
- Patched result: **48 kept / 1,308 rejected**.
- Patched rejection families include 9 posting-integrity, 84 restricted-role,
  10 outsourcing, 1 contextual-role, 195 staffing, 86 excluded-industry, 25
  non-full-time, and 13 non-US decisions.

The patched 48 are pre-enrichment candidates, not guaranteed Airtable leads.
The controlled lookback targets 60 pre-enrichment candidates on low-yield live
days so that the downstream target of approximately 30 reviewable leads is
plausible without weakening the filters. Exact daily output cannot be guaranteed
from a single job source because it also depends on live inventory, company-size
qualification, buyer discovery, and email contactability.

## Production policy

- Keep `NUM_PAGES=1`.
- Keep `JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN=150`.
- Keep `TARGET_REVIEWABLE_LEADS_PER_RUN=30`.
- Keep the five new Instantly campaigns paused until one controlled post-deploy
  run has been reviewed.
- Do not run Approved Leads Sync during filter validation.
