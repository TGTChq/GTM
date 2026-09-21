"""Shadow A (6c79aa1) vs B (corroborated mail domain) on IDENTICAL, already-paid people.

Input: the company-level export of every person enriched in the 2026-09-21
production runs (no local parts, no names). Runs the REAL gate functions.

Conservative by construction: production's sibling set is every verified person
ever stored at the employer; this shadow only sees the day's 976, so B in
production accepts at least as many as reported here.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tgtc_core.domain.gates import corroborated_mail_domain, evaluate_email  # noqa: E402
from tgtc_core.domain.jurisdiction import observe_contact_country_with_provenance  # noqa: E402

rows = [json.loads(line) for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines() if line.strip()]
by_employer = defaultdict(list)
for r in rows:
    if r["employer_id"] and r["email_status"] == "verified" and r["email_domain"]:
        by_employer[r["employer_id"]].append(r)

a_approved = sum(1 for r in rows if r["a_approved"])
candidates = [r for r in rows if not r["a_approved"] and r["email_status"] == "verified"
              and r["employment_verified"] == "true" and r["a_alignment"] == ""]

accepted, reasons, employers, countries = [], Counter(), set(), Counter()
eligible_by_basis = Counter()
for r in candidates:
    person = {"organization": {"primary_domain": r["org_domain"], "website_url": r.get("org_website", ""),
                               "suborganizations": r.get("org_suborgs", [])},
              "employment_history": r["history"]}
    # Non-circular by construction: every sibling carries its A-state alignment,
    # and nothing B accepts in this pass is fed back as a seed.
    siblings = [{"email_domain": s["email_domain"], "alignment": s["a_alignment"], "org_domain": s["org_domain"]}
                for s in by_employer.get(r["employer_id"], []) if s["k"] != r["k"]]
    ok, why = corroborated_mail_domain(email=f"x@{r['email_domain']}", person=person,
                                       employer_domains={r["employer_domain"]},
                                       employer_name=r.get("employer_name", ""), siblings=siblings)
    reasons[why] += 1
    if not ok:
        continue
    gate = evaluate_email(email=f"x@{r['email_domain']}", email_status="verified",
                          employer_domains={r["employer_domain"]}, mail_domains={r["email_domain"]})
    assert gate.passed, gate
    accepted.append(r)
    employers.add(r["employer_id"])
    country, _ = observe_contact_country_with_provenance({"country": r["stored_country"], "state": r["stored_state"]})
    countries[country or "unknown"] += 1
    if country == "US":
        eligible_by_basis[why] += 1

print(f"people matched (credits paid): {len(rows)}")
print(f"A approved:                    {a_approved}")
print(f"A rejected on domain only:     {len(candidates)}")
print(f"B additionally accepted:       {len(accepted)} across {len(employers)} employers")
print(f"B approved total:              {a_approved + len(accepted)}")
print(f"approvals per credit:          A {a_approved / len(rows):.3f} -> B {(a_approved + len(accepted)) / len(rows):.3f}")
print("rule outcomes on the A-rejected:", dict(reasons))
print("B additions by resolved contact country:", dict(countries))
print("B US-eligible additions by evidence type:", dict(eligible_by_basis))
out = Path(sys.argv[2]) if len(sys.argv) > 2 else None
if out:
    out.write_text(chr(10).join(str(r["k"]) for r in accepted), encoding="utf-8")
