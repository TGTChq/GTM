"""Resolve one stored opportunity into its Wave 1 outbound decision.

    record -> campaign -> account assignment -> signal -> scope -> proof -> offer
           -> render -> QA -> experiment metadata

A record assigned to Control A comes back with no rendered copy at all: the live
Instantly sequence is the control and this package never touches it.

Nothing in this module performs I/O beyond reading the static claim registry
once. It is safe to run over a whole approved queue with zero provider calls and
zero writes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .assignment import (
    ARM_A,
    ARM_B,
    ARM_NONE,
    Assignment,
    account_assignment,
    company_assignment_key,
)
from .campaigns import (
    OFFER_CLASS_BY_TYPE,
    OFFER_TESTING_OVERVIEW,
    PROOF_ECONOMICS,
    PROOF_REMOTE_READINESS,
    PROOF_TESTING_MECHANICS,
    TIER_1,
    TIER_2,
    CampaignPolicy,
    campaign_for_bucket,
)
from .claims import ClaimRegistry, load_claim_registry, resolve_role_page
from .evidence import MAX_FOCUS_ITEMS_IN_EMAIL, read_focus_evidence, render_evidence_list
from .qa import audit_arm_consistency, run_qa_gates
from .render import COPY_VERSION, RenderInputs, render_sequence, to_html
from .scope import derive_scope_combination
from .signals import (
    EMPTY_COMPANY_CONTEXT,
    CompanyContext,
    open_role_titles,
    posting_is_eligible,
    resolve_signal,
)
from .timing import schedule_as_dicts, sequence_schedule

DEFAULT_EXPERIMENT_ID = "outbound_wave1_challenger_v1"

#: Arm label used when a record stays on the live control sequence.
CONTROL_COPY_NOTE = "control_arm_uses_live_instantly_campaign_copy"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _first_name(fields: Dict) -> str:
    full = _text(fields.get("Hiring Manager"))
    return full.split(" ", 1)[0].strip() if full else ""


@dataclass
class Wave1Resolution:
    """The complete Wave 1 decision for one opportunity record."""

    # --- identity -----------------------------------------------------------
    record_id: str = ""
    lead_key: str = ""
    company: str = ""
    role: str = ""
    canonical_role: str = ""
    role_bucket: str = ""
    role_confidence: str = ""
    company_confidence: str = ""

    # --- experiment ---------------------------------------------------------
    experiment_id: str = ""
    experiment_arm: str = ARM_A
    company_assignment_key: str = ""
    assignment_bucket: int = -1
    assignment_reason: str = ""
    campaign: str = ""
    campaign_key: str = ""
    campaign_id: str = ""

    # --- signal -------------------------------------------------------------
    signal_tier: str = ""
    signal_type: str = ""
    signal_evidence: Dict[str, Any] = field(default_factory=dict)
    signal_evidence_required: bool = False
    degrade_reasons: List[str] = field(default_factory=list)
    opening_count: int = 0
    job_age_days: Optional[int] = None
    usable_evidence_count: int = 0
    evidence_list: str = ""
    evidence_item_count: int = 0
    scope_combination: str = ""
    scope_list: str = ""
    scope_item_count: int = 0

    # --- proof / offer ------------------------------------------------------
    friction_angle: str = ""
    proof_type: str = ""
    claim_source: str = ""
    claim_id: str = ""
    verified_claim_text: str = ""
    unverified_claim_rendered: bool = False
    outbound_offer_type: str = ""
    offer_noun: str = ""
    offer_class: str = ""
    offer_fallback_type: str = "none"
    role_page_match: bool = False
    role_page_url: str = ""
    role_page_reason: str = ""
    economics_role: str = ""

    # --- copy ---------------------------------------------------------------
    copy_version: str = COPY_VERSION
    rendered_subject: str = ""
    rendered_email_1: str = ""
    rendered_email_2: str = ""
    rendered_email_3: str = ""
    rendered_email_4: str = ""
    #: The same four emails in the HTML shape an Instantly campaign body needs.
    #: The plain text above stays the auditable canonical output.
    rendered_email_1_html: str = ""
    rendered_email_2_html: str = ""
    rendered_email_3_html: str = ""
    rendered_email_4_html: str = ""
    e1_segments: Dict[str, str] = field(default_factory=dict)
    send_schedule: List[Dict[str, Any]] = field(default_factory=list)

    # --- QA -----------------------------------------------------------------
    #: Passed the structural pre-render checks (campaign known, fields present,
    #: posting still eligible).
    eligible: bool = True
    ineligible_reason: str = ""
    qa_pass: bool = False
    qa_reasons: List[str] = field(default_factory=list)
    #: In the experiment at all. False means SUPPRESSED -- the record is not in
    #: arm A, not in arm B, and not in either denominator.
    wave1_eligible: bool = False
    suppression_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_custom_variables(self) -> Dict[str, str]:
        """Instantly custom variables for a Challenger enrolment.

        The rendered bodies are final text: the Challenger campaign template is
        ``{{rendered_email_N}}`` plus the account signature, so Instantly performs
        no second rendering pass over anything this package produced.
        """
        payload = {
            "experiment_id": self.experiment_id,
            "experiment_arm": self.experiment_arm,
            "company_assignment_key": self.company_assignment_key,
            "wave1_campaign": self.campaign,
            "signal_tier": self.signal_tier,
            "signal_type": self.signal_type,
            "friction_angle": self.friction_angle,
            "proof_type": self.proof_type,
            "claim_source": self.claim_source,
            "outbound_offer_type": self.outbound_offer_type,
            "offer_noun": self.offer_noun,
            "offer_class": self.offer_class,
            "offer_fallback_type": self.offer_fallback_type,
            "copy_version": self.copy_version,
            "role_page_match": "1" if self.role_page_match else "0",
            "rendered_subject": self.rendered_subject,
            "rendered_email_1": self.rendered_email_1,
            "rendered_email_2": self.rendered_email_2,
            "rendered_email_3": self.rendered_email_3,
            "rendered_email_4": self.rendered_email_4,
            # The Challenger campaign body uses the _html variants; Instantly
            # bodies are HTML and would collapse the plain-text paragraphs.
            "rendered_email_1_html": self.rendered_email_1_html,
            "rendered_email_2_html": self.rendered_email_2_html,
            "rendered_email_3_html": self.rendered_email_3_html,
            "rendered_email_4_html": self.rendered_email_4_html,
        }
        return {key: value for key, value in payload.items() if value not in (None, "")}


def _ineligible(
    base: Wave1Resolution, reason: str, *, policy: Optional[CampaignPolicy] = None
) -> Wave1Resolution:
    base.eligible = False
    base.ineligible_reason = reason
    base.qa_pass = False
    base.qa_reasons = [reason]
    return base


def _resolve_proof_and_offer(
    policy: CampaignPolicy,
    *,
    registry: ClaimRegistry,
    canonical_role: str,
    display_role: str,
) -> Dict[str, Any]:
    """Pick the strongest proof/offer pair the registry actually licenses."""
    matched, page, page_reason = resolve_role_page(
        registry, canonical_role=canonical_role, display_role=display_role
    )
    out: Dict[str, Any] = {
        "role_page_match": matched,
        "role_page_url": page.url if page else "",
        "role_page_reason": page_reason,
        "economics_role": "",
        "claim_source": "",
        "claim_id": "",
        "verified_claim_text": "",
        "strong_claim_text": "",
        "local_comparison_published": False,
        "offer_fallback_type": "none",
    }

    proof = policy.preferred_proof
    offer = policy.preferred_offer

    if proof == PROOF_ECONOMICS:
        if matched and page is not None and page.can_quote_economics:
            out["economics_role"] = page.canonical_role
            out["claim_source"] = page.claim_source
            out["claim_id"] = f"role_page:{page.canonical_role}"
            out["local_comparison_published"] = page.local_comparison_published
        else:
            out["offer_fallback_type"] = offer
            proof, offer = policy.fallback_proof, policy.fallback_offer

    if proof == PROOF_REMOTE_READINESS:
        claim = registry.claim(policy.strong_claim_id)
        if claim is not None and claim.usable:
            out["verified_claim_text"] = claim.text
            out["claim_source"] = claim.claim_source
            out["claim_id"] = claim.claim_id
        else:
            # No verified claim -> the campaign has no remote-readiness proof at
            # all, so it degrades to the safe, source-free process statement.
            out["offer_fallback_type"] = offer
            proof, offer = PROOF_TESTING_MECHANICS, OFFER_TESTING_OVERVIEW

    if proof == PROOF_TESTING_MECHANICS and policy.strong_claim_id:
        claim = registry.claim(policy.strong_claim_id)
        if claim is not None and claim.usable:
            out["strong_claim_text"] = claim.text
            out["claim_source"] = claim.claim_source
            out["claim_id"] = claim.claim_id

    out["proof_type"] = proof
    out["outbound_offer_type"] = offer
    out["offer_class"] = OFFER_CLASS_BY_TYPE.get(offer, "")
    return out


def resolve_challenger(
    fields: Dict,
    *,
    experiment_id: str = DEFAULT_EXPERIMENT_ID,
    registry: Optional[ClaimRegistry] = None,
    company: CompanyContext = EMPTY_COMPANY_CONTEXT,
    assignment: Optional[Assignment] = None,
    record_id: str = "",
    as_of: Optional[datetime] = None,
    sequence_start: Optional[date | datetime | str] = None,
) -> Wave1Resolution:
    """Render and QA the Challenger for one record, regardless of its arm.

    ``resolve_wave1`` calls this for arm-B records. The dry run also calls it for
    arm-A records so every campaign can be reviewed, without changing what those
    records would actually be sent.
    """
    claim_registry = registry if registry is not None else load_claim_registry()
    bucket = _text(fields.get("Role Bucket")).lower()
    result = Wave1Resolution(
        record_id=record_id or _text(fields.get("id")),
        lead_key=_text(fields.get("Lead Key")),
        company=_text(fields.get("Outbound Company")) or _text(fields.get("Company")),
        role=_text(fields.get("Outbound Role")) or _text(fields.get("Open Role")),
        canonical_role=_text(fields.get("Matched Role")),
        role_bucket=bucket,
        role_confidence=_text(fields.get("Outbound Role Confidence")),
        company_confidence=_text(fields.get("Outbound Company Confidence")),
        experiment_id=experiment_id,
        experiment_arm=ARM_B,
        campaign_id=_text(fields.get("Campaign ID")),
        copy_version=COPY_VERSION,
    )
    assigned = assignment or account_assignment(fields, experiment_id=experiment_id)
    result.company_assignment_key = assigned.key
    result.assignment_bucket = assigned.bucket
    result.assignment_reason = assigned.reason

    policy = campaign_for_bucket(bucket)
    if policy is None:
        return _ineligible(result, f"role_bucket_not_in_wave1:{bucket or 'blank'}")
    result.campaign = policy.name
    result.campaign_key = policy.key

    first_name = _first_name(fields)
    missing = [
        name for name, value in (
            ("first_name", first_name),
            ("outbound_company", result.company),
            ("outbound_role", result.role),
        ) if not value
    ]
    if missing:
        return _ineligible(result, "missing_required_render_field:" + ",".join(missing))

    eligible, eligibility_reason = posting_is_eligible(fields)
    if not eligible:
        return _ineligible(result, f"posting_not_eligible:{eligibility_reason}")

    focus = read_focus_evidence(fields)
    scope = derive_scope_combination(policy, focus)
    signal = resolve_signal(
        fields, policy=policy, focus=focus, scope=scope, company=company, as_of=as_of
    )

    result.signal_tier = signal.tier
    result.signal_type = signal.signal_type
    result.signal_evidence = dict(signal.evidence)
    result.signal_evidence_required = signal.tier in (TIER_1, TIER_2)
    result.degrade_reasons = list(signal.degrade_reasons)
    result.opening_count = signal.opening_count or len(open_role_titles(fields))
    result.job_age_days = signal.job_age_days
    result.usable_evidence_count = focus.usable_count

    # Evidence and scope lists are capped by what the row genuinely supports:
    # never more items than usable evidence, never more than three in an email.
    evidence_items = focus.renderable(limit=MAX_FOCUS_ITEMS_IN_EMAIL)
    result.evidence_list = render_evidence_list(evidence_items)
    result.evidence_item_count = len(evidence_items)
    scope_limit = min(len(scope.facets), focus.usable_count, MAX_FOCUS_ITEMS_IN_EMAIL)
    scope_items = tuple(scope.facets[:scope_limit]) if scope.sufficient else ()
    result.scope_list = render_evidence_list(scope_items)
    result.scope_item_count = len(scope_items)
    result.scope_combination = " + ".join(scope_items)

    proof = _resolve_proof_and_offer(
        policy,
        registry=claim_registry,
        canonical_role=result.canonical_role,
        display_role=result.role,
    )
    result.proof_type = proof["proof_type"]
    result.outbound_offer_type = proof["outbound_offer_type"]
    result.offer_class = proof["offer_class"]
    result.offer_fallback_type = proof["offer_fallback_type"]
    result.claim_source = proof["claim_source"]
    result.claim_id = proof["claim_id"]
    result.verified_claim_text = proof["verified_claim_text"]
    result.role_page_match = proof["role_page_match"]
    result.role_page_url = proof["role_page_url"]
    result.role_page_reason = proof["role_page_reason"]
    result.economics_role = proof["economics_role"] or (
        result.role if proof["proof_type"] == PROOF_ECONOMICS else ""
    )

    evidence = signal.evidence
    rendered = render_sequence(
        policy,
        signal_type=signal.signal_type,
        proof_type=result.proof_type,
        offer_type=result.outbound_offer_type,
        data=RenderInputs(
            first_name=first_name,
            company=result.company,
            role=result.role,
            economics_role=result.economics_role,
            opening_count=result.opening_count,
            cross_function_count=int(evidence.get("function_count") or 0),
            cross_function_openings=int(evidence.get("total_openings") or 0),
            job_age_days=result.job_age_days,
            evidence_list=result.evidence_list,
            scope_list=result.scope_list,
            audience=policy.audience_by_bucket.get(bucket, ""),
            verified_claim_text=result.verified_claim_text,
            strong_claim_text=proof["strong_claim_text"],
            local_comparison_published=bool(proof["local_comparison_published"]),
        ),
    )
    result.rendered_subject = rendered.subject
    (
        result.rendered_email_1,
        result.rendered_email_2,
        result.rendered_email_3,
        result.rendered_email_4,
    ) = rendered.emails
    (
        result.rendered_email_1_html,
        result.rendered_email_2_html,
        result.rendered_email_3_html,
        result.rendered_email_4_html,
    ) = tuple(to_html(body) for body in rendered.emails)
    result.friction_angle = rendered.friction_angle
    result.offer_noun = rendered.offer_noun
    result.e1_segments = dict(rendered.e1_segments)
    result.send_schedule = schedule_as_dicts(sequence_schedule(sequence_start))

    result.qa_pass, result.qa_reasons = run_qa_gates(result.to_dict(), policy=policy)
    return result


def _control_from(challenger: Wave1Resolution, assigned: Assignment) -> Wave1Resolution:
    """A control-arm resolution for a record that passed the SHARED gate.

    It keeps the record's classification -- campaign, signal tier, proof, offer,
    friction -- because those describe the opportunity, not the arm. That is what
    lets the analysis stratify control the same way it stratifies treatment. The
    rendered copy is dropped: Control A is the live Instantly sequence and nothing
    here produces it.
    """
    control = replace(
        challenger,
        experiment_arm=ARM_A,
        assignment_bucket=assigned.bucket,
        assignment_reason=assigned.reason,
        copy_version="control-a-live-instantly-copy",
        rendered_subject="",
        rendered_email_1="",
        rendered_email_2="",
        rendered_email_3="",
        rendered_email_4="",
        rendered_email_1_html="",
        rendered_email_2_html="",
        rendered_email_3_html="",
        rendered_email_4_html="",
        e1_segments={},
        send_schedule=[],
        wave1_eligible=True,
        suppression_reason="",
        qa_pass=True,
        qa_reasons=[CONTROL_COPY_NOTE],
    )
    return control


def resolve_wave1_pair(
    fields: Dict,
    *,
    experiment_id: str = DEFAULT_EXPERIMENT_ID,
    b_split_pct: int = 50,
    salt: str = "",
    registry: Optional[ClaimRegistry] = None,
    company: CompanyContext = EMPTY_COMPANY_CONTEXT,
    record_id: str = "",
    as_of: Optional[datetime] = None,
    sequence_start: Optional[date | datetime | str] = None,
) -> Tuple[Wave1Resolution, Wave1Resolution]:
    """Resolve one record, returning ``(outcome, challenger_render)``.

    The challenger render is returned alongside the outcome so a reviewer can read
    the copy a control-arm record WOULD have received, without re-deriving it.

    Eligibility comes FIRST and is arm-independent: the challenger is rendered and
    QA'd before anything is randomised. Only records that clear that shared gate
    are assigned to A or B, so the two arms are drawn from one identical eligible
    population.

    A record that fails the gate is SUPPRESSED -- ``wave1_eligible=False``,
    ``experiment_arm=NONE``, ``suppression_reason`` set. It is never relabelled A.
    """
    # Phase 1: shared eligibility, computed without reference to any arm.
    challenger = resolve_challenger(
        fields,
        experiment_id=experiment_id,
        registry=registry,
        company=company,
        record_id=record_id,
        as_of=as_of,
        sequence_start=sequence_start,
    )
    assigned = account_assignment(
        fields, experiment_id=experiment_id, b_split_pct=b_split_pct, salt=salt
    )
    challenger.assignment_bucket = assigned.bucket
    challenger.assignment_reason = assigned.reason

    suppression: List[str] = []
    if not challenger.eligible:
        suppression.append(challenger.ineligible_reason or "record_not_wave1_eligible")
    elif not challenger.qa_pass:
        suppression.extend(challenger.qa_reasons)
    if not assigned.assignable:
        suppression.append(assigned.reason)

    if suppression:
        challenger.wave1_eligible = False
        challenger.experiment_arm = ARM_NONE
        challenger.suppression_reason = ";".join(dict.fromkeys(suppression))
        challenger.qa_pass = False
        return challenger, challenger

    # Phase 2: randomise the eligible record.
    challenger.wave1_eligible = True
    if assigned.arm == ARM_B:
        challenger.experiment_arm = ARM_B
        return challenger, challenger
    return _control_from(challenger, assigned), challenger


def resolve_wave1(
    fields: Dict,
    *,
    experiment_id: str = DEFAULT_EXPERIMENT_ID,
    b_split_pct: int = 50,
    salt: str = "",
    registry: Optional[ClaimRegistry] = None,
    company: CompanyContext = EMPTY_COMPANY_CONTEXT,
    record_id: str = "",
    as_of: Optional[datetime] = None,
    sequence_start: Optional[date | datetime | str] = None,
) -> Wave1Resolution:
    """Resolve one record's actual Wave 1 outcome. See ``resolve_wave1_pair``."""
    outcome, _challenger = resolve_wave1_pair(
        fields,
        experiment_id=experiment_id,
        b_split_pct=b_split_pct,
        salt=salt,
        registry=registry,
        company=company,
        record_id=record_id,
        as_of=as_of,
        sequence_start=sequence_start,
    )
    return outcome


