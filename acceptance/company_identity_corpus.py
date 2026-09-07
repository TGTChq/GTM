"""One corpus of company identities, resolved by whichever resolver is importable.

Used two ways, and that is the point of keeping it in one place:

* ``python acceptance/company_identity_corpus.py`` prints the BEFORE/AFTER table --
  run it against a checkout of the deployed resolver to get "before", against this
  branch to get "after", and diff the two;
* ``tests/test_company_identity_generalization.py`` imports :data:`CORPUS` and pins
  the after-state, so a later change cannot quietly re-hold a row that this work
  released, or release one it deliberately holds.

The first six rows are the real 2026-09-07 production holds, with the anchors and
published names the run's own retained evidence recorded. Everything after them is
adversarial: shapes that LOOK like those six and must not resolve. A corpus of only
the cases a change was built for measures nothing.

No contact detail, no fingerprint and no provider payload is stored here -- only the
company name and the two identifiers, which is all the resolver reads.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: ``(id, expectation, organization, org_linkedin_name, slug, domain, note)``
#: ``expectation`` is what the row SHOULD do once identity is handled generally:
#: ``resolve`` (one organization, evidence available) or ``hold`` (no evidence, or
#: evidence that these are different organizations).
#:
#: A row whose ``note`` opens with ``KNOWN GAP`` is a case this work does NOT solve
#: and does not claim to. Its expectation records what the system actually does, so
#: the corpus never reports a limitation as a pass by quietly expecting the wrong
#: thing -- and never buries it among the real failures either.
CORPUS = [
    # ---- the six real 2026-09-07 records --------------------------------------
    ("endeavor", "resolve", "Endeavor Business Media", "EndeavorB2B",
     "endeavorb2b", "endeavorbusinessmedia.com",
     "rebrand: each published name is corroborated by a different identifier"),
    ("bsb", "resolve", "BS&B Safety Systems", "BS&B SAFETY SYSTEMS, LLC..",
     "bsbsafetysystems", "bsbsystems.com",
     "domain drops an interior word of the company's own name"),
    ("kai", "resolve", "Kai", "", "kaisecurity", "kai.security",
     "brand TLD: the whole domain spells the slug"),
    ("nash", "resolve", "Nash", "", "", "usenash.com",
     "registrar vanity prefix on a domain-only record"),
    ("zwicker", "resolve", "Zwicker & Associates, P.C.", "",
     "zwickerassociatespc", "zwickerpc.com",
     "domain keeps the first and last words of the name"),
    ("minth", "hold", "Minth North America", "Minth North America, Inc.",
     "minthnorthamericainc", "minthgroup.com",
     "SUBSIDIARY on a parent group domain -- must stay separate"),
    # ---- shapes that must not resolve -----------------------------------------
    ("shared_ats", "hold", "Acme Corp", "", "", "applicantpro.com",
     "shared applicant-tracking host is not an employer identity"),
    ("shared_ats_2", "hold", "Beta Industries", "", "", "applicantpro.com",
     "a second employer on the same host must not inherit the first"),
    ("parent_brand", "hold", "Instagram", "", "instagram", "meta.com",
     "subsidiary brand on the parent's domain"),
    ("unrelated", "hold", "Acme", "", "globex", "globex.com",
     "the published name is built into neither identifier"),
    ("gov_portal", "hold", "Massachusetts Department of Developmental Services", "",
     "ddsmass", "mass.gov",
     "a government portal domain is shared by every agency on it"),
    ("acronym", "hold", "Resource Management Concepts", "",
     "resourcemanagementconceptsinc", "rmcweb.com",
     "an INITIALISM -- deliberately not derivable, three letters collide"),
    ("acronym_collision", "hold", "Hex Technologies", "", "hextechnologies",
     "hexagon.com", "a shared opening fragment is not a shared identity"),
    ("vanity_trap", "hold", "Resa", "", "", "theresa.com",
     "'the' is NOT a vanity prefix -- a domain-only record cannot read theresa.com "
     "as the company Resa, and there is no second identifier to corroborate it"),
    ("homonym_prefix", "resolve", "Apple", "", "applebank", "apple.com",
     "KNOWN GAP, unchanged by this work: the pre-existing prefix tier reads a name "
     "that OPENS a longer slug as the same brand decorated, which is what lets "
     "clark/clarkaudit through. It cannot tell that apart from a homonym whose "
     "brand happens to open another organization's slug. Verified to behave "
     "identically on the deployed resolver, so it is a limitation to state rather "
     "than a regression to fix here; narrowing it would re-hold clark and carpe."),
    ("franchise", "hold", "Diamond Jo Casino & Hotel", "", "diamondjoworth",
     "boydgaming.com", "property under an operator's corporate domain"),
    ("malformed", "hold", "null, 12345", "", "acmeco", "acme.com",
     "a coded or malformed label is not a company name"),
    ("no_anchor", "hold", "Some Employer", "", "", "",
     "no stable identifier at all"),
    # ---- shapes that must resolve, none of them among the reviewed five --------
    ("vanity_get", "resolve", "Clark", "", "clarkaudit", "getclark.com",
     "registrar vanity prefix plus a decorated slug"),
    ("brand_tld_ai", "resolve", "Cursor", "", "cursorai", "cursor.ai",
     "brand TLD on a company never reviewed by hand"),
    ("dropped_word", "resolve", "Northwind Logistics Group", "",
     "northwindlogisticsgroup", "northwindgroup.com",
     "same shape as BS&B, a company that has never been seen"),
    ("legal_suffix", "resolve", "Hexcel Composites, Inc.", "", "hexcelcomposites",
     "hexcelcomposites.com", "trailing legal suffix only"),
]

FIELDS = ("id", "expectation", "organization", "org_linkedin_name", "slug", "domain", "note")


def resolve_corpus(overrides_path=None):
    """Resolve every row with a fresh cache and NO manual entries by default."""
    import tempfile
    from company_display_resolver import CompanyDisplayCache, resolve_company_display

    tmp = Path(tempfile.mkdtemp())
    empty = tmp / "no_overrides.json"
    empty.write_text(json.dumps({"entries": {}, "aliases": {}}), encoding="utf-8")
    rows = []
    for index, row in enumerate(CORPUS):
        record = dict(zip(FIELDS, row))
        cache = CompanyDisplayCache(tmp / f"cache_{index}.json",
                                    overrides_path=overrides_path or empty)
        result = resolve_company_display(
            organization=record["organization"],
            org_linkedin_name=record["org_linkedin_name"],
            org_linkedin_slug=record["slug"],
            employer_domain=record["domain"],
            cache=cache, persist=False)
        evidence = result.evidence
        rows.append({
            **record,
            "hold": bool(result.hold),
            "confidence": result.confidence,
            "display_name": result.name,
            "reasons": list(evidence.get("reasons") or []),
            "manual_override": bool(evidence.get("manual_override")),
            "missing_evidence": (evidence.get("missing_evidence") or {}).get("need", []),
            "meets_expectation": (result.hold is (record["expectation"] == "hold")),
        })
    return rows


def main() -> int:
    rows = resolve_corpus()
    met = sum(1 for r in rows if r["meets_expectation"])
    print(f"{'id':20} {'want':8} {'got':8} {'conf':7} reasons")
    for row in rows:
        got = "hold" if row["hold"] else "resolve"
        flag = " " if row["meets_expectation"] else "X"
        print(f"{flag}{row['id']:19} {row['expectation']:8} {got:8} "
              f"{row['confidence']:7} {','.join(row['reasons'])}")
    print(f"\n{met}/{len(rows)} rows meet their expectation")
    gaps = [r["id"] for r in rows if r["note"].startswith("KNOWN GAP")]
    print(f"resolved by evidence : {sum(1 for r in rows if not r['hold'])}")
    print(f"held and justified   : {sum(1 for r in rows if r['hold'])}")
    print(f"known gaps stated    : {len(gaps)} {gaps}")
    print(json.dumps({"rows": rows}, indent=2, default=str),
          file=open(Path(__file__).with_name("company_identity_corpus_result.json"),
                    "w", encoding="utf-8"))
    return 0 if met == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
