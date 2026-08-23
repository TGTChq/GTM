"""Function-aware acquisition: role-family partition, activity ontology and the
slug crosswalk that lets company x function saturation move UPSTREAM of billing.

Three cooperating pieces, all DEFAULT OFF:

1. ROLE-FAMILY PARTITION (Part 3B) -- every acquisition title clause belongs to
   exactly ONE family. Fantastic bills per returned row *before* our local
   ``seen_ids`` dedupe, so if one job could match two paid family queries we would
   pay twice. ``validate_partition()`` makes that structurally impossible.

2. SLUG CROSSWALK (Part 3A) -- a durable, non-Airtable artifact mapping
   ``organization_slug <-> employer domain <-> company x function``. The provider
   exclusion key is the LinkedIn slug (present on 100% of billed rows) while our
   existing dedupe identity is domain/name-keyed, so the bridge must be persisted
   at acquisition time. Carries provenance, observed_at, a TTL and rebrand/drift
   handling.

3. ACTIVITY ONTOLOGY (Part 4) -- functional clusters ANCHORED IN OUTCOMES, derived
   from ``ai_key_skills`` (the only activity signal the Direct API returns; job
   descriptions are absent from 1,696/1,696 historical rows). Each cluster records
   its measured billed/FINAL_PASS evidence so nothing here is intuition-only.

IMPORTANT PROVIDER REALITY: ``ai_key_skills`` is a RETURNED field, not a queryable
one. Activity intent can only be pushed server-side through ``title_advanced``,
``description_advanced``, ``ai_taxonomies_a[_primary]`` / ``exclude_ai_taxonomies_a``,
``seniority`` and ``ai_work_arrangement``. The ontology therefore informs which
*titles/taxonomies* we buy -- it is never a post-billing description filter.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

CROSSWALK_SCHEMA = "fantastic-slug-crosswalk/1"

# --------------------------------------------------------------------------
# 1. ROLE-FAMILY PARTITION
# --------------------------------------------------------------------------
# Families are the paid acquisition units. Keyword rules are evaluated IN ORDER and
# the FIRST match wins, which guarantees a partition (never an overlap) no matter
# how the catalog grows. `validate_partition()` proves the invariant at runtime.
FAMILY_GTM = "gtm_revenue"
FAMILY_MARKETING = "marketing_growth"
FAMILY_CS = "customer_success_support"
FAMILY_ENGINEERING = "engineering_ai_automation"
FAMILY_FINOPS = "finance_operations"
FAMILY_PRODUCT = "product_design"
FAMILY_PEOPLE = "people_hr"

FAMILY_ORDER: Tuple[str, ...] = (
    FAMILY_GTM, FAMILY_MARKETING, FAMILY_CS, FAMILY_ENGINEERING,
    FAMILY_FINOPS, FAMILY_PRODUCT, FAMILY_PEOPLE,
)

# (family, ordered keyword rules). First rule that matches a lower-cased role name
# assigns the family. Order matters: more specific phrases precede generic tokens.
_FAMILY_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (FAMILY_CS, ("customer success", "customer support", "customer experience",
                 "customer onboarding", "customer retention", "customer operations",
                 "technical support", "product support", "implementation specialist",
                 "community manager", "support")),
    (FAMILY_GTM, ("account executive", "account manager", "business development",
                  "sales development", "inside sales", "sales operations",
                  "revenue operations", "sales enablement", "partnerships",
                  "lead generation", "deal desk", "gtm", "sales", "crm administrator")),
    (FAMILY_MARKETING, ("marketing", "seo", "paid media", "ppc", "social media",
                        "copywriter", "content writer", "brand manager", "lifecycle",
                        "crm marketing", "demand", "growth", "podcast")),
    (FAMILY_ENGINEERING, ("software engineer", "frontend", "backend", "full stack",
                          "cloud engineer", "devops", "qa engineer", "qa analyst",
                          "data engineer", "data analyst", "data scientist",
                          "business intelligence", "systems administrator",
                          "database administrator", "ai engineer", "machine learning",
                          "prompt engineer", "ai operations", "automation specialist",
                          "conversational ai", "ai automation", "ai content",
                          "data labeling", "data annotator", "chatbot", "engineer")),
    (FAMILY_FINOPS, ("accountant", "bookkeeper", "payroll", "financial analyst",
                     "fp&a", "billing", "collections", "accounts payable",
                     "accounts receivable", "ap specialist", "ar specialist",
                     "accounting", "operations analyst", "business operations",
                     "executive assistant", "administrative assistant",
                     "virtual assistant", "project coordinator", "data entry",
                     "e-commerce", "amazon", "shopify", "catalog", "listings")),
    (FAMILY_PRODUCT, ("product manager", "product analyst", "product designer",
                      "technical writer", "ux", "ui", "web designer", "motion designer",
                      "video editor", "video producer", "graphic designer", "designer")),
    (FAMILY_PEOPLE, ("recruiter", "talent acquisition", "people operations",
                     "hr ", "hr generalist", "hr analyst", "hr administrator",
                     "hr operations", "benefits", "compensation",
                     "learning & development", "recruiting coordinator")),
)

_DEFAULT_FAMILY = FAMILY_FINOPS  # catch-all; validate_partition surfaces the count


def family_for_role(role: str) -> str:
    """Deterministic family for one catalog role. First matching rule wins.

    Matching is WORD-BOUNDARY based, never bare substring: a short token like
    ``ui`` would otherwise match inside "rec-ui-ter" and mis-file Recruiter under
    product_design. Punctuation (``/``, ``&``, ``-``) is normalised to spaces so
    "UX/UI Designer" and "FP&A" tokenise correctly.
    """
    name = " ".join(re.sub(r"[^a-z0-9&]+", " ", str(role or "").lower()).split())
    if not name:
        return _DEFAULT_FAMILY
    for family, keys in _FAMILY_RULES:
        for k in keys:
            key = " ".join(re.sub(r"[^a-z0-9&]+", " ", k.lower()).split())
            if not key:
                continue
            if re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", name):
                return family
    return _DEFAULT_FAMILY


def partition_roles(roles: Iterable[str]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {f: [] for f in FAMILY_ORDER}
    for r in roles:
        out[family_for_role(r)].append(str(r))
    return out


def validate_partition(roles: Sequence[str]) -> Dict[str, Any]:
    """PROVE the no-double-billing invariant: every role maps to exactly one family
    and no canonical title clause is shared between families. Raises on violation."""
    assign: Dict[str, str] = {}
    dupes: List[str] = []
    for r in roles:
        key = " ".join(str(r or "").lower().split())
        fam = family_for_role(r)
        if key in assign and assign[key] != fam:
            dupes.append(key)
        assign[key] = fam
    if dupes:
        raise ValueError(f"role clause assigned to multiple families: {sorted(set(dupes))}")
    counts = {f: 0 for f in FAMILY_ORDER}
    for fam in assign.values():
        counts[fam] += 1
    if sum(counts.values()) != len(assign):
        raise ValueError("partition lost a role")
    return {"families": counts, "roles": len(assign), "overlaps": 0}


# --------------------------------------------------------------------------
# 2. SLUG CROSSWALK
# --------------------------------------------------------------------------
@dataclass
class CrosswalkEntry:
    slug: str
    domain: str = ""
    company: str = ""
    buckets: List[str] = field(default_factory=list)   # functions seen for this org
    source: str = "fantastic_jobs"
    observed_at: str = ""
    version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SlugCrosswalk:
    """Durable ``slug <-> domain <-> company x function`` map.

    Deliberately a STATE ARTIFACT, not an Airtable field: it is pipeline plumbing,
    not business data, and Airtable schema changes are avoided. Entries carry
    provenance + observed_at and expire via TTL so a rebrand/slug drift self-heals
    (a stale slug simply ages out and the new one is learned on the next sighting).
    """

    def __init__(self, path: str, *, ttl_days: int = 120, now: Optional[datetime] = None) -> None:
        self.path = str(path or "")
        self.ttl_days = int(ttl_days)
        self._now = now
        self.state: Dict[str, Any] = self._load()
        self._dirty = False

    def now(self) -> datetime:
        return self._now or datetime.now(timezone.utc)

    def _load(self) -> Dict[str, Any]:
        if not self.path:
            return {"schema": CROSSWALK_SCHEMA, "by_slug": {}}
        try:
            with open(self.path, encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d, dict) and d.get("schema") == CROSSWALK_SCHEMA:
                d.setdefault("by_slug", {})
                return d
        except (OSError, ValueError):
            pass
        return {"schema": CROSSWALK_SCHEMA, "by_slug": {}}

    def save(self) -> None:
        if not self.path or not self._dirty:
            return
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            self.state["updated_at"] = self.now().isoformat()
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh)
            os.replace(tmp, self.path)
            self._dirty = False
        except OSError:
            pass  # crosswalk is best-effort; never fails a run

    # -- learning -----------------------------------------------------------
    def observe(self, *, slug: str, domain: str = "", company: str = "", bucket: str = "") -> None:
        slug = str(slug or "").strip().lower()
        if not slug:
            return
        e = self.state["by_slug"].get(slug) or CrosswalkEntry(slug=slug).to_dict()
        if domain:
            e["domain"] = str(domain).strip().lower()
        if company:
            e["company"] = str(company).strip()
        if bucket:
            b = str(bucket).strip().lower()
            if b and b not in e["buckets"]:
                e["buckets"].append(b)
        e["observed_at"] = self.now().isoformat()
        self.state["by_slug"][slug] = e
        self._dirty = True

    def observe_jobs(self, jobs: Iterable[Dict[str, Any]]) -> int:
        n = 0
        for j in jobs:
            slug = j.get("org_linkedin_slug")
            if not slug:
                continue
            self.observe(slug=slug, domain=str(j.get("employer_website") or ""),
                         company=str(j.get("employer_name") or ""),
                         bucket=str(j.get("_role_bucket") or ""))
            n += 1
        return n

    # -- lookup -------------------------------------------------------------
    def _fresh(self, entry: Dict[str, Any]) -> bool:
        try:
            seen = datetime.fromisoformat(str(entry.get("observed_at")))
        except (ValueError, TypeError):
            return False
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        return self.now() - seen <= timedelta(days=self.ttl_days)

    def slugs_for_domains(self, domains: Iterable[str]) -> List[str]:
        want = {str(d or "").strip().lower() for d in domains if str(d or "").strip()}
        out = []
        for slug, e in self.state["by_slug"].items():
            if e.get("domain", "").lower() in want and self._fresh(e):
                out.append(slug)
        return sorted(set(out))

    def to_dict(self) -> Dict[str, Any]:
        return {"entries": len(self.state.get("by_slug") or {}), "path": self.path,
                "ttl_days": self.ttl_days}


# --------------------------------------------------------------------------
# 3. COVERED-SLUG EXCLUSION (chunked)
# --------------------------------------------------------------------------
def covered_slugs_for_family(crosswalk: SlugCrosswalk, covered_function_keys: Iterable[str],
                             family: str, bucket_to_family=family_for_role) -> List[str]:
    """Slugs already actively covered FOR THIS FUNCTION only.

    ``covered_function_keys`` are the existing ``domain:x|bucket:y`` /
    ``name:x|bucket:y`` keys from ``snapshot_existing_identity`` -- i.e. ACTIVE
    Airtable rows only, so Rejected/Error companies stay acquirable. A company
    covered for GTM is excluded from the GTM query ONLY; its Engineering demand
    remains fully eligible.
    """
    domains: Set[str] = set()
    for key in covered_function_keys or []:
        k = str(key)
        if "|bucket:" not in k or not k.startswith("domain:"):
            continue
        dom, bucket = k[len("domain:"):].split("|bucket:", 1)
        if bucket_to_family(bucket) == family and dom:
            domains.add(dom.lower())
    return crosswalk.slugs_for_domains(domains)


def chunk_slugs(slugs: Sequence[str], chunk_size: int = 250,
                max_url_chars: int = 12000) -> List[List[str]]:
    """Split a covered-slug list into request-safe chunks.

    250 is the operational default: a live probe honored 500 slugs at a 13.6 KB URL,
    which is uncomfortably close to common ~16 KB server ceilings, so we keep a
    wide margin. ``max_url_chars`` additionally caps by encoded length so unusually
    long slugs cannot silently blow the limit.
    """
    out: List[List[str]] = []
    cur: List[str] = []
    cur_len = 0
    for s in slugs:
        add = len(s) + 1
        if cur and (len(cur) >= chunk_size or cur_len + add > max_url_chars):
            out.append(cur)
            cur, cur_len = [], 0
        cur.append(s)
        cur_len += add
    if cur:
        out.append(cur)
    return out


# --------------------------------------------------------------------------
# 4. ACTIVITY ONTOLOGY (evidence-anchored)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ActivityCluster:
    """One functional/activity cluster. ``billed``/``final_pass`` are the MEASURED
    historical counts for the anchor skills (1,696-posting corpus, baseline
    17.39% FINAL_PASS per billed job) -- evidence, not intuition."""
    id: str
    family: str
    activities: Tuple[str, ...]
    anchor_skills: Tuple[str, ...]
    billed: int
    final_pass: int
    note: str = ""

    @property
    def yield_rate(self) -> float:
        return (self.final_pass / self.billed) if self.billed else 0.0


# Anchors measured from ai_key_skills x _final_state on the reprocess corpus.
ACTIVITY_CLUSTERS: Tuple[ActivityCluster, ...] = (
    ActivityCluster("revenue_systems", FAMILY_GTM,
                    ("CRM operations", "sales automation", "lead routing",
                     "pipeline/forecast systems", "outbound infrastructure"),
                    ("crm management", "salesforce", "hubspot", "forecasting",
                     "crm proficiency", "customer relationship management"),
                    billed=85 + 70 + 36 + 72, final_pass=29 + 27 + 15 + 27,
                    note="Strongest measured cluster; every anchor >30% vs 17.4% baseline."),
    ActivityCluster("sales_execution", FAMILY_GTM,
                    ("pipeline generation", "qualification", "enterprise selling"),
                    ("lead qualification", "pipeline generation", "outbound prospecting",
                     "enterprise sales", "saas sales", "objection handling"),
                    billed=36 + 31 + 49 + 43 + 35 + 22, final_pass=13 + 10 + 15 + 14 + 11 + 9),
    ActivityCluster("financial_systems", FAMILY_FINOPS,
                    ("financial modelling", "ERP/NetSuite operations", "variance analysis"),
                    ("netsuite", "financial modeling", "variance analysis"),
                    billed=18 + 21 + 39, final_pass=9 + 10 + 14,
                    note="High yield but small n; ERP-adjacent finance, not clerical AP/AR."),
    # --- measured NEGATIVE clusters: real work, but near-zero TGTC fit ---------
    ActivityCluster("recruiting_ops", FAMILY_PEOPLE,
                    ("ATS administration", "interview coordination", "talent sourcing"),
                    ("ats management", "interview coordination", "talent sourcing",
                     "candidate qualification", "offer management", "outbound sourcing"),
                    billed=31 + 41 + 29 + 25 + 30 + 33, final_pass=0 + 1 + 0 + 0 + 0 + 0,
                    note="NEGATIVE cluster: 2/189 FINAL_PASS. Recruiting ops is not TGTC-serviceable."),
    ActivityCluster("clerical_finance", FAMILY_FINOPS,
                    ("bookkeeping", "invoice/AP processing", "payroll runs"),
                    ("bookkeeping", "invoice processing", "payroll processing",
                     "billing", "financial records"),
                    billed=38 + 27 + 34 + 31 + 25, final_pass=1 + 0 + 1 + 1 + 0,
                    note="NEGATIVE cluster: 3/155 FINAL_PASS. Clerical, not systems work."),
    ActivityCluster("frontend_stack", FAMILY_ENGINEERING,
                    ("frontend framework delivery",),
                    ("angular", "react.js", "elasticsearch"),
                    billed=51 + 22 + 21, final_pass=1 + 0 + 0,
                    note="NEGATIVE cluster: 1/94 FINAL_PASS. Pure product-eng, not automation."),
)

# Adjacent titles that perform HIGH-YIELD cluster work but are absent from the
# 118-term catalog. UNPROVEN until count-probed + outcome-measured; every entry is
# scoped to one family so the partition invariant holds.
ADJACENT_TITLE_CANDIDATES: Dict[str, Tuple[str, ...]] = {
    FAMILY_GTM: ("Revenue Systems Manager", "Sales Systems Manager", "GTM Operations",
                 "Revenue Enablement Manager", "Sales Operations Analyst",
                 "CRM Operations Manager", "Business Systems Analyst"),
    FAMILY_MARKETING: ("Marketing Operations Manager", "Campaign Operations Manager",
                       "Lifecycle Marketing Manager", "Growth Operations Manager"),
    FAMILY_CS: ("Customer Success Operations", "Support Operations Manager"),
    FAMILY_ENGINEERING: ("Business Process Automation Engineer", "Integration Engineer",
                         "Solutions Engineer", "Workflow Automation Engineer"),
}

# Contextual (family-SCOPED) exclusions for broad activity words. Applied ONLY
# inside the clause of the family named -- never globally.
FAMILY_SCOPED_EXCLUSIONS: Dict[str, Tuple[str, ...]] = {
    FAMILY_ENGINEERING: ("hvac", "building automation", "industrial automation",
                         "manufacturing controls", "plc"),
}

# Provider-side fields that can express functional intent BEFORE billing. Verified
# present in the authenticated OpenAPI contract for /v1/active-jb.
PROVIDER_FUNCTIONAL_SIGNALS: Dict[str, str] = {
    "title_advanced": "tsquery over titles; scoped '& !term' negation PROVEN honored",
    "description_advanced": "present in the contract; semantics NOT yet verified by probe",
    "ai_taxonomies_a": "33-value enum, comma-joined, overlap match (broad)",
    "ai_taxonomies_a_primary": "same enum, matches only the PRIMARY taxonomy (tight)",
    "exclude_ai_taxonomies_a": "33-value enum, comma-joined, excludes on any overlap",
    "seniority": "server-side seniority filter",
    "ai_work_arrangement": "enum: Remote Solely | Remote OK | Hybrid | On-site",
}

# NOTE: ai_key_skills -- the signal the ontology is DERIVED from -- is a RETURNED
# field only. It cannot be queried, so activity intent must be expressed through
# the fields above. Never use it as a post-billing filter to justify buying rows.


def ontology_summary() -> Dict[str, Any]:
    return {
        "families": list(FAMILY_ORDER),
        "clusters": [{"id": c.id, "family": c.family, "billed": c.billed,
                      "final_pass": c.final_pass, "yield": round(c.yield_rate, 4),
                      "polarity": "positive" if c.yield_rate > 0.1739 else "negative"}
                     for c in ACTIVITY_CLUSTERS],
        "adjacent_candidates": {k: len(v) for k, v in ADJACENT_TITLE_CANDIDATES.items()},
        "provider_signals": sorted(PROVIDER_FUNCTIONAL_SIGNALS),
    }