def build_company_index(
    records: Sequence[Dict],
    *,
    experiment_id: str = DEFAULT_EXPERIMENT_ID,
) -> Dict[str, CompanyContext]:
    """Company-level facts for the batch, keyed by account key.

    Cross-bucket signals read this. It is derived only from the records being
    resolved -- no lookup, no provider call.
    """
    buckets: Dict[str, Dict[str, Any]] = {}
    for record in records:
        fields = record.get("fields") if isinstance(record, dict) and "fields" in record else record
        fields = fields or {}
        key = company_assignment_key(fields)
        if not key:
            continue
        bucket = _text(fields.get("Role Bucket")).lower()
        if not bucket:
            continue
        entry = buckets.setdefault(key, {"openings": {}, "roles": {}})
        titles = open_role_titles(fields)
        known = set(entry["roles"].get(bucket, ()))
        known.update(titles)
        entry["roles"][bucket] = tuple(sorted(known))
        entry["openings"][bucket] = len(known)
    return {
        key: CompanyContext(
            buckets=tuple(sorted(entry["openings"])),
            openings_by_bucket=dict(entry["openings"]),
            roles_by_bucket=dict(entry["roles"]),
            indexed=True,
        )
        for key, entry in buckets.items()
    }


def resolve_batch(
    records: Sequence[Dict],
    *,
    experiment_id: str = DEFAULT_EXPERIMENT_ID,
    b_split_pct: int = 50,
    salt: str = "",
    registry: Optional[ClaimRegistry] = None,
    as_of: Optional[datetime] = None,
    sequence_start: Optional[date | datetime | str] = None,
    challenger_preview: bool = False,
) -> Tuple[List[Wave1Resolution], List[Wave1Resolution], List[str]]:
    """Resolve a whole batch.

    Returns ``(resolutions, challenger_previews, batch_qa_failures)``. The
    previews exist only so a reviewer can inspect Challenger copy for records
    that were assigned to Control A; they are never enrolled.
    """
    claim_registry = registry if registry is not None else load_claim_registry()
    index = build_company_index(records, experiment_id=experiment_id)

    resolutions: List[Wave1Resolution] = []
    previews: List[Wave1Resolution] = []
    for record in records:
        if isinstance(record, dict) and "fields" in record:
            fields = record.get("fields") or {}
            record_id = _text(record.get("id"))
        else:
            fields = record or {}
            record_id = _text(fields.get("id"))
        key = company_assignment_key(fields)
        company = index.get(key, EMPTY_COMPANY_CONTEXT)
        resolution, challenger = resolve_wave1_pair(
            fields,
            experiment_id=experiment_id,
            b_split_pct=b_split_pct,
            salt=salt,
            registry=claim_registry,
            company=company,
            record_id=record_id,
            as_of=as_of,
            sequence_start=sequence_start,
        )
        resolutions.append(resolution)
        if challenger_preview and resolution.experiment_arm == ARM_A:
            previews.append(challenger)

    failures = audit_arm_consistency([item.to_dict() for item in resolutions])
    return resolutions, previews, failures
