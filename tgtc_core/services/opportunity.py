"""Qualify one opportunity: employer facts, candidate discovery, selective paid
enrichment, real gates, deterministic approval + outbox in one transaction.

PRODUCT_CONTRACT §7-§8. The provider is called OUTSIDE any transaction; intent is
committed before every potentially chargeable request and the outcome after.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

import psycopg

from ..db.connection import jsonb, transaction
from ..domain.approval import ApprovalRefusal, ApprovedLead, airtable_fields, build_approved_lead, instantly_payload
from ..domain.gates import corroborated_alternate_domains, evaluate_contact, evaluate_email, person_organization, title_matches
from ..domain.identity import company_names_compatible, domain_name_consistent, person_ref, safe_employer_domain
from ..policy.campaigns import buyer_titles, is_founder_tier, resolve_campaign_id
from ..policy.requirements import excluded_industry, rule
from ..providers.apollo import ApolloClient, ApolloResult, Outcome, person_org_domain
from . import provider_state
from .suppression import account_keys, check as suppression_check, company_function_keys


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


class OpportunityService:
    def __init__(self, conn: psycopg.Connection, apollo: ApolloClient, *, campaign_env: Dict[str, str],
                 signing_key: str, retry_hours: float = 6.0, people_search_max_pages: int = 2,
                 person_uniqueness: bool = True, verify_on_import: bool = False,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.conn = conn
        self.apollo = apollo
        self.campaign_env = campaign_env
        self.signing_key = signing_key
        self.retry_hours = retry_hours
        self.search_pages = max(1, people_search_max_pages)
        self.person_uniqueness = person_uniqueness
        self.verify_on_import = verify_on_import
        self.now = now
        self._pass_permitted = False

    # --- helpers ------------------------------------------------------------
    def _load(self, opportunity_id: int) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
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
                WHERE op.opportunity_id = %s AND p.state <> 'closed' AND p.state <> 'expired'
                  AND NOT c.excluded AND %s = ANY (c.compatible_functions)
                ORDER BY p.commercial_age_anchor DESC LIMIT 1
                """,
                (opportunity_id, opp["function_key"]),
            )
            posting = cur.fetchone()
        self.conn.commit()
        return dict(opp), dict(emp), dict(posting) if posting else {}, dict(posting["classification_json"]) if posting else {}

    def _close(self, opportunity_id: int, reason: str) -> QualifyOutcome:
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("UPDATE opportunities SET state = 'closed', close_reason = %s, updated_at = now() WHERE id = %s",
                            (reason[:200], opportunity_id))
        return QualifyOutcome(opportunity_id, "closed", reason)

    def _intent(self, operation: str, *, opportunity_id: int, person_ref_value: str = "", params: Dict[str, Any],
                estimated_credits: Optional[float]) -> int:
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO request_attempts (provider, operation, opportunity_id, person_ref, params_json, estimated_credits) "
                    "VALUES ('apollo', %s, %s, %s, %s, %s) RETURNING id",
                    (operation, opportunity_id, person_ref_value or None, jsonb(params), estimated_credits),
                )
                return int(cur.fetchone()["id"])

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

    def _guard_provider(self) -> None:
        """One controlled attempt per qualification pass against a refusing provider.

        The first guard in a pass decides; later guards in the SAME pass reuse that
        decision, so marking the attempt does not re-block the pass that was permitted.
        A pass that is permitted may make its search and one paid call; it never loops.
        """
        if self._pass_permitted:
            return
        state = provider_state.may_attempt(self.conn, "apollo", retry_hours=self.retry_hours, now=self.now())
        if not state["allowed"]:
            raise ProviderWait(f"apollo_{state['state']}_until_{state['next_attempt_after'].isoformat()}", state["next_attempt_after"])
        if state["state"] in (provider_state.REFUSING, provider_state.UNAUTHORIZED):
            provider_state.mark_attempt(self.conn, "apollo", now=self.now())
        self._pass_permitted = True

    def _handle_global(self, result: ApolloResult, *, chargeable: bool = True) -> None:
        """Refusals/throttles/timeouts raise; a served CHARGEABLE call records recovery.

        A served 0-credit search proves reachability, not credits, so it never flips a
        refusing provider back to serving.
        """
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
        facts: Dict[str, Any] = {"apollo_found": bool(org)}
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                if org:
                    apollo_domain = safe_employer_domain(org.get("primary_domain") or org.get("domain") or org.get("website_url"))
                    name_ok = company_names_compatible(emp["canonical_name"], str(org.get("name") or ""))
                    domain_ok = bool(emp.get("domain") and apollo_domain and apollo_domain == emp["domain"])
                    trusted = domain_ok or (name_ok and (not emp.get("domain") or not apollo_domain))
                    if emp.get("domain") and apollo_domain and apollo_domain != emp["domain"]:
                        # Different domain for the same name: record, never overwrite the observed identity.
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
                        facts["apollo_untrusted"] = True
                        cur.execute("UPDATE employers SET facts_json = facts_json || %s, enriched_at = %s, updated_at = now() WHERE id = %s",
                                    (jsonb({"apollo_untrusted_match": str(org.get("name") or "")}), self.now(), emp["id"]))
                else:
                    cur.execute("UPDATE employers SET facts_json = facts_json || %s, enriched_at = %s, updated_at = now() WHERE id = %s",
                                (jsonb({"apollo": "not_found"}), self.now(), emp["id"]))
                cur.execute("SELECT * FROM employers WHERE id = %s", (emp["id"],))
                return dict(cur.fetchone())

    # --- candidates -------------------------------------------------------------
    def _search_candidates(self, opp: Dict[str, Any], emp: Dict[str, Any], titles: Sequence[str]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Returns (candidates, dropped). ``dropped`` are search results whose organization is
        provably a different company; they are recorded, never silently discarded."""
        people: List[Dict[str, Any]] = []
        dropped: List[Dict[str, Any]] = []
        for selector in (("domain", emp["domain"]), ("organization_id", emp.get("apollo_org_id") or "")):
            if not selector[1]:
                continue
            for page in range(1, self.search_pages + 1):
                self._guard_provider()
                params = {selector[0]: selector[1], "titles": list(titles), "page": page}
                attempt_id = self._intent("people_search", opportunity_id=opp["id"], params=params, estimated_credits=0)
                kwargs = {"titles": list(titles), "page": page}
                kwargs[selector[0]] = selector[1]
                result = self.apollo.search_people(**kwargs)
                self._finish(attempt_id, result, operation="people_search", estimated=0)
                self._handle_global(result, chargeable=False)
                if not result.served:
                    break
                page_people = [p for p in (result.data.get("people") or []) if isinstance(p, dict)]
                for p in page_people:
                    pd = person_org_domain(p)
                    if pd and pd != emp["domain"] and not pd.endswith("." + emp["domain"]):
                        # Wrong-company guard (legacy-proven), refined: a different org DOMAIN is
                        # tolerated only when the organization is corroborated as this employer
                        # (compatible name or the same Apollo organization id). The email gate then
                        # decides alignment. Anything else is a different company: dropped, recorded.
                        org = person_organization(p)
                        corroborated = company_names_compatible(emp["canonical_name"], str(org.get("name") or "")) or (
                            emp.get("apollo_org_id") and str(org.get("id") or "") == str(emp.get("apollo_org_id")))
                        if not corroborated:
                            p["_drop_reason"] = f"contact:wrong_organization_search_guard:{pd}"
                            dropped.append(p)
                            continue
                    people.append(p)
                pagination = result.data.get("pagination") if isinstance(result.data.get("pagination"), dict) else {}
                total_pages = pagination.get("total_pages")
                if len(page_people) < 25 or (isinstance(total_pages, int) and page >= total_pages):
                    break
            if people:
                break
        # dedupe by id, keep first
        seen: Set[str] = set()
        out: List[Dict[str, Any]] = []
        for p in people + dropped:
            ref = person_ref(apollo_person_id=str(p.get("id") or p.get("person_id") or ""), linkedin_url=p.get("linkedin_url"),
                             name=f"{p.get('first_name','')} {p.get('last_name','')}", employer_key=f"domain:{emp['domain']}")
            if ref and ref not in seen:
                seen.add(ref)
                p["_ref"] = ref
                if "_drop_reason" not in p:
                    out.append(p)
        return out, [p for p in dropped if "_ref" in p]

    def _rank(self, candidates: List[Dict[str, Any]], titles: Sequence[str]) -> List[Dict[str, Any]]:
        def rank(p: Dict[str, Any]) -> int:
            t = str(p.get("title") or "")
            for i, target in enumerate(titles):
                if title_matches(t, [target]):
                    return i
            return len(titles) + (5 if is_founder_tier(t) else 0)
        return sorted(candidates, key=rank)

    def _excluded_refs(self, opportunity_id: int) -> Tuple[Set[str], Set[str], Set[str]]:
        """(attempted for this opportunity, active approvals by apollo id, suppressed emails)"""
        with self.conn.cursor() as cur:
            # A provider refusal or a lost response is not an attempt AGAINST the candidate:
            # the same person is retried once the provider serves again.
            cur.execute(
                "SELECT candidate_ref FROM candidate_attempts WHERE opportunity_id = %s AND ("
                "(attempt_kind = 'match' AND outcome NOT IN ('refused', 'uncertain')) OR attempt_kind = 'gate')",
                (opportunity_id,))
            attempted = {r["candidate_ref"] for r in cur.fetchall()}
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
        return attempted, approved, suppressed

    def _record_attempt(self, opportunity_id: int, ref: str, kind: str, outcome: str, reason: str,
                        person_id: Optional[int] = None, attempt_id: Optional[int] = None, details: Optional[Dict[str, Any]] = None) -> None:
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO candidate_attempts (opportunity_id, person_id, candidate_ref, attempt_kind, outcome, reason, attempt_id, details) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (opportunity_id, candidate_ref, attempt_kind) DO UPDATE SET "
                    "outcome = EXCLUDED.outcome, reason = EXCLUDED.reason, person_id = COALESCE(EXCLUDED.person_id, candidate_attempts.person_id), details = EXCLUDED.details, created_at = now()",
                    (opportunity_id, person_id, ref, kind, outcome, reason[:200], attempt_id, jsonb(details or {})),
                )

    def _upsert_person(self, enriched: Dict[str, Any], emp: Dict[str, Any], *, email_alignment: str) -> int:
        org = person_organization(enriched)
        email = str(enriched.get("email") or "").strip().lower() or None
        status = str(enriched.get("email_status") or enriched.get("contact_email_status") or "").lower() or None
        with transaction(self.conn):
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
                     enriched.get("title"), emp["id"], org.get("name") or enriched.get("organization_name"),
                     person_org_domain(enriched) or None, email, status, "apollo" if email else None,
                     self.now() if status == "verified" else None, jsonb({"email_alignment": email_alignment, "headline": enriched.get("headline")})),
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
        """Approval + both outbox items in ONE transaction."""
        lead = approved.lead
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO approvals (opportunity_id, person_id, employer_id, campaign_key, function_key, campaign_id, policy_version,
                                           lead_key, fingerprint, lead_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                    """,
                    (opp["id"], person_id, opp["employer_id"], lead["campaign_key"], lead["function_key"], lead["campaign_id"],
                     lead["policy_version"], approved.lead_key, approved.fingerprint, jsonb(lead)),
                )
                approval_id = int(cur.fetchone()["id"])
                cur.execute("UPDATE opportunities SET state = 'approved', approved_person_id = %s, close_reason = NULL, updated_at = now() WHERE id = %s",
                            (person_id, opp["id"]))
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

    # --- main -------------------------------------------------------------------
    def process(self, opportunity_id: int) -> QualifyOutcome:
        self._pass_permitted = False
        opp, emp, posting, classification = self._load(opportunity_id)
        if opp["state"] != "open":
            return QualifyOutcome(opportunity_id, "closed", f"already_{opp['state']}")
        if not posting:
            return self._close(opportunity_id, "no_active_compatible_posting")
        # company × function suppression (imported history) closes before any spend
        hits = suppression_check(
            self.conn,
            company_function=company_function_keys(domain=emp.get("domain") or "", name=emp["canonical_name"],
                                                   slug=emp.get("linkedin_slug") or "", function_key=opp["function_key"]),
            account=account_keys(domain=emp.get("domain") or "", name=emp["canonical_name"]),
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

        founder_allowed = count is not None and count <= int(rule("founder_fallback_max_employees"))
        titles = buyer_titles(opp["function_key"], founder_allowed=founder_allowed)
        employer_domains = {emp["domain"]} | self._alias_domains(emp["id"])
        max_attempts = int(rule("max_match_attempts_per_opportunity"))
        attempted, approved_refs, suppressed_emails = self._excluded_refs(opportunity_id)
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM candidate_attempts WHERE opportunity_id = %s AND attempt_kind = 'match' "
                        "AND outcome NOT IN ('refused', 'uncertain')", (opportunity_id,))
            matches_done = int(cur.fetchone()["n"])
        self.conn.commit()

        # 1) reuse a person already verified at this employer within TTL
        reuse = self._reusable_person(emp, titles, approved_refs, suppressed_emails, employer_domains)
        if reuse is not None:
            decision = self._approve(opp, emp, posting, classification, reuse)
            if isinstance(decision, ApprovedLead):
                approval_id = self._commit_approval(opp, int(reuse["id"]), decision)
                self._record_attempt(opportunity_id, reuse["_ref"], "gate", "pass", "reused_verified_person", person_id=int(reuse["id"]))
                return QualifyOutcome(opportunity_id, "approved", "reused_verified_person", approval_id, int(reuse["id"]))
            if decision.reason in {"suppressed", "posting_too_old", "posting_excluded", "employer_identity_incomplete",
                                   "employer_too_small", "employer_too_large", "employer_excluded_industry", "no_campaign_configured",
                                   "campaign_id_not_allowed", "signing_key_missing", "no_responsibility_evidence", "copy_fields_incomplete"}:
                return self._close(opportunity_id, f"approval_refused:{decision.reason}")

        # 2) candidate discovery (0-credit search)
        try:
            found, dropped = self._search_candidates(opp, emp, titles)
            candidates = self._rank(found, titles)
        except ProviderWait as w:
            return QualifyOutcome(opportunity_id, "wait", w.reason, details={"until": w.until.isoformat()})
        except ProviderRetry as r:
            return QualifyOutcome(opportunity_id, "retry", f"apollo_{r}")
        for p in dropped:
            if p["_ref"] not in attempted:
                self._record_attempt(opportunity_id, p["_ref"], "gate", "skipped_pre_enrichment", p["_drop_reason"],
                                     details={"title": p.get("title"), "org": person_organization(p).get("name")})
        if not candidates:
            return self._close(opportunity_id, "no_candidates_found")

        made = 0
        for cand in candidates:
            if matches_done + made >= max_attempts:
                break
            ref = cand["_ref"]
            if ref in attempted or ref in approved_refs:
                continue
            pre = evaluate_contact(person=cand, employer_name=emp["canonical_name"], employer_domains=employer_domains,
                                   buyer_titles=titles, founder_allowed=founder_allowed,
                                   require_linkedin=bool(rule("require_contact_linkedin")), require_current_employment=False)
            if not pre.passed:
                self._record_attempt(opportunity_id, ref, "gate", "skipped_pre_enrichment", pre.reason, details=pre.evidence)
                continue
            # paid match
            try:
                self._guard_provider()
            except ProviderWait as w:
                return QualifyOutcome(opportunity_id, "wait", w.reason, attempts_made=made, details={"until": w.until.isoformat()})
            pid = str(cand.get("id") or cand.get("person_id") or "")
            attempt_id = self._intent("person_match", opportunity_id=opportunity_id, person_ref_value=ref, params={"id": pid}, estimated_credits=1)
            result = self.apollo.match_person(pid)
            self._finish(attempt_id, result, operation="person_match", estimated=1)
            try:
                self._handle_global(result)
            except ProviderWait as w:
                self._record_attempt(opportunity_id, ref, "match", "refused", w.reason, attempt_id=attempt_id)
                return QualifyOutcome(opportunity_id, "wait", w.reason, attempts_made=made, details={"until": w.until.isoformat()})
            except ProviderRetry as r:
                self._record_attempt(opportunity_id, ref, "match", "uncertain", str(r), attempt_id=attempt_id)
                return QualifyOutcome(opportunity_id, "retry", f"apollo_{r}", attempts_made=made)
            made += 1
            if not result.served or not (result.data.get("person") or {}):
                outcome = "not_found" if (result.served or result.status == 404) else result.outcome.value
                self._record_attempt(opportunity_id, ref, "match", outcome, result.message or "no_person_in_response", attempt_id=attempt_id)
                continue
            enriched = dict(result.data["person"])
            enriched.setdefault("id", pid)
            # full gates on enriched evidence
            contact = evaluate_contact(person=enriched, employer_name=emp["canonical_name"], employer_domains=employer_domains,
                                       buyer_titles=titles, founder_allowed=founder_allowed,
                                       require_linkedin=bool(rule("require_contact_linkedin")),
                                       require_current_employment=bool(rule("require_current_employment_evidence")))
            apollo_org = (emp.get("facts_json") or {}).get("apollo") if isinstance(emp.get("facts_json"), dict) else None
            alt = corroborated_alternate_domains(employer_name=emp["canonical_name"], employer_domains=employer_domains,
                                                 person=enriched, apollo_org={"name": (apollo_org or {}).get("name"),
                                                                             "primary_domain": (apollo_org or {}).get("primary_domain"),
                                                                             "id": emp.get("apollo_org_id")} if apollo_org else None)
            email = evaluate_email(email=enriched.get("email"), email_status=enriched.get("email_status") or enriched.get("contact_email_status"),
                                   employer_domains=employer_domains, corroborated_domains=alt)
            person_id = self._upsert_person(enriched, emp, email_alignment=str(email.evidence.get("alignment") or ""))
            self._record_attempt(opportunity_id, ref, "match", "served", "enriched", person_id=person_id, attempt_id=attempt_id)
            if not contact.passed:
                self._record_attempt(opportunity_id, ref, "gate", "fail", contact.reason, person_id=person_id, details=contact.evidence)
                continue
            if not email.passed:
                self._record_attempt(opportunity_id, ref, "gate", "fail", email.reason, person_id=person_id, details=email.evidence)
                continue
            if str(enriched.get("email") or "").strip().lower() in suppressed_emails:
                self._record_attempt(opportunity_id, ref, "gate", "fail", "suppressed:person_email", person_id=person_id)
                continue
            person_row = self._person_row(person_id)
            person_row.update({"contact_gate_passed": True, "contact_gate_reason": contact.reason, "email_gate_passed": True,
                               "email_alignment": email.evidence.get("alignment", "")})
            decision = self._approve(opp, emp, posting, classification, person_row)
            if isinstance(decision, ApprovalRefusal):
                self._record_attempt(opportunity_id, ref, "gate", "fail", f"approval_refused:{decision.reason}", person_id=person_id, details=decision.details)
                if decision.reason in {"suppressed", "posting_too_old", "posting_excluded", "employer_identity_incomplete",
                                       "employer_too_small", "employer_too_large", "employer_excluded_industry", "no_campaign_configured",
                                       "campaign_id_not_allowed", "signing_key_missing", "no_responsibility_evidence", "copy_fields_incomplete"}:
                    return self._close(opportunity_id, f"approval_refused:{decision.reason}")
                continue
            try:
                approval_id = self._commit_approval(opp, person_id, decision)
            except psycopg.errors.UniqueViolation:
                self.conn.rollback()
                self._record_attempt(opportunity_id, ref, "gate", "fail", "person_already_approved_elsewhere", person_id=person_id)
                continue
            self._record_attempt(opportunity_id, ref, "gate", "pass", "approved", person_id=person_id)
            return QualifyOutcome(opportunity_id, "approved", "approved", approval_id, person_id, attempts_made=made)

        if matches_done + made >= max_attempts:
            return self._close(opportunity_id, "no_verified_buyer_after_max_attempts")
        return self._close(opportunity_id, "no_verified_buyer_in_candidates")

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

    def _reusable_person(self, emp: Dict[str, Any], titles: Sequence[str], approved_refs: Set[str], suppressed: Set[str],
                         employer_domains: Set[str]) -> Optional[Dict[str, Any]]:
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
            if not title_matches(str(row.get("title") or ""), titles):
                continue
            email = evaluate_email(email=row.get("email"), email_status=row.get("email_status"), employer_domains=employer_domains)
            if not email.passed:
                continue
            row.update({"_ref": ref, "contact_gate_passed": True, "contact_gate_reason": "reused_verified_person",
                        "email_gate_passed": True, "email_alignment": email.evidence.get("alignment", "")})
            return row
        return None
