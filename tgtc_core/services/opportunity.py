"""Qualify one opportunity: employer facts, candidate discovery, selective paid
enrichment, real gates, deterministic approval + outbox in one transaction.

PRODUCT_CONTRACT §7-§8, tightened after review a090a2b:

* R01 -- a SEARCH result carries limited data (Apollo documents no email, no LinkedIn).
  Before enrichment only search-visible evidence is judged (title/authority, founder
  tier, a provably different organization). LinkedIn, current employment, territory
  and the verified employer email are checked AFTER enrichment, on the enriched record.
  A pre-enrichment skip is cheap and re-evaluable; it never blacklists a candidate.
* R04 -- a stored person is reused ONLY after passing the same current-employment,
  authority, territory and email checks as a fresh enrichment, from the evidence stored
  with them. ``people.employer_id`` is set only when the contact gate proved the
  employment; a contradicted employer is never attributed.
* Recovery (a) -- selectors fall back to ``organization_ids[]`` when the domain search
  leaves no USABLE candidate after exclusions, not merely when it returns nobody.
* Recovery (b) -- paid attempts are budgeted per evidence epoch. A reopen on new
  evidence (a new or modified posting) starts the next epoch: the history stays,
  candidates already judged on evidence stay excluded, and the number of epochs is
  itself bounded (``max_evidence_epochs``).
* R08 -- the controlled probe after a refusal is reserved atomically across workers.

The provider is called OUTSIDE any transaction; intent is committed before every
potentially chargeable request and the outcome after.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

import psycopg

from ..db.connection import jsonb, transaction
from ..db import work_queue
from ..domain.approval import ApprovalRefusal, ApprovedLead, airtable_fields, build_approved_lead, instantly_payload
from ..domain.facts import RULE_VERSION
from ..domain.gates import (
    corroborated_alternate_domains, evaluate_contact, evaluate_email, person_organization, pre_enrichment_check,
    title_matches,
)
from ..domain.identity import company_names_compatible, domain_name_consistent, person_ref, safe_employer_domain
from ..policy.campaigns import (
    DIRECT_BUYER_TITLES, campaign_route_configured, buyer_titles, is_founder_tier, resolve_campaign_id,
)
from ..policy.requirements import excluded_industry, rule
from ..domain.employer_attribution import employer_attribution_conflict
from ..providers.apollo import ApolloClient, ApolloResult, Outcome, person_org_domain
from . import provider_state
from .spend_budget import SpendBudget
from .suppression import (
    account_keys,
    check as suppression_check,
    company_function_contact_keys,
    company_function_keys,
)

#: Approval refusals that describe the OPPORTUNITY (not the candidate): no other
#: candidate can change them, so the opportunity closes with the reason.
OPPORTUNITY_LEVEL_REFUSALS = frozenset({
    "suppressed", "posting_too_old", "posting_expired", "posting_not_active", "posting_excluded",
    "employer_identity_incomplete", "employer_too_small", "employer_too_large", "employer_excluded_industry",
    "employer_is_agency",
    "no_responsibility_evidence", "copy_fields_incomplete", "function_not_compatible",
})

# These are runtime dependencies, not evidence that the opportunity is bad.
# Closing them permanently both loses volume and can waste Apollo credits before
# discovering a missing Instantly route.
CONFIGURATION_LEVEL_REFUSALS = frozenset({
    "no_campaign_configured", "campaign_id_not_allowed", "signing_key_missing",
})

RECOVERABLE_OPPORTUNITY_CLOSE_REASONS = frozenset({
    "no_candidates_found", "no_usable_candidates_in_search",
    "no_unjudged_candidates_remaining", "no_verified_buyer_in_candidates",
    *(f"approval_refused:{reason}" for reason in CONFIGURATION_LEVEL_REFUSALS),
})

#: Stored per person so reuse can run the SAME gates later without a new paid call.
EVIDENCE_KEYS = ("title", "headline", "linkedin_url", "organization", "employment_history", "city", "state", "country",
                 "seniority", "organization_name", "organization_domain")

# ---------------------------------------------------------------------------
# Phase 2 audit task 9 (2026-09-19): contact depth 2-3, role-diverse, person-unique.
#
# Authority: search 2-3 role-diverse contacts per company x campaign -- the
# functional hiring owner, the executive functional leader, and a TA/People
# leader when appropriate. Three people in the same role are NOT
# diversification. A person counts once globally.
#
# Measured defect: contact depth never exceeds 1 (86/86 approved opportunities
# had exactly one contact; no purchase ever followed a first approval), because
# ``_finalize_approved`` moves the opportunity to the terminal 'approved' state
# on ANY approval, whether or not the configured quota was met, and it never
# reopens (``process()`` refuses any opportunity whose state isn't 'open').
# Ranking (``_rank``, below) followed title-list order with no notion of
# persona at all.
# ---------------------------------------------------------------------------
PERSONA_FUNCTIONAL_OWNER = "functional_owner"
PERSONA_EXECUTIVE_LEADER = "executive_leader"
PERSONA_TA_PEOPLE_LEADER = "ta_people_leader"
CONTACT_PERSONAS = (PERSONA_FUNCTIONAL_OWNER, PERSONA_EXECUTIVE_LEADER, PERSONA_TA_PEOPLE_LEADER)

#: Within the people_hr buyer hierarchy specifically, a Talent-Acquisition title
#: is its own persona (distinct from generic HR/People-ops titles): a TA/People
#: leader is a genuinely different contact from an HR functional owner even
#: though both are in the SAME buyer hierarchy (task 9 authority, "a TA/People
#: leader when appropriate").
_TA_PEOPLE_TITLES = ("Talent Acquisition Director", "Head of Talent Acquisition")


def contact_persona(title: str, function_key: str) -> str:
    """Which of the three diversification personas a buyer title represents.

    Shares the SAME title hierarchy ``campaigns.py`` already searches
    (``DIRECT_BUYER_TITLES`` = the functional hiring owner) and the SAME
    matcher (``title_matches``) instead of a second title classifier, so this
    view cannot drift from what was actually searched. Anything not a direct
    manager (and not a people_hr TA title) is the executive functional leader
    -- ``buyer_titles()`` only ever returns direct-manager or executive-tier
    titles (founders last, or never).
    """
    if function_key == "people_hr" and title_matches(title, _TA_PEOPLE_TITLES):
        return PERSONA_TA_PEOPLE_LEADER
    if title_matches(title, DIRECT_BUYER_TITLES.get(function_key, ())):
        return PERSONA_FUNCTIONAL_OWNER
    return PERSONA_EXECUTIVE_LEADER


@dataclass
class ContactState:
    """How many independently-approved contacts an opportunity already holds
    (``existing_count`` -- the quota baseline; may include legacy/company-wide
    contact records that carry no comparable Apollo identity, so it is a COUNT
    only, never compared to a candidate for identity), which of THOSE contacts
    have a known Apollo identity (``existing_person_keys``, in the SAME
    ``person_ref()`` format a search candidate's ``person_key`` uses -- the
    identity ``select_next_contact`` actually compares), and which personas
    they are.

    Fix round 1, C3 (CRITICAL, independent review): ``existing_person_keys``
    used to be seeded from ``existing_contacts`` -- ``email:<addr>`` or a
    legacy imported ``contact_key`` (``suppression.py``'s
    ``company_function_contact_keys``) -- a namespace a search candidate's
    ``pid:``/``li:`` ref (``identity.person_ref``) essentially never
    intersects, so the "never re-select a held person" check was comparing two
    disjoint key spaces: dead code in the live path (the actual global
    uniqueness guarantee comes from ``approvals_person_active_uq`` and
    ``_excluded_refs``, which already exclude an approved person from ever
    reaching the candidate list in the first place). ``existing_count`` and
    ``existing_person_keys`` are now two separate fields precisely so the
    quota arithmetic (which legitimately needs the broader legacy count) and
    the identity comparison (which needs an apples-to-apples format) cannot be
    conflated again. See ``_build_contact_state`` below for the seeding.
    """
    existing_count: int = 0
    existing_person_keys: Set[str] = field(default_factory=set)
    existing_personas: List[str] = field(default_factory=list)

    def add(self, ref: str, persona: str = "") -> None:
        """Record one new approval made during this call."""
        self.existing_count += 1
        if ref:
            self.existing_person_keys.add(ref)
        if persona:
            self.existing_personas.append(persona)

    def wants_more_contacts(self, max_contacts: int) -> bool:
        """Never raises the configured maximum: clamped to [1, 3] here too, the
        same clamp ``OpportunityService.__init__`` applies to its own setting."""
        return self.existing_count < max(1, min(3, max_contacts))


def _build_contact_state(existing_contacts: Set[str], approved_rows: Sequence[Dict[str, Any]],
                         function_key: str) -> "ContactState":
    """The ``ContactState`` for one opportunity: ``existing_contacts`` is the
    legacy quota-only baseline (see ``ContactState`` docstring); ``approved_rows``
    are THIS opportunity's own current approvals, each carrying the real Apollo
    identity (``apollo_person_id``/``linkedin_url``) a search candidate's
    ``person_key`` is directly comparable to.

    Fix round 2 (Minor, independent review): comparable via the SAME
    precedence-ordered ``person_ref()`` FUNCTION ``_search_candidates`` calls
    to build ``cand["_ref"]`` -- not literally identical keyword arguments
    (``_search_candidates`` also passes ``name``/``employer_key`` as a
    fallback for a candidate with no id, which Apollo search results always
    carry in practice, so ``apollo_person_id`` alone decides both calls'
    output; the two calls are not byte-identical, but their governing
    precedence rule and, for every real candidate, their result, are)."""
    person_keys = {
        person_ref(apollo_person_id=row.get("apollo_person_id"), linkedin_url=row.get("linkedin_url"))
        for row in approved_rows
    }
    person_keys.discard("")
    personas = [contact_persona(str(row.get("title") or ""), function_key) for row in approved_rows if row.get("title")]
    return ContactState(existing_count=len(existing_contacts), existing_person_keys=person_keys, existing_personas=personas)


def select_next_contact(
    *, candidates: Sequence[Dict[str, Any]],
    existing_person_keys: Set[str] = frozenset(),
    existing_personas: Sequence[str] = (),
) -> Optional[Dict[str, Any]]:
    """The next candidate worth pursuing: never a person already held (globally
    unique), and only one whose persona is not already represented.

    Fix round 1, I4 (Luis's ruling): "three people in the same role are NOT
    diversification" is a fixed business rule, not a preference the acceptance
    test merely illustrated -- once every persona present among the eligible
    candidates is already held, this returns ``None`` (stop at the current
    depth) rather than falling back to a same-persona pick.
    """
    held_personas = set(existing_personas)
    unheld = [c for c in candidates if c.get("person_key") not in existing_person_keys]
    for cand in unheld:
        if cand.get("persona") and cand["persona"] not in held_personas:
            return cand
    return None


@dataclass
class QualifyOutcome:
    opportunity_id: int
    outcome: str  # approved | closed | wait | retry
    reason: str = ""
    approval_id: Optional[int] = None
    person_id: Optional[int] = None
    attempts_made: int = 0
    details: Dict[str, Any] = field(default_factory=dict)


class ProviderWait(Exception):
    def __init__(self, reason: str, until: datetime):
        super().__init__(reason)
        self.reason, self.until = reason, until


class ProviderRetry(Exception):
    pass


def reopen_recoverable_opportunities(
    conn: psycopg.Connection,
    *,
    campaign_env: Dict[str, str],
    signing_key: str,
    now: Optional[datetime] = None,
    limit: int = 5000,
    max_contacts_per_opportunity: int = 1,
) -> int:
    """Re-enter opportunities that were closed for a recoverable dependency.

    This does not create a new evidence epoch: no new commercial evidence was
    invented. It simply restores the existing opportunity and lets the current
    configuration/search policy decide its next state.
    """
    if not signing_key:
        return 0
    moment = now or datetime.now(timezone.utc)
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT o.id, o.function_key, o.lane, e.employee_count
                FROM opportunities o
                JOIN employers e ON e.id = o.employer_id
                WHERE o.state = 'closed' AND (
                    o.close_reason = ANY(%s)
                    OR (
                        o.close_reason LIKE 'no_verified_buyer_after_max_attempts:epoch_%%'
                        AND (SELECT count(*) FROM candidate_attempts ca
                             WHERE ca.opportunity_id = o.id AND ca.attempt_kind = 'match'
                               AND ca.outcome NOT IN ('refused', 'uncertain')
                               AND ca.epoch = o.evidence_epoch) < %s
                    )
                )
                ORDER BY o.last_posting_at DESC
                LIMIT %s
                """,
                (list(RECOVERABLE_OPPORTUNITY_CLOSE_REASONS),
                 int(rule("max_match_attempts_per_evidence_epoch")) * max(1, min(3, max_contacts_per_opportunity)),
                 limit),
            )
            rows = [dict(row) for row in cur.fetchall()]
            reopened = 0
            for row in rows:
                route_ready = (
                    bool(resolve_campaign_id(row["function_key"], row.get("employee_count"), campaign_env))
                    if row.get("employee_count") is not None
                    else campaign_route_configured(row["function_key"], campaign_env)
                )
                if not route_ready:
                    continue
                cur.execute(
                    "UPDATE opportunities SET state = 'open', close_reason = NULL, reopened_at = %s, updated_at = now() "
                    "WHERE id = %s AND state = 'closed'",
                    (moment, row["id"]),
                )
                if not cur.rowcount:
                    continue
                work_queue.enqueue(
                    conn, kind="qualify_opportunity", subject_kind="opportunity",
                    subject_id=int(row["id"]), lane=row["lane"], reopen=True,
                    available_at=moment,
                )
                reopened += 1
    return reopened


