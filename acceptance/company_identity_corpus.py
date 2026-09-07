"""One corpus of company identities, resolved by whichever resolver is importable.

Used two ways, and that is the point of keeping it in one place:

* ``python acceptance/company_identity_corpus.py`` prints the table -- run it against
  a checkout of the deployed resolver to get "before", against this branch to get
  "after", and diff the two;
* ``tests/test_company_identity_generalization.py`` imports :data:`CORPUS` and pins
  the after-state, so a later change cannot quietly re-hold a row that this work
  released, or release one it deliberately holds.

The first rows are the real 2026-09-07 production holds, with the anchors and
published names the run's own retained evidence recorded. Everything after them is
adversarial: shapes that LOOK like those and must not resolve. A corpus of only the
cases a change was built for measures nothing.

THE ATTESTATION COLUMN. ``linkedin_website`` is what the organization's own LinkedIn
page declares as its website -- a field the provider already returns. It is the
difference between "these two identifiers look related" and "the organization says
they are mine", and several rows appear twice, once with it and once without, because
the whole question is what happens in each case. ``Clark``/``clarkaudit``/
``getclark.com`` and ``Apple``/``applebank``/``apple.com`` are the SAME string
relation; only corroboration separates them.

No contact detail, no fingerprint and no provider payload is stored here -- only the
company name, the two identifiers and that declared website.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: ``(id, expectation, organization, org_linkedin_name, slug, domain,
#:   linkedin_website, note)``
#: ``expectation`` is what the row SHOULD do: ``resolve`` (one organization, evidence
#: available) or ``hold`` (no evidence, or evidence that these are different
#: organizations).
CORPUS = [
    # ---- the real 2026-09-07 records ------------------------------------------
    ("bsb", "resolve", "BS&B Safety Systems", "BS&B SAFETY SYSTEMS, LLC..",
     "bsbsafetysystems", "bsbsystems.com", "",
     "the domain drops an interior word of the company's own name; NOTHING in "
     "either identifier is left unexplained, so no corroboration is needed"),
    ("kai", "resolve", "Kai", "", "kaisecurity", "kai.security", "",
     "brand TLD: read whole, the domain IS the slug -- identical identifiers"),
    ("nash", "resolve", "Nash", "", "", "usenash.com", "",
     "one identifier only; there is nothing for it to disagree with"),
    ("zwicker", "resolve", "Zwicker & Associates, P.C.", "",
     "zwickerassociatespc", "zwickerpc.com", "",
     "the domain keeps the first and last words; no residue either side"),
    ("endeavor_attested", "resolve", "Endeavor Business Media", "EndeavorB2B",
     "endeavorb2b", "endeavorbusinessmedia.com", "https://endeavorbusinessmedia.com",
     "rebrand, corroborated by the organization's own declared website"),
    ("endeavor_bare", "hold", "Endeavor Business Media", "EndeavorB2B",
     "endeavorb2b", "endeavorbusinessmedia.com", "",
     "SAME rebrand with nothing corroborating it. Two names in one record can be an "
     "incorrect association -- a client named beside a staffing agency, a parent "
     "beside a subsidiary -- so the shape alone does not settle it"),
    ("minth", "hold", "Minth North America", "Minth North America, Inc.",
     "minthnorthamericainc", "minthgroup.com", "",
     "SUBSIDIARY on a parent group domain -- must stay separate"),
    # ---- the homonym, and the decorated brand it is indistinguishable from -----
    ("homonym_prefix", "hold", "Apple", "", "applebank", "apple.com", "",
     "THE FIX: `apple` inside `applebank` is the same relation as `clark` inside "
     "`clarkaudit`. Nothing in the record says which, so it holds"),
    ("homonym_counter_attested", "hold", "Apple", "", "applebank", "apple.com",
     "https://applebank.com",
     "the organization declares applebank.com, NOT apple.com -- an attestation that "
     "does not match the employer domain corroborates nothing"),
    ("vanity_get_attested", "resolve", "Clark", "", "clarkaudit", "getclark.com",
     "https://getclark.com",
     "a legitimate decorated domain, corroborated: the organization declares it"),
    ("vanity_get_bare", "hold", "Clark", "", "clarkaudit", "getclark.com", "",
     "the same brand with nothing corroborating it -- string-identical to the "
     "homonym above, so it must behave the same way"),
    ("vanity_my_attested", "resolve", "Carpe", "", "carpe1", "mycarpe.com",
     "https://mycarpe.com", "second decorated brand, corroborated"),
    ("bridged_attested", "resolve", "Blueground", "", "bluegroundco",
     "theblueground.com", "https://theblueground.com",
     "the name sits inside both identifiers AND the organization declares the "
     "domain; shared letters alone no longer resolve anything"),
    ("bridged_bare", "hold", "Blueground", "", "bluegroundco", "theblueground.com",
     "", "shared letters are not shared evidence"),
    # ---- shapes that must not resolve -----------------------------------------
    ("shared_ats", "hold", "Acme Corp", "", "", "applicantpro.com", "",
     "shared applicant-tracking host is not an employer identity"),
    ("shared_ats_2", "hold", "Beta Industries", "", "", "applicantpro.com", "",
     "a second employer on the same host must not inherit the first"),
    ("parent_brand", "hold", "Instagram", "", "instagram", "meta.com", "",
     "subsidiary brand on the parent's domain"),
    ("sibling_companies", "hold", "Northwind Logistics", "Northwind Capital",
     "northwindcapital", "northwindlogistics.com", "",
     "two names, each exactly corroborated by a different identifier, sharing a "
     "long leading token -- every shape test a rebrand passes, and they are two "
     "companies. Only corroboration separates this from endeavor_attested"),
    ("unrelated", "hold", "Acme", "", "globex", "globex.com", "",
     "the published name is built into neither identifier"),
    ("gov_portal", "hold", "Massachusetts Department of Developmental Services", "",
     "ddsmass", "mass.gov", "",
     "a government portal domain is shared by every agency on it"),
    ("acronym", "hold", "Resource Management Concepts", "",
     "resourcemanagementconceptsinc", "rmcweb.com", "",
     "an INITIALISM -- deliberately not derivable, three letters collide"),
    ("acronym_collision", "hold", "Hex Technologies", "", "hextechnologies",
     "hexagon.com", "", "a shared opening fragment is not a shared identity"),
    ("vanity_trap", "hold", "Resa", "", "", "theresa.com", "",
     "'the' is NOT a vanity prefix -- a domain-only record cannot read theresa.com "
     "as the company Resa, and there is no second identifier to corroborate it"),
    ("franchise", "hold", "Diamond Jo Casino & Hotel", "", "diamondjoworth",
     "boydgaming.com", "", "property under an operator's corporate domain"),
    ("malformed", "hold", "null, 12345", "", "acmeco", "acme.com", "",
     "a coded or malformed label is not a company name"),
    ("no_anchor", "hold", "Some Employer", "", "", "", "",
     "no stable identifier at all"),
    # ---- must resolve, and none of them among the reviewed five ----------------
    ("brand_tld_ai", "resolve", "Cursor", "", "cursorai", "cursor.ai", "",
     "brand TLD on a company never reviewed by hand"),
    ("dropped_word", "resolve", "Northwind Logistics Group", "",
     "northwindlogisticsgroup", "northwindgroup.com", "",
     "same shape as BS&B, a company that has never been seen"),
    ("legal_suffix", "resolve", "Hexcel Composites, Inc.", "", "hexcelcomposites",
     "hexcelcomposites.com", "", "trailing legal suffix only"),
    ("dropped_word_2", "resolve", "Pinewood Medical Devices", "",
     "pinewoodmedicaldevices", "pinewooddevices.com", "",
     "another unseen company whose domain drops an interior word"),
]

FIELDS = ("id", "expectation", "organization", "org_linkedin_name", "slug", "domain",
          "linkedin_website", "note")


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
        kwargs = dict(
            organization=record["organization"],
            org_linkedin_name=record["org_linkedin_name"],
            org_linkedin_slug=record["slug"],
            employer_domain=record["domain"],
            cache=cache, persist=False)
        # Older resolvers do not accept the attestation; the corpus must still run
        # against them so "before" is measurable on exactly these rows.
        try:
            result = resolve_company_display(
                org_linkedin_website=record["linkedin_website"], **kwargs)
        except TypeError:  # pragma: no cover - only on a resolver without the field
            result = resolve_company_display(**kwargs)
        evidence = result.evidence
        rows.append({
            **record,
            "hold": bool(result.hold),
            "confidence": result.confidence,
            "display_name": result.name,
            "reasons": list(evidence.get("reasons") or []),
            "manual_override": bool(evidence.get("manual_override")),
            "relation": (evidence.get("identifier_agreement") or {}).get("kind", ""),
            "missing_evidence": (evidence.get("missing_evidence") or {}).get("need", []),
            "meets_expectation": (result.hold is (record["expectation"] == "hold")),
        })
    return rows


def main() -> int:
    rows = resolve_corpus()
    met = sum(1 for r in rows if r["meets_expectation"])
    print(f"{'id':26} {'want':8} {'got':8} {'conf':7} {'relation':18} reasons")
    for row in rows:
        got = "hold" if row["hold"] else "resolve"
        flag = " " if row["meets_expectation"] else "X"
        print(f"{flag}{row['id']:25} {row['expectation']:8} {got:8} "
              f"{row['confidence']:7} {row['relation']:18} {','.join(row['reasons'])}")
    attested = [r["id"] for r in rows if r["linkedin_website"] and not r["hold"]]
    print(f"\n{met}/{len(rows)} rows meet their expectation")
    print(f"resolved by evidence      : {sum(1 for r in rows if not r['hold'])}")
    print(f"  of those, corroborated  : {len(attested)} {attested}")
    print(f"held and justified        : {sum(1 for r in rows if r['hold'])}")
    return 0 if met == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
