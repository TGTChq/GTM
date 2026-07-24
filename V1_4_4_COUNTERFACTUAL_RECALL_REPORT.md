# v1.4.4 counterfactual recall replay

## Captured real corpus

- Two archived production runs from July 22–23, 2026.
- 1,058 raw records; 983 unique postings after job-id deduplication.
- Zero live API calls.

## Result

- v1.4.3 prefilter survivors: **69 postings / 66 companies**.
- v1.4.4 prefilter survivors: **93 postings / 90 companies**.
- Recovered: **24 postings**.
- Lost: **0 postings**.

The recovered records are not FINAL_PASS. They still pass through CRM exclusion, Job Gate, Role Gate, Account Gate, Contact Gate, Email Gate, human approval, and pre-send source revalidation.

## What changed

- Ignores generic sentence starters such as `This is a...` as supposed company identities.
- Allows recent, active Greenhouse listings with unknown `first_published` into a review lane.
- Treats structured worldwide roles as US-inclusive unless an explicit foreign-only restriction exists.
- Moves structured provider identity conflicts to review instead of hard rejection.
- Restores a placeholder employer only from a separately verified ATS board company identity.

## Recovered postings

1. **Fuku — Machine Learning Engineer - Search, Ranking & Personalization - Full-time**  
   Previous reason: `description_employer_identity_conflict`
2. **Snowflake — Account Executive, Media & Entertainment**  
   Previous reason: `description_employer_identity_conflict`
3. **Dropbox — Account Executive UKI**  
   Previous reason: `description_employer_identity_conflict`
4. **T-Mobile — Account Executive, Business Team Sales- Central IL**  
   Previous reason: `description_employer_identity_conflict`
5. **Nectar Social — Event & Community Manager**  
   Previous reason: `description_employer_identity_conflict`
6. **TriHire Solutions — Financial System Administrator**  
   Previous reason: `description_employer_identity_conflict`
7. **Solomon Page — Accounts Receivable Specialist job at Solomon Page in Houston, TX**  
   Previous reason: `description_employer_identity_conflict`
8. **HireHawk — Apparel Graphic Designer / Design Assistant | United States**  
   Previous reason: `description_employer_identity_conflict`
9. **Mimecast — Account Executive, Mid-Market (North Central)**  
   Previous reason: `description_employer_identity_conflict`
10. **Playbook — Premium Creator Account Manager, Remote Job**  
   Previous reason: `description_employer_identity_conflict`
11. **Bisnow — Entry Level Business Development Representative job at Bisnow in Washington, DC, Philadelphia, PA**  
   Previous reason: `description_employer_identity_conflict`
12. **Illumio — Motion Designer (Brand & Campaigns)**  
   Previous reason: `description_employer_identity_conflict`
13. **My Amazon Guy — E-commerce Web Designer (CRO)**  
   Previous reason: `description_employer_identity_conflict`
14. **GuidePoint Security — Account Executive - Velocity (North Carolina)**  
   Previous reason: `description_employer_identity_conflict`
15. **JMAC Lending — Wholesale Account Executive, National**  
   Previous reason: `description_employer_identity_conflict`
16. **Quandary Consulting Group — Account Executive- AI and Automation**  
   Previous reason: `description_employer_identity_conflict`
17. **Hire Hangar — Paid Media Specialist – Digital Acquisition**  
   Previous reason: `description_employer_identity_conflict`
18. **PointClickCare — (US) Customer Success Manager - Health Plan (Great Lakes)**  
   Previous reason: `description_employer_identity_conflict`
19. **Element 84 — Senior DevOps Engineer (Hub-Remote: DC or Philly Metro)**  
   Previous reason: `description_employer_identity_conflict`
20. **Pinterest — Software Engineer II, Big Data, tvScientific**  
   Previous reason: `description_employer_identity_conflict`
21. **Sentinel — Enterprise Data Analyst**  
   Previous reason: `description_employer_identity_conflict`
22. **Sanity — Senior Manager, Paid Media, Remote Job**  
   Previous reason: `description_employer_identity_conflict`
23. **Pearl — Remote Executive Assistant (US Hours)**  
   Previous reason: `description_employer_identity_conflict`
24. **Anthropos — Account Executive US Market - Skills Assessment and AI Adoption (East Coast only)**  
   Previous reason: `description_employer_identity_conflict`

## Limitation

The exact 1,988-posting multi-source shadow raw archive was not included in the available files. The package therefore includes `run_counterfactual_recall_replay.py`, which can replay that archive offline with zero external calls and list every recovered or lost posting.

## Freeze recommendation

This release is the recall/precision stopping point for the current contract. No further broad loosening is supported by the evidence. The raw-to-prefilter ratio must not be used as an optimization target because the acquired inventory intentionally includes many postings outside the ICP.

Only reopen filtering code when production review identifies a repeated false-rejection or false-acceptance family with concrete examples. Otherwise, leave v1.4.4 unchanged and evaluate the system on FINAL_PASS yield, human approval rate, and downstream reply quality.