class OpportunityService:
    def __init__(self, conn: psycopg.Connection, apollo: ApolloClient, *, campaign_env: Dict[str, str],
                 signing_key: str, retry_hours: float = 6.0, people_search_max_pages: int = 2,
                 people_search_page_size: int = 100, run_id: str = "legacy-unattributed",
                 max_contacts_per_opportunity: int = 1,
                 person_uniqueness: bool = True, verify_on_import: bool = False,
                 spend_budget: Optional[SpendBudget] = None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.conn = conn
        self.apollo = apollo
        self.campaign_env = campaign_env
        self.signing_key = signing_key
        self.retry_hours = retry_hours
        self.search_pages = max(1, people_search_max_pages)
        self.search_page_size = max(1, min(100, people_search_page_size))
        self.run_id = run_id or "legacy-unattributed"
        self.max_contacts = max(1, min(3, max_contacts_per_opportunity))
        self.person_uniqueness = person_uniqueness
        self.verify_on_import = verify_on_import
        self.spend_budget = spend_budget
        self.now = now
        self._work_item = None

    # --- helpers ------------------------------------------------------------
    def _load(self, opportunity_id: int) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
        moment = self.now()
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM opportunities WHERE id = %s", (opportunity_id,))
            opp = cur.fetchone()
            if not opp:
                raise LookupError(f"opportunity {opportunity_id} not found")
            cur.execute("SELECT * FROM employers WHERE id = %s", (opp["employer_id"],))
            emp = cur.fetchone()
            cur.execute(
                """
                SELECT p.*, c.result_json AS classification_json, c.compatible_functions, c.excluded, c.method
                FROM opportunity_postings op JOIN postings p ON p.id = op.posting_id
                JOIN classifications c ON c.id = op.classification_id
                WHERE op.opportunity_id = %s AND p.state = 'classified'
                  AND p.employer_id = %s AND c.result_json->>'input_content_hash' = p.content_hash
                  AND (p.date_valid_through IS NULL OR p.date_valid_through >= %s)
                  AND NOT c.excluded AND %s = ANY (c.compatible_functions)
                ORDER BY p.commercial_age_anchor DESC LIMIT 1
                """,
                (opportunity_id, opp["employer_id"], moment, opp["function_key"]),
            )
            posting = cur.fetchone()
        self.conn.commit()
        return dict(opp), dict(emp), dict(posting) if posting else {}, dict(posting["classification_json"]) if posting else {}

    def _close(self, opportunity_id: int, reason: str) -> QualifyOutcome:
        with transaction(self.conn):
            work_queue.assert_owned(self.conn, self._work_item, now=self.now())
            with self.conn.cursor() as cur:
                cur.execute("UPDATE opportunities SET state = 'closed', close_reason = %s, updated_at = now() WHERE id = %s",
                            (reason[:200], opportunity_id))
        return QualifyOutcome(opportunity_id, "closed", reason)

    def _wait_for_dependency(self, opportunity_id: int, reason: str, *, hours: Optional[float] = None,
                             details: Optional[Dict[str, Any]] = None) -> QualifyOutcome:
        until = self.now() + timedelta(hours=self.retry_hours if hours is None else hours)
        payload = dict(details or {})
        payload["until"] = until.isoformat()
        return QualifyOutcome(opportunity_id, "wait", reason, details=payload)

    def _intent(self, operation: str, *, opportunity_id: int, person_ref_value: str = "", params: Dict[str, Any],
                estimated_credits: Optional[float]) -> int:
        with transaction(self.conn):
            work_queue.assert_owned(self.conn, self._work_item, now=self.now())
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO request_attempts (provider, operation, opportunity_id, person_ref, params_json, estimated_credits) "
                    "VALUES ('apollo', %s, %s, %s, %s, %s) RETURNING id",
                    (operation, opportunity_id, person_ref_value or None, jsonb(params), estimated_credits),
                )
                attempt_id = int(cur.fetchone()["id"])
                if self.spend_budget:
                    self.spend_budget.reserve_attempt(
                        cur, attempt_id=attempt_id, provider="apollo", operation=operation,
                        estimated_credits=float(estimated_credits or 0),
                    )
                return attempt_id

    def _finish(self, attempt_id: int, result: ApolloResult, *, operation: str, estimated: Optional[float]) -> None:
        status = {Outcome.SERVED: "served", Outcome.TIMEOUT: "uncertain", Outcome.CREDIT_EXHAUSTED: "refused",
                  Outcome.UNAUTHORIZED: "refused", Outcome.RATE_LIMITED: "refused"}.get(result.outcome, "failed")
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute(
                    "UPDATE request_attempts SET status = %s, http_status = %s, error_class = %s, error_code = %s, finished_at = now(), response_summary = %s WHERE id = %s",
                    (status, result.status, result.outcome.value, (result.error_code or "")[:200], jsonb(result.summary()), attempt_id),
                )
                if estimated and status in ("served", "uncertain"):
                    cur.execute(
                        "INSERT INTO credit_events (provider, operation, attempt_id, requests, estimated_credits, confirmed_credits, basis) VALUES ('apollo', %s, %s, 1, %s, NULL, 'estimate')",
                        (operation, attempt_id, estimated),
                    )
        if self.spend_budget:
            self.spend_budget.finish_attempt(attempt_id, status)

    def _guard_provider(self, *, chargeable: bool = True) -> None:
        """Reserve only at a chargeable call. A free search can find no usable
        people; it must not consume the sole recovery probe for the next opportunity."""
        check = provider_state.reserve_probe if chargeable else provider_state.may_attempt
        gate = check(self.conn, "apollo", retry_hours=self.retry_hours, now=self.now())
        if not gate["allowed"]:
            raise ProviderWait(f"apollo_{gate['state']}_until_{gate['next_attempt_after'].isoformat()}", gate["next_attempt_after"])

    def _handle_global(self, result: ApolloResult, *, chargeable: bool = True, allow_not_found: bool = False) -> None:
        """Refusals/throttles/timeouts raise; a served CHARGEABLE call records recovery.
        A served 0-credit search proves reachability, not credits."""
        moment = self.now()
        if result.outcome is Outcome.CREDIT_EXHAUSTED:
            provider_state.record_refusal(self.conn, "apollo", result.error_code or "credit_exhausted", now=moment,
                                          details=result.context)
            raise ProviderWait("apollo_credit_exhausted", moment + timedelta(hours=self.retry_hours))
        if result.outcome is Outcome.UNAUTHORIZED:
            provider_state.record_refusal(self.conn, "apollo", result.error_code or "unauthorized",
                                          state=provider_state.UNAUTHORIZED, now=moment)
            raise ProviderWait("apollo_unauthorized", moment + timedelta(hours=self.retry_hours))
        if result.outcome is Outcome.RATE_LIMITED:
            wait = min(float(result.retry_after or 60.0), 900.0)
            raise ProviderWait("apollo_rate_limited", moment + timedelta(seconds=wait))
        if result.outcome is Outcome.TIMEOUT or result.outcome is Outcome.SERVER:
            raise ProviderRetry(result.outcome.value)
        if result.outcome is Outcome.VALIDATION and not (allow_not_found and result.status == 404):
            raise ProviderWait(f"apollo_validation_http_{result.status}", moment + timedelta(hours=self.retry_hours))
        if result.outcome is Outcome.SERVED and chargeable:
            provider_state.record_served(self.conn, "apollo", now=moment)

    # --- employer facts -------------------------------------------------------
    def _ensure_employer_facts(self, opp: Dict[str, Any], emp: Dict[str, Any]) -> Dict[str, Any]:
        need_domain = not emp.get("domain")
        need_size = emp.get("employee_count") is None
        need_industry = not emp.get("industry")
        stale = emp.get("enriched_at") is None
        if not (need_domain or need_size or need_industry) or not stale:
            return emp
        self._guard_provider()
        params = {"domain": emp.get("domain") or "", "name": emp["canonical_name"]}
        attempt_id = self._intent("organization_enrich", opportunity_id=opp["id"], params=params, estimated_credits=1)
        result = self.apollo.enrich_organization(domain=params["domain"], name=params["name"])
        self._finish(attempt_id, result, operation="organization_enrich", estimated=1)
        self._handle_global(result)
        org = (result.data or {}).get("organization") or {}
        with transaction(self.conn):
            work_queue.assert_owned(self.conn, self._work_item, now=self.now())
            with self.conn.cursor() as cur:
                if org:
                    apollo_domain = safe_employer_domain(org.get("primary_domain") or org.get("domain") or org.get("website_url"))
                    name_ok = company_names_compatible(emp["canonical_name"], str(org.get("name") or ""))
                    domain_ok = bool(emp.get("domain") and apollo_domain and apollo_domain == emp["domain"])
                    trusted = domain_ok or (name_ok and (not emp.get("domain") or not apollo_domain))
                    if emp.get("domain") and apollo_domain and apollo_domain != emp["domain"]:
                        cur.execute(
                            "INSERT INTO evidence (subject_kind, subject_id, fact, value, status, source, excerpt) VALUES ('employer', %s, 'apollo_domain_disagreement', %s, 'recorded', 'apollo', %s)",
                            (emp["id"], jsonb({"employer_domain": emp["domain"], "apollo_domain": apollo_domain}), str(org.get("name") or "")),
                        )
                        trusted = name_ok
                    if trusted:
                        new_domain = emp.get("domain") or (apollo_domain if domain_name_consistent(emp["canonical_name"], apollo_domain) or name_ok else None)
                        count = org.get("estimated_num_employees") or org.get("num_employees")
                        cur.execute(
                            """
                            UPDATE employers SET domain = COALESCE(domain, %s), apollo_org_id = COALESCE(apollo_org_id, %s),
                                   employee_count = COALESCE(%s, employee_count), industry = COALESCE(%s, industry),
                                   founded_year = COALESCE(%s, founded_year), facts_json = facts_json || %s, enriched_at = %s, updated_at = now()
                            WHERE id = %s
                            """,
                            (new_domain, str(org.get("id") or "") or None, int(count) if isinstance(count, int) else None,
                             org.get("industry") or None, org.get("founded_year") if isinstance(org.get("founded_year"), int) else None,
                             jsonb({"apollo": {"name": org.get("name"), "primary_domain": apollo_domain, "linkedin_url": org.get("linkedin_url")}}),
                             self.now(), emp["id"]),
                        )
                        if new_domain and not emp.get("domain"):
                            cur.execute("INSERT INTO employer_aliases (employer_id, alias_kind, alias_value, evidence) VALUES (%s, 'domain', %s, %s) ON CONFLICT DO NOTHING",
                                        (emp["id"], new_domain, jsonb({"source": "apollo", "basis": "organization_enrich_name_consistent"})))
                        if org.get("id"):
                            cur.execute("INSERT INTO employer_aliases (employer_id, alias_kind, alias_value, evidence) VALUES (%s, 'apollo_org_id', %s, %s) ON CONFLICT DO NOTHING",
                                        (emp["id"], str(org.get("id")), jsonb({"source": "apollo"})))
                    else:
                        cur.execute("UPDATE employers SET facts_json = facts_json || %s, enriched_at = %s, updated_at = now() WHERE id = %s",
                                    (jsonb({"apollo_untrusted_match": str(org.get("name") or "")}), self.now(), emp["id"]))
                else:
                    cur.execute("UPDATE employers SET facts_json = facts_json || %s, enriched_at = %s, updated_at = now() WHERE id = %s",
                                (jsonb({"apollo": "not_found"}), self.now(), emp["id"]))
                cur.execute("SELECT * FROM employers WHERE id = %s", (emp["id"],))
                return dict(cur.fetchone())

    # --- candidates -------------------------------------------------------------
    def _search_candidates(self, opp: Dict[str, Any], emp: Dict[str, Any], titles: Sequence[str],
                           excluded: Set[str], *, broaden: bool = False) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, int]]:
        """Returns (usable candidates, pre-enrichment rejections, stats).

        Both known selectors are searched within the existing per-selector page
        budget. A plausible search result can still fail enrichment; it must not
        prevent the alternate selector from contributing a verified candidate.
        """
        usable: List[Dict[str, Any]] = []
        dropped: List[Dict[str, Any]] = []
        stats = {"returned": 0, "excluded_already_judged": 0, "dropped_other_company": 0,
                 "selectors_tried": 0, "pages_searched": 0, "page_caps_reached": 0,
                 "broadened_searches": int(broaden)}
        seen: Set[str] = set()
        # A few portability/compatibility tests intentionally construct the
        # service with ``__new__`` and only provide the historical
        # ``search_pages`` attribute.  Keep the production default available
        # for those legacy construction paths instead of making the new page
        # size setting an implicit required dependency.
        page_size = max(1, min(100, int(getattr(self, "search_page_size", 100))))
        for selector in (("domain", emp["domain"]), ("organization_id", emp.get("apollo_org_id") or "")):
            if not selector[1]:
                continue
            stats["selectors_tried"] += 1
            for page in range(1, self.search_pages + 1):
                self._guard_provider(chargeable=False)
                params = {selector[0]: selector[1], "titles": list(titles), "page": page,
                          "per_page": page_size, "email_statuses": ["verified"]}
                if broaden:
                    params["include_similar_titles"] = True
                attempt_id = self._intent("people_search", opportunity_id=opp["id"], params=params, estimated_credits=0)
                kwargs: Dict[str, Any] = {"titles": list(titles), "page": page,
                                          "per_page": page_size,
                                          "email_statuses": ["verified"]}
                if broaden:
                    kwargs["include_similar_titles"] = True
                kwargs[selector[0]] = selector[1]
                result = self.apollo.search_people(**kwargs)
                self._finish(attempt_id, result, operation="people_search", estimated=0)
                self._handle_global(result, chargeable=False)
                if not result.served:
                    break
                stats["pages_searched"] += 1
                page_people = [p for p in (result.data.get("people") or []) if isinstance(p, dict)]
                stats["returned"] += len(page_people)
                for p in page_people:
                    ref = person_ref(apollo_person_id=str(p.get("id") or p.get("person_id") or ""),
                                     name=f"{p.get('first_name', '')} {p.get('last_name', '')}", employer_key=f"domain:{emp['domain']}")
                    if not ref or ref in seen:
                        continue
                    seen.add(ref)
                    p["_ref"] = ref
                    pd = person_org_domain(p)
                    if pd and pd != emp["domain"] and not pd.endswith("." + emp["domain"]):
                        org = person_organization(p)
                        corroborated = company_names_compatible(emp["canonical_name"], str(org.get("name") or "")) or (
                            emp.get("apollo_org_id") and str(org.get("id") or "") == str(emp.get("apollo_org_id")))
                        if not corroborated:
                            p["_drop_reason"] = f"contact:wrong_organization_search_guard:{pd}"
                            dropped.append(p)
                            stats["dropped_other_company"] += 1
                            continue
                    if ref in excluded:
                        stats["excluded_already_judged"] += 1
                        continue
                    count = emp.get("employee_count")
                    pre = pre_enrichment_check(
                        person=p, employer_name=emp["canonical_name"],
                        employer_domains={emp["domain"]}, buyer_titles=titles,
                        founder_allowed=count is not None and count <= int(rule("founder_fallback_max_employees")),
                    )
                    if not pre.passed:
                        p["_drop_reason"] = pre.reason
                        dropped.append(p)
                        continue
                    usable.append(p)
                pagination = result.data.get("pagination") if isinstance(result.data.get("pagination"), dict) else {}
                total_pages = pagination.get("total_pages")
                if len(page_people) < page_size or (isinstance(total_pages, int) and page >= total_pages):
                    break
                if page == self.search_pages:
                    stats["page_caps_reached"] += 1
        if not usable and not broaden:
            # Broaden discovery only. Local authority, current employment,
            # territory, suppression and verified email gates are unchanged.
            extra, extra_dropped, extra_stats = self._search_candidates(opp, emp, titles, excluded, broaden=True)
            usable.extend(extra)
            dropped.extend(p for p in extra_dropped if p["_ref"] not in seen)
            for key, value in extra_stats.items():
                stats[key] += value
        return usable, dropped, stats

    def _rank(self, candidates: List[Dict[str, Any]], titles: Sequence[str]) -> List[Dict[str, Any]]:
        def rank(p: Dict[str, Any]) -> int:
            t = str(p.get("title") or "")
            for i, target in enumerate(titles):
                if title_matches(t, [target]):
                    return i
            return len(titles) + (5 if is_founder_tier(t) else 0)
        return sorted(candidates, key=rank)

    def _excluded_refs(self, opportunity_id: int) -> Tuple[Set[str], Set[str], Set[str]]:
        """(judged for this opportunity, active approvals, suppressed emails).

        Judged = a served/not-found paid match or a post-enrichment gate FAIL. A provider
        refusal, a lost response, or a pre-enrichment skip (search-only evidence) is not
        a judgement and never excludes the candidate (R01).
        """
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT candidate_ref FROM candidate_attempts WHERE opportunity_id = %s AND ("
                "(attempt_kind = 'match' AND outcome NOT IN ('refused', 'uncertain')) OR (attempt_kind = 'gate' AND outcome = 'fail'))",
                (opportunity_id,))
            judged = {r["candidate_ref"] for r in cur.fetchall()}
            cur.execute("SELECT p.apollo_person_id, p.linkedin_url FROM approvals a JOIN people p ON p.id = a.person_id WHERE a.state <> 'revoked'")
            approved = set()
            for r in cur.fetchall():
                if r["apollo_person_id"]:
                    approved.add(f"pid:{r['apollo_person_id']}")
                if r["linkedin_url"]:
                    approved.add(f"li:{str(r['linkedin_url']).lower().rstrip('/')}")
            cur.execute("SELECT key FROM suppressions WHERE kind = 'person_email'")
            suppressed = {r["key"] for r in cur.fetchall()}
        self.conn.commit()
        return judged, approved, suppressed

    def _record_attempt(self, opportunity_id: int, ref: str, kind: str, outcome: str, reason: str, *, epoch: int,
                        person_id: Optional[int] = None, attempt_id: Optional[int] = None, details: Optional[Dict[str, Any]] = None) -> None:
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO candidate_attempts (opportunity_id, person_id, candidate_ref, attempt_kind, outcome, reason, attempt_id, details, epoch) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (opportunity_id, candidate_ref, attempt_kind) DO UPDATE SET "
                    "outcome = EXCLUDED.outcome, reason = EXCLUDED.reason, person_id = COALESCE(EXCLUDED.person_id, candidate_attempts.person_id), "
                    "details = EXCLUDED.details, epoch = EXCLUDED.epoch, attempt_id = COALESCE(EXCLUDED.attempt_id, candidate_attempts.attempt_id), created_at = now()",
                    (opportunity_id, person_id, ref, kind, outcome, reason[:200], attempt_id, jsonb(details or {}), epoch),
                )

    def _upsert_person(self, enriched: Dict[str, Any], emp: Dict[str, Any], *, email_alignment: str,
                       employment_verified: bool) -> int:
        """Persist the enriched record with the evidence the gates need later (R04).
        ``employer_id`` is attributed only when the contact gate proved current
        employment at this employer."""
        org = person_organization(enriched)
        email = str(enriched.get("email") or "").strip().lower() or None
        status = str(enriched.get("email_status") or enriched.get("contact_email_status") or "").lower() or None
        evidence = {k: enriched.get(k) for k in EVIDENCE_KEYS if enriched.get(k) not in (None, "", [])}
        with transaction(self.conn):
            work_queue.assert_owned(self.conn, self._work_item, now=self.now())
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO people (apollo_person_id, linkedin_url, first_name, last_name, title, employer_id, organization_name,
                                        organization_domain, email, email_status, email_authority, email_verified_at, facts_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (apollo_person_id) WHERE apollo_person_id IS NOT NULL DO UPDATE SET
                        linkedin_url = COALESCE(EXCLUDED.linkedin_url, people.linkedin_url), first_name = EXCLUDED.first_name,
                        last_name = EXCLUDED.last_name, title = EXCLUDED.title, employer_id = EXCLUDED.employer_id,
                        organization_name = EXCLUDED.organization_name, organization_domain = EXCLUDED.organization_domain,
                        email = COALESCE(EXCLUDED.email, people.email), email_status = COALESCE(EXCLUDED.email_status, people.email_status),
                        email_authority = EXCLUDED.email_authority, email_verified_at = COALESCE(EXCLUDED.email_verified_at, people.email_verified_at),
                        facts_json = people.facts_json || EXCLUDED.facts_json, updated_at = now()
                    RETURNING id
                    """,
                    (str(enriched.get("id") or ""), enriched.get("linkedin_url"), enriched.get("first_name"), enriched.get("last_name"),
                     enriched.get("title"), emp["id"] if employment_verified else None, org.get("name") or enriched.get("organization_name"),
                     person_org_domain(enriched) or None, email, status, "apollo" if email else None,
                     self.now() if status == "verified" else None,
                     jsonb({"email_alignment": email_alignment, "headline": enriched.get("headline"), "enriched": evidence,
                            "enriched_at": self.now().isoformat(), "employment_verified": employment_verified})),
                )
                return int(cur.fetchone()["id"])

    # --- approval ----------------------------------------------------------------
    def _approve(self, opp: Dict[str, Any], emp: Dict[str, Any], posting: Dict[str, Any], classification: Dict[str, Any],
                 person_row: Dict[str, Any]) -> ApprovedLead | ApprovalRefusal:
        campaign_id = resolve_campaign_id(opp["function_key"], emp.get("employee_count"), self.campaign_env)
        hits = suppression_check(
            self.conn, email=str(person_row.get("email") or ""),
            company_function=company_function_keys(domain=emp.get("domain") or "", name=emp["canonical_name"],
                                                   slug=emp.get("linkedin_slug") or "", function_key=opp["function_key"]),
            account=account_keys(domain=emp.get("domain") or "", name=emp["canonical_name"]),
            company_function_limit=self.max_contacts,
        )
        self.conn.commit()
        employer_view = dict(emp)
        employer_view["excluded_industry"] = excluded_industry(str(emp.get("industry") or ""))
        return build_approved_lead(
            posting=posting, classification=classification, employer=employer_view, person=person_row,
            function_key=opp["function_key"], campaign_id=campaign_id,
            allowed_campaign_ids=list(self.campaign_env.values()), signing_key=self.signing_key, now=self.now(),
            suppression_hits=hits,
        )

    def _commit_approval(self, opp: Dict[str, Any], person_id: int, approved: ApprovedLead) -> int:
        """One approval + both outbox items in ONE transaction.

        The opportunity stays open until this qualification pass has collected
        every independently valid contact available up to ``max_contacts``.
        """
        lead = approved.lead
        with transaction(self.conn):
            work_queue.assert_owned(self.conn, self._work_item, now=self.now())
            with self.conn.cursor() as cur:
                cur.execute("SELECT state, evidence_epoch FROM opportunities WHERE id = %s FOR UPDATE", (opp["id"],))
                current_opp = cur.fetchone()
                cur.execute("SELECT content_hash, employer_id, state, date_valid_through FROM postings WHERE id = %s FOR UPDATE",
                            (lead["posting_id"],))
                current_posting = cur.fetchone()
                if (not current_opp or current_opp["state"] != "open"
                        or int(current_opp["evidence_epoch"]) != int(opp.get("evidence_epoch") or 1)
                        or not current_posting or current_posting["state"] != "classified"
                        or current_posting["employer_id"] != opp["employer_id"]
                        or current_posting["content_hash"] != lead["posting_content_hash"]
                        or (current_posting["date_valid_through"] and current_posting["date_valid_through"] < self.now())):
                    raise work_queue.EvidenceChanged("approval input changed during qualification")
                cur.execute(
                    """
                    INSERT INTO approvals (opportunity_id, person_id, employer_id, campaign_key, function_key, campaign_id, policy_version,
                                           lead_key, fingerprint, lead_json, run_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                    """,
                    (opp["id"], person_id, opp["employer_id"], lead["campaign_key"], lead["function_key"], lead["campaign_id"],
                     lead["policy_version"], approved.lead_key, approved.fingerprint, jsonb(lead), self.run_id),
                )
                approval_id = int(cur.fetchone()["id"])
                cur.execute(
                    "UPDATE opportunities SET approved_person_id = COALESCE(approved_person_id, %s), "
                    "close_reason = NULL, updated_at = now() WHERE id = %s",
                    (person_id, opp["id"]),
                )
                cur.execute(
                    "INSERT INTO delivery_outbox (approval_id, channel, idempotency_key, payload_json, available_at) VALUES (%s, 'airtable', %s, %s, %s)",
                    (approval_id, f"airtable:{approved.lead_key}", jsonb(airtable_fields(lead, approved.fingerprint)), self.now()),
                )
                cur.execute(
                    "INSERT INTO delivery_outbox (approval_id, channel, idempotency_key, payload_json, available_at) VALUES (%s, 'instantly', %s, %s, %s)",
                    (approval_id, f"instantly:{lead['campaign_id']}:{lead['email']}",
                     jsonb(instantly_payload(lead, skip_if_in_workspace=self.person_uniqueness, verify_on_import=self.verify_on_import)),
                     self.now()),
                )
        return approval_id

    def _finalize_approved(self, opportunity_id: int) -> None:
        with transaction(self.conn):
            work_queue.assert_owned(self.conn, self._work_item, now=self.now())
            with self.conn.cursor() as cur:
                cur.execute(
                    "UPDATE opportunities SET state = 'approved', close_reason = NULL, updated_at = now() "
                    "WHERE id = %s AND state = 'open' AND EXISTS ("
                    "SELECT 1 FROM approvals WHERE opportunity_id = %s AND state <> 'revoked')",
                    (opportunity_id, opportunity_id),
                )

    def _finalize_or_wait_partial(self, opportunity_id: int, *, state: "ContactState", approval_ids: List[int],
                                  approved_person_ids: List[int], approved_total: int, matches_done: int, made: int,
                                  max_attempts: int, no_further_candidates: bool, reason_if_quota_met: str,
                                  wait_reason: str) -> QualifyOutcome:
        """One decision point for "we hold at least one contact for this
        opportunity, but not yet the full quota": finalize (the quota is now
        met), give up and finalize with what we have, or wait and retry --
        the same idiom ``buyer_search_pending`` already uses elsewhere for
        "absence is not proven yet".

        Fix round 1, I5 (Luis's ruling): a partially filled opportunity must
        not requeue at ``buyer_search_pending`` forever, corrupting
        ``opportunities.state = 'approved'`` into a near-unreachable state.

        Fix round 2, CRITICAL (independent review): round 1's give-up
        condition keyed on the paid-attempt BUDGET
        (``matches_done + made >= max_attempts``) alone -- but the two paths
        that make NO progress (``select_next_contact`` returns ``None``, or a
        pass finds zero candidates at all) reach this with ``made = 0``,
        making zero paid attempts, so the budget never advances and the
        opportunity waited forever. 9 of 10 functions expose exactly 2
        reachable personas (only people_hr has a third), so at the shipped
        default quota of 3 this was not a corner case. Termination now keys
        PRIMARILY on ``no_further_candidates`` -- there is no further
        role-diverse candidate this pass could select, so persona diversity
        or credits could never advance the quota regardless of how long this
        waited -- with the attempt-budget check kept only as a secondary,
        genuinely distinct case: real untried candidates still exist, but
        this epoch's spend cap was reached trying others first.
        """
        details = {"approvals_created": len(approval_ids), "approved_total": approved_total + len(approval_ids)}
        if not state.wants_more_contacts(self.max_contacts):
            self._finalize_approved(opportunity_id)
            return QualifyOutcome(
                opportunity_id, "approved", reason_if_quota_met,
                approval_ids[0] if approval_ids else None, approved_person_ids[0] if approved_person_ids else None,
                attempts_made=made, details=details,
            )
        if no_further_candidates:
            self._finalize_approved(opportunity_id)
            details["contact_quota_target"] = self.max_contacts
            return QualifyOutcome(
                opportunity_id, "approved", "approved_no_further_role_diverse_candidates",
                approval_ids[0] if approval_ids else None, approved_person_ids[0] if approved_person_ids else None,
                attempts_made=made, details=details,
            )
        if matches_done + made >= max_attempts:
            self._finalize_approved(opportunity_id)
            details["contact_quota_target"] = self.max_contacts
            return QualifyOutcome(
                opportunity_id, "approved", "approved_partial_quota_epoch_exhausted",
                approval_ids[0] if approval_ids else None, approved_person_ids[0] if approved_person_ids else None,
                attempts_made=made, details=details,
            )
        return self._wait_for_dependency(opportunity_id, wait_reason, hours=max(24.0, self.retry_hours), details=details)

    def _gate_enriched(self, enriched: Dict[str, Any], emp: Dict[str, Any], titles: Sequence[str], founder_allowed: bool,
                       employer_domains: Set[str]):
        """The complete final checks on an enriched (or stored) record: identity, current
        employer, authority, territory, LinkedIn, verified employer email."""
        contact = evaluate_contact(person=enriched, employer_name=emp["canonical_name"], employer_domains=employer_domains,
                                   buyer_titles=titles, founder_allowed=founder_allowed,
                                   require_linkedin=bool(rule("require_contact_linkedin")),
                                   require_current_employment=bool(rule("require_current_employment_evidence")))
        apollo_org = (emp.get("facts_json") or {}).get("apollo") if isinstance(emp.get("facts_json"), dict) else None
        alt = corroborated_alternate_domains(
            employer_name=emp["canonical_name"], employer_domains=employer_domains, person=enriched,
            apollo_org={"name": (apollo_org or {}).get("name"), "primary_domain": (apollo_org or {}).get("primary_domain"),
                        "id": emp.get("apollo_org_id")} if isinstance(apollo_org, dict) else None)
        email = evaluate_email(email=enriched.get("email"), email_status=enriched.get("email_status") or enriched.get("contact_email_status"),
                               employer_domains=employer_domains, corroborated_domains=alt)
        return contact, email

    # --- main -------------------------------------------------------------------
    def process(self, opportunity_id: int, *, work_item: Optional[work_queue.WorkItem] = None) -> QualifyOutcome:
        self._work_item = work_item
        opp, emp, posting, classification = self._load(opportunity_id)
        if opp["state"] != "open":
            return QualifyOutcome(opportunity_id, "closed", f"already_{opp['state']}")
        if not posting:
            return self._close(opportunity_id, "no_active_compatible_posting")
        if employer_attribution_conflict(posting.get("description_text"),
                                         employer_name=emp["canonical_name"], employer_domain=emp.get("domain") or ""):
            return self._close(opportunity_id, "employer_attribution_conflict")
        # Known disqualifiers precede paid organization enrichment. Unknown facts
        # remain eligible for enrichment; do not spend to rediscover a rejection.
        if emp.get("agency_flag") is True:
            return self._close(opportunity_id, "employer_is_agency")
        known_industry = excluded_industry(str(emp.get("industry") or ""))
        if known_industry:
            return self._close(opportunity_id, f"employer_excluded_industry:{known_industry}")
        known_count = emp.get("employee_count")
        if known_count is not None and known_count < int(rule("min_employees")):
            return self._close(opportunity_id, "employer_too_small")
        if known_count is not None and known_count > int(rule("max_employees")):
            return self._close(opportunity_id, "employer_too_large")
        # Refuse to spend Apollo credits when the output route cannot currently
        # produce a lead. A shared campaign env (for example Customer Experience)
        # counts as configured for either of its function keys.
        if not self.signing_key:
            return self._wait_for_dependency(opportunity_id, "configuration_pending:signing_key_missing")
        if not campaign_route_configured(opp["function_key"], self.campaign_env):
            return self._wait_for_dependency(
                opportunity_id, f"configuration_pending:no_campaign_configured:{opp['function_key']}"
            )
        epoch = int(opp.get("evidence_epoch") or 1)
        if epoch > int(rule("max_evidence_epochs")):
            return self._close(opportunity_id, "evidence_epochs_exhausted")
        company_keys = company_function_keys(
            domain=emp.get("domain") or "", name=emp["canonical_name"],
            slug=emp.get("linkedin_slug") or "", function_key=opp["function_key"],
        )
        hits = suppression_check(
            self.conn,
            company_function=company_keys,
            account=account_keys(domain=emp.get("domain") or "", name=emp["canonical_name"]),
            company_function_limit=self.max_contacts,
        )
        self.conn.commit()
        if hits:
            return self._close(opportunity_id, f"suppressed:{hits[0]}"[:200])
        try:
            emp = self._ensure_employer_facts(opp, emp)
        except ProviderWait as w:
            return QualifyOutcome(opportunity_id, "wait", w.reason, details={"until": w.until.isoformat()})
        except ProviderRetry as r:
            return QualifyOutcome(opportunity_id, "retry", f"apollo_{r}")
        if not emp.get("domain"):
            return self._close(opportunity_id, "employer_domain_unresolved")
        if emp.get("agency_flag") is True:
            return self._close(opportunity_id, "employer_is_agency")
        ind = excluded_industry(str(emp.get("industry") or ""))
        if ind:
            return self._close(opportunity_id, f"employer_excluded_industry:{ind}")
        count = emp.get("employee_count")
        if count is not None and count < int(rule("min_employees")):
            return self._close(opportunity_id, "employer_too_small")
        if count is not None and count > int(rule("max_employees")):
            return self._close(opportunity_id, "employer_too_large")
        if not resolve_campaign_id(opp["function_key"], count, self.campaign_env):
            return self._wait_for_dependency(
                opportunity_id, f"configuration_pending:no_campaign_for_size:{opp['function_key']}"
            )

        founder_allowed = count is not None and count <= int(rule("founder_fallback_max_employees"))
        titles = buyer_titles(opp["function_key"], founder_allowed=founder_allowed)
        employer_domains = {emp["domain"]} | self._alias_domains(emp["id"])
        # The operational attempt allowance scales with the contact quota; the
        # evidence and approval gates themselves are unchanged for every person.
        max_attempts = int(rule("max_match_attempts_per_evidence_epoch")) * self.max_contacts
        judged, approved_refs, suppressed_emails = self._excluded_refs(opportunity_id)
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM candidate_attempts WHERE opportunity_id = %s AND attempt_kind = 'match' "
                        "AND outcome NOT IN ('refused', 'uncertain') AND epoch = %s", (opportunity_id, epoch))
            matches_done = int(cur.fetchone()["n"])
            approved_rows: List[Dict[str, Any]] = []
            if self.max_contacts > 1:
                cur.execute(
                    "SELECT a.id, lower(p.email) AS email, p.title, p.apollo_person_id, p.linkedin_url "
                    "FROM approvals a JOIN people p ON p.id = a.person_id "
                    "WHERE a.opportunity_id = %s AND a.state <> 'revoked'",
                    (opportunity_id,),
                )
                approved_rows = [dict(row) for row in cur.fetchall()]
        self.conn.commit()
        approved_total = len(approved_rows)
        existing_contacts: Set[str] = set()
        if self.max_contacts > 1:
            existing_contacts = company_function_contact_keys(self.conn, company_keys)
            existing_contacts.update(
                f"email:{row['email']}" if row.get("email") else f"approval:{row['id']}"
                for row in approved_rows
            )
            self.conn.commit()
        # Task 9: how many contacts (and which personas) this opportunity already
        # holds -- the single source of truth ``wants_more_contacts`` and
        # ``select_next_contact`` decide against for the rest of this call.
        state = _build_contact_state(existing_contacts, approved_rows, opp["function_key"])
        if not state.wants_more_contacts(self.max_contacts):
            self._finalize_approved(opportunity_id)
            return QualifyOutcome(
                opportunity_id, "approved", "contact_quota_already_reached",
                details={"approvals_created": 0, "approved_total": approved_total},
            )

        approval_ids: List[int] = []
        approved_person_ids: List[int] = []

        # 1) reuse a person already verified at this employer within TTL -- through the SAME gates (R04)
        reuse = self._reusable_person(emp, titles, founder_allowed, approved_refs, suppressed_emails, employer_domains, opportunity_id, epoch)
        if reuse is not None:
            decision = self._approve(opp, emp, posting, classification, reuse)
            if isinstance(decision, ApprovedLead):
                approval_id = self._commit_approval(opp, int(reuse["id"]), decision)
                self._record_attempt(opportunity_id, reuse["_ref"], "gate", "pass", "reused_verified_person", epoch=epoch, person_id=int(reuse["id"]),
                                     details={"rule_version": RULE_VERSION})
                approval_ids.append(approval_id)
                approved_person_ids.append(int(reuse["id"]))
                approved_refs.add(reuse["_ref"])
                state.add(reuse["_ref"], contact_persona(str(reuse.get("title") or ""), opp["function_key"]))
                # Phase 2 audit task 9 fix (2026-09-19): this used to finalize
                # (terminal 'approved' state) whenever max_contacts == 1 -- correct
                # for that case, but the SAME unconditional finalize also ran
                # further down (below) whenever ANY approval existed, regardless
                # of whether the configured quota (default 3) was met. That is
                # the measured defect: 86/86 approved opportunities had exactly
                # one contact, and the terminal state meant no later call could
                # ever pursue the rest. Finalizing now always goes through
                # ``state.wants_more_contacts`` instead of a bare max_contacts==1
                # check, so it is correct for every configured quota.
                if not state.wants_more_contacts(self.max_contacts):
                    self._finalize_approved(opportunity_id)
                    return QualifyOutcome(
                        opportunity_id, "approved", "reused_verified_person",
                        approval_id, int(reuse["id"]),
                        details={"approvals_created": 1, "approved_total": approved_total + 1},
                    )
            elif decision.reason in CONFIGURATION_LEVEL_REFUSALS:
                return self._wait_for_dependency(
                    opportunity_id, f"configuration_pending:{decision.reason}"
                )
            elif decision.reason in OPPORTUNITY_LEVEL_REFUSALS:
                return self._close(opportunity_id, f"approval_refused:{decision.reason}")

        # 2) candidate discovery (0-credit search), with the org-id fallback on no USABLE candidate
        try:
            usable, dropped, stats = self._search_candidates(opp, emp, titles, judged | approved_refs)
            candidates = self._rank(usable, titles)
            for cand in candidates:
                cand["persona"] = contact_persona(str(cand.get("title") or ""), opp["function_key"])
                cand["person_key"] = cand["_ref"]
        except ProviderWait as w:
            return QualifyOutcome(opportunity_id, "wait", w.reason, details={"until": w.until.isoformat()})
        except ProviderRetry as r:
            return QualifyOutcome(opportunity_id, "retry", f"apollo_{r}")
        for p in dropped:
            if p["_ref"] not in judged:
                self._record_attempt(opportunity_id, p["_ref"], "gate", "skipped_pre_enrichment", p["_drop_reason"], epoch=epoch,
                                     details={"title": p.get("title"), "org": person_organization(p).get("name")})
        if not candidates:
            if approval_ids or approved_total:
                # Task 9 / fix round 1, I5 / fix round 2: at least one contact
                # exists for this opportunity (from this call, a prior one, or
                # both), and this pass found ZERO candidates at all -- that is
                # itself "no further candidates" (fix round 2: give-up must key
                # on no progress, not on a paid-attempt budget that a candidate-
                # less pass never advances), so finalize with what was found
                # rather than waiting on a search that just came back empty.
                return self._finalize_or_wait_partial(
                    opportunity_id, state=state, approval_ids=approval_ids, approved_person_ids=approved_person_ids,
                    approved_total=approved_total, matches_done=matches_done, made=0, max_attempts=max_attempts,
                    no_further_candidates=True,
                    reason_if_quota_met="approved_available_contacts", wait_reason="buyer_search_pending:contact_quota_partial",
                )
            if stats["returned"] == 0:
                reason = "no_candidates_found"
            elif any(p["_ref"] not in judged | approved_refs for p in dropped):
                # Search-only rejection is not a paid judgement. Do not report
                # exhausted candidate history when a new person failed this gate.
                reason = "no_usable_candidates_in_search"
            else:
                reason = "no_unjudged_candidates_remaining"
            # A capped search is not evidence that Apollo has no more buyers.
            # Keep the opportunity open and retry later as Apollo's index changes.
            return self._wait_for_dependency(
                opportunity_id, f"buyer_search_pending:{reason}",
                hours=max(24.0, self.retry_hours),
                details={"search_coverage": stats, "absence_proven": False},
            )

        made = 0
        remaining = list(candidates)
        # Fix round 2, CRITICAL (independent review): whether THIS pass hit a
        # structural wall -- real candidates were still in ``remaining``, but
        # none had a persona not already held, so ``select_next_contact``
        # (I4's hard stop) returned ``None``. Set ONLY there: a pass that
        # simply runs out of candidates because it successfully used them all
        # up (``remaining`` empties via consumption, no ``break``) is not the
        # same signal -- real paid attempts were made, so ``matches_done``
        # keeps advancing across repeated calls, and a later search may still
        # find a genuinely new person. The quota-met break and the
        # attempt-budget break are different, unrelated reasons to stop and
        # also do not set this.
        no_further_candidates = False
        while remaining:
            if not state.wants_more_contacts(self.max_contacts):
                break
            if matches_done + made >= max_attempts:
                break
            # Task 9: choose the next candidate for role diversity and global
            # person-uniqueness, not the fixed title-list order ``_rank`` alone
            # gives every caller (the base ordering ``_rank`` provides still
            # decides ties WITHIN a persona).
            cand = select_next_contact(candidates=remaining, existing_person_keys=state.existing_person_keys,
                                       existing_personas=state.existing_personas)
            if cand is None:
                no_further_candidates = True
                break
            remaining = [c for c in remaining if c["_ref"] != cand["_ref"]]
            ref = cand["_ref"]
            # R01: judge only what the search carries; LinkedIn/employment/email come with enrichment.
            pre = pre_enrichment_check(person=cand, employer_name=emp["canonical_name"], employer_domains=employer_domains,
                                       buyer_titles=titles, founder_allowed=founder_allowed)
            if not pre.passed:
                self._record_attempt(opportunity_id, ref, "gate", "skipped_pre_enrichment", pre.reason, epoch=epoch, details=pre.evidence)
                continue
            try:
                self._guard_provider()
            except ProviderWait as w:
                return QualifyOutcome(opportunity_id, "wait", w.reason, attempts_made=made, details={"until": w.until.isoformat()})
            pid = str(cand.get("id") or cand.get("person_id") or "")
            attempt_id = self._intent("person_match", opportunity_id=opportunity_id, person_ref_value=ref, params={"id": pid}, estimated_credits=1)
            result = self.apollo.match_person(pid)
            self._finish(attempt_id, result, operation="person_match", estimated=1)
            try:
                self._handle_global(result, allow_not_found=True)
            except ProviderWait as w:
                self._record_attempt(opportunity_id, ref, "match", "refused", w.reason, epoch=epoch, attempt_id=attempt_id)
                return QualifyOutcome(opportunity_id, "wait", w.reason, attempts_made=made, details={"until": w.until.isoformat()})
            except ProviderRetry as r:
                self._record_attempt(opportunity_id, ref, "match", "uncertain", str(r), epoch=epoch, attempt_id=attempt_id)
                return QualifyOutcome(opportunity_id, "retry", f"apollo_{r}", attempts_made=made)
            made += 1
            if not result.served or not (result.data.get("person") or {}):
                outcome = "not_found" if (result.served or result.status == 404) else result.outcome.value
                self._record_attempt(opportunity_id, ref, "match", outcome, result.message or "no_person_in_response", epoch=epoch, attempt_id=attempt_id)
                continue
            enriched = dict(result.data["person"])
            enriched.setdefault("id", pid)
            contact, email = self._gate_enriched(enriched, emp, titles, founder_allowed, employer_domains)
            person_id = self._upsert_person(enriched, emp, email_alignment=str(email.evidence.get("alignment") or ""),
                                            employment_verified=contact.passed)
            self._record_attempt(opportunity_id, ref, "match", "served", "enriched", epoch=epoch, person_id=person_id, attempt_id=attempt_id)
            if not contact.passed:
                self._record_attempt(opportunity_id, ref, "gate", "fail", contact.reason, epoch=epoch, person_id=person_id, details=contact.evidence)
                continue
            if not email.passed:
                self._record_attempt(opportunity_id, ref, "gate", "fail", email.reason, epoch=epoch, person_id=person_id, details=email.evidence)
                continue
            if str(enriched.get("email") or "").strip().lower() in suppressed_emails:
                self._record_attempt(opportunity_id, ref, "gate", "fail", "suppressed:person_email", epoch=epoch, person_id=person_id)
                continue
            person_row = self._person_row(person_id)
            person_row.update({"contact_gate_passed": True, "contact_gate_reason": contact.reason, "email_gate_passed": True,
                               "email_alignment": email.evidence.get("alignment", "")})
            decision = self._approve(opp, emp, posting, classification, person_row)
            if isinstance(decision, ApprovalRefusal):
                self._record_attempt(opportunity_id, ref, "gate", "fail", f"approval_refused:{decision.reason}", epoch=epoch,
                                     person_id=person_id, details=decision.details)
                if decision.reason in CONFIGURATION_LEVEL_REFUSALS:
                    return self._wait_for_dependency(
                        opportunity_id, f"configuration_pending:{decision.reason}"
                    )
                if decision.reason in OPPORTUNITY_LEVEL_REFUSALS:
                    return self._close(opportunity_id, f"approval_refused:{decision.reason}")
                continue
            try:
                approval_id = self._commit_approval(opp, person_id, decision)
            except psycopg.errors.UniqueViolation:
                self.conn.rollback()
                self._record_attempt(opportunity_id, ref, "gate", "fail", "person_already_approved_elsewhere", epoch=epoch, person_id=person_id)
                continue
            self._record_attempt(opportunity_id, ref, "gate", "pass", "approved", epoch=epoch, person_id=person_id,
                                 details={"persona": cand.get("persona", ""), "rule_version": RULE_VERSION})
            approval_ids.append(approval_id)
            approved_person_ids.append(person_id)
            state.add(ref, cand.get("persona", ""))

        if approval_ids or approved_total:
            # Task 9 / fix round 1, I5 / fix round 2: finalize (quota met, no
            # further role-diverse candidate could be selected, or the
            # epoch's attempt budget is exhausted with real candidates still
            # untried), or wait and retry.
            return self._finalize_or_wait_partial(
                opportunity_id, state=state, approval_ids=approval_ids, approved_person_ids=approved_person_ids,
                approved_total=approved_total, matches_done=matches_done, made=made, max_attempts=max_attempts,
                no_further_candidates=no_further_candidates,
                reason_if_quota_met="approved", wait_reason="buyer_search_pending:contact_quota_partial",
            )

        if matches_done + made >= max_attempts:
            return self._close(opportunity_id, f"no_verified_buyer_after_max_attempts:epoch_{epoch}")
        return self._wait_for_dependency(
            opportunity_id, "buyer_search_pending:no_verified_buyer_in_candidates",
            hours=max(24.0, self.retry_hours),
            details={"absence_proven": False, "attempts_made": made},
        )

    def _alias_domains(self, employer_id: int) -> Set[str]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT alias_value FROM employer_aliases WHERE employer_id = %s AND alias_kind = 'domain'", (employer_id,))
            out = {r["alias_value"] for r in cur.fetchall()}
        self.conn.commit()
        return out

    def _person_row(self, person_id: int) -> Dict[str, Any]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM people WHERE id = %s", (person_id,))
            row = dict(cur.fetchone())
        self.conn.commit()
        return row

    def _reusable_person(self, emp: Dict[str, Any], titles: Sequence[str], founder_allowed: bool, approved_refs: Set[str],
                         suppressed: Set[str], employer_domains: Set[str], opportunity_id: int, epoch: int) -> Optional[Dict[str, Any]]:
        """A stored, employment-verified person with a verified email inside the TTL is a
        candidate for reuse -- and is judged by the SAME gates as a fresh enrichment,
        from the evidence stored with them (R04). A rejection is recorded like any other."""
        ttl = timedelta(days=int(rule("person_evidence_ttl_days")))
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM people WHERE employer_id = %s AND email_status = 'verified' AND email_verified_at >= %s ORDER BY email_verified_at DESC",
                        (emp["id"], self.now() - ttl))
            rows = [dict(r) for r in cur.fetchall()]
        self.conn.commit()
        for row in rows:
            ref = person_ref(apollo_person_id=row.get("apollo_person_id"), linkedin_url=row.get("linkedin_url"))
            if ref in approved_refs or str(row.get("email") or "").lower() in suppressed:
                continue
            facts = row.get("facts_json") if isinstance(row.get("facts_json"), dict) else {}
            evidence = dict(facts.get("enriched") or {})
            record = {**evidence, "id": row.get("apollo_person_id"), "first_name": row.get("first_name"), "last_name": row.get("last_name"),
                      "title": row.get("title") or evidence.get("title"), "linkedin_url": row.get("linkedin_url"),
                      "email": row.get("email"), "email_status": row.get("email_status")}
            contact, email = self._gate_enriched(record, emp, titles, founder_allowed, employer_domains)
            if not contact.passed or not email.passed:
                reason = contact.reason if not contact.passed else email.reason
                self._record_attempt(opportunity_id, ref, "gate", "fail", f"reuse_rejected:{reason}", epoch=epoch, person_id=int(row["id"]),
                                     details={"source": "stored_evidence"})
                continue
            row.update({"_ref": ref, "contact_gate_passed": True, "contact_gate_reason": "reused_verified_person",
                        "email_gate_passed": True, "email_alignment": email.evidence.get("alignment", "")})
            return row
        return None
