"""Challenger B copy.

Every email is assembled from labelled segments so the QA gates can inspect what
was generated rather than guessing at a finished blob. Nothing here interpolates
a merge variable into the output: the record's own values are substituted at
render time, so the rendered text that leaves this module is final. The Instantly
side of a Challenger campaign is therefore just ``{{rendered_email_N}}`` plus the
account signature, and there is no second, unaudited rendering pass.

E2/E3/E4 are frozen by the Wave 1 specification and appear here verbatim. Only
``offer_noun`` and ``role`` are substituted into them, and ``offer_noun`` is
inherited from E1 so the offer cannot change mid-thread.
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .campaigns import (
    PLURAL_OFFER_NOUNS,
    OFFER_EMPLOYMENT_ADMIN_OVERVIEW,
    OFFER_HEADCOUNT_OVERVIEW,
    OFFER_REMOTE_READINESS_OVERVIEW,
    OFFER_ROLE_ECONOMICS,
    OFFER_TESTING_OVERVIEW,
    PROOF_ECONOMICS,
    PROOF_EMPLOYMENT_ADMIN,
    PROOF_HEADCOUNT_MODEL,
    PROOF_REMOTE_READINESS,
    PROOF_TESTING_MECHANICS,
    SIGNAL_ACTIVE_REQ,
    SIGNAL_JOB_AGE,
    SIGNAL_MULTI_OPENING,
    SIGNAL_MULTI_OPENING_CROSS_BUCKET,
    SIGNAL_ROLE_FOCUS_MATCH,
    SIGNAL_SCOPE_COMBINATION,
    CampaignPolicy,
)

COPY_VERSION = "outbound-wave1-challenger/1"

# --- friction angles --------------------------------------------------------
FRICTION_SCREENING_BANDWIDTH = "screening_bandwidth"
FRICTION_CROSS_FUNCTION_SCREENING = "cross_function_screening"
FRICTION_MULTI_AREA_SHORTLIST = "multi_area_shortlist"
FRICTION_COMBINED_SCOPE_POOL = "combined_scope_pool"
FRICTION_TIME_OPEN = "time_open"
FRICTION_PARALLEL_POOL = "parallel_pool"
#: Campaign-specific frictions. Each one states the reason THIS buyer has for
#: caring, which is what makes the nine arguments different rather than nine
#: wordings of one argument. All are hedged observations about the reader's own
#: situation ("usually", "almost nothing"), never asserted facts about their team.
FRICTION_APPROVAL_SEQUENCING = "approval_sequencing"
FRICTION_TITLE_VS_SCOPE = "title_vs_scope"
FRICTION_EMPLOYMENT_OBLIGATION = "employment_obligation"
FRICTION_TITLE_INFLATION = "title_inflation"
FRICTION_INTERVIEW_WEAK_SIGNAL = "interview_weak_signal"
FRICTION_SCOPE_COMPRESSION = "scope_compression"
FRICTION_RARE_INTERSECTION = "rare_intersection"
FRICTION_NO_TRACK_RECORD_YET = "no_track_record_yet"
#: Used ONLY on the economics path. It must never survive a degrade to a
#: testing offer -- a cost-framed friction paired with "how we test" is
#: incoherent, and a QA gate rejects that pairing.
FRICTION_COST_COMPARISON = "cost_comparison"


@dataclass(frozen=True)
class RenderInputs:
    """Everything the copy is allowed to reference. Nothing else is in scope."""

    first_name: str
    company: str
    role: str
    #: Canonical role the economics claim belongs to (equals ``role`` when an
    #: exact role-page match was made; empty otherwise).
    economics_role: str = ""
    opening_count: int = 0
    cross_function_count: int = 0
    cross_function_openings: int = 0
    job_age_days: Optional[int] = None
    #: Grammatical list built from however many usable evidence items exist.
    evidence_list: str = ""
    #: Grammatical list of derived scope facets.
    scope_list: str = ""
    #: "customer support" / "customer success" for the CX campaign only.
    audience: str = ""
    #: Verified claim text (e.g. the remote-readiness wording), when licensed.
    verified_claim_text: str = ""
    #: A verified campaign-specific proof sentence from the claim registry. When
    #: absent (the shipped state) the safe, source-free process statement is used.
    strong_claim_text: str = ""
    local_comparison_published: bool = False


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    body: str
    #: Labelled segments, for QA inspection.
    segments: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RenderedSequence:
    subject: str
    emails: Tuple[str, str, str, str]
    friction_angle: str
    offer_noun: str
    #: Segments of E1 only; E2-E4 are frozen text.
    e1_segments: Dict[str, str] = field(default_factory=dict)


def _cap(text: str) -> str:
    """Capitalise a sentence-initial noun phrase without touching the rest."""
    return text[:1].upper() + text[1:] if text else text


def _paragraphs(*parts: str) -> str:
    return "\n\n".join(part.strip() for part in parts if part and part.strip())


#: Instantly campaign bodies are HTML. A plain-text value interpolated into one
#: loses every paragraph break, because HTML collapses newlines to whitespace --
#: verified against the live workspace, whose campaign bodies are
#: ``<div>para</div><div><br /></div><div>para</div>``. So each rendered email is
#: ALSO published in that exact shape.
#:
#: This is a transport conversion, not a copy decision. It escapes, wraps and
#: joins; it adds no word, link, image or attribute of its own, and ``html_to_text``
#: proves the round trip so a QA gate can assert nothing was introduced.
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
_TAG_RE = re.compile(r"</?div>")


def to_html(text: str) -> str:
    """Convert one rendered plain-text email to the campaign body's HTML shape."""
    paragraphs = [
        part.strip() for part in _PARAGRAPH_SPLIT.split(str(text or "")) if part.strip()
    ]
    if not paragraphs:
        return ""
    blocks: List[str] = []
    for index, paragraph in enumerate(paragraphs):
        if index:
            blocks.append("<div><br /></div>")
        escaped = "<br />".join(
            _html.escape(line.strip(), quote=False)
            for line in paragraph.split("\n") if line.strip()
        )
        blocks.append(f"<div>{escaped}</div>")
    return "".join(blocks)


def html_to_text(markup: str) -> str:
    """Inverse of :func:`to_html`. Used by QA to prove the conversion is faithful."""
    text = str(markup or "")
    text = text.replace("<div><br /></div>", "\n\n")
    text = text.replace("<br />", "\n")
    text = _TAG_RE.sub("", text)
    return _html.unescape(text).strip()


# ---------------------------------------------------------------------------
# Signal lines
# ---------------------------------------------------------------------------

def _signal_line(
    policy: CampaignPolicy, signal_type: str, data: RenderInputs
) -> str:
    if signal_type == SIGNAL_MULTI_OPENING:
        # The count is ALWAYS the record's real opening count -- never a
        # hard-coded "two".
        noun = data.audience or _campaign_role_noun(policy)
        return f"{data.opening_count} {noun} roles open at {data.company} at once."
    if signal_type == SIGNAL_MULTI_OPENING_CROSS_BUCKET:
        return (
            f"{data.company} has {data.cross_function_openings} openings across "
            f"{data.cross_function_count} functions."
        )
    if signal_type == SIGNAL_ROLE_FOCUS_MATCH:
        return (
            f"The {data.role} opening at {data.company} reads like it covers "
            f"{data.evidence_list}."
        )
    if signal_type == SIGNAL_SCOPE_COMBINATION:
        return (
            f"The {data.role} opening at {data.company} looks like it combines "
            f"{data.scope_list}."
        )
    if signal_type == SIGNAL_JOB_AGE:
        return (
            f"The {data.role} opening at {data.company} has been up for about "
            f"{data.job_age_days} days."
        )
    if signal_type == SIGNAL_ACTIVE_REQ:
        return f"Saw {data.company} is hiring for {data.role}."
    return ""  # pragma: no cover - signal table is closed


#: Plain-language noun for a campaign's own openings. Deliberately generic --
#: it describes the function, never a seniority or a proprietary category.
_CAMPAIGN_ROLE_NOUN: Dict[str, str] = {
    "product": "product",
    "ecommerce": "ecommerce",
    "operations": "operations",
    "finance": "finance",
    "people_hr": "people",
    "marketing_creative": "marketing",
    "gtm_systems": "revenue",
    "ai_technical": "technical",
}


def _campaign_role_noun(policy: CampaignPolicy) -> str:
    return _CAMPAIGN_ROLE_NOUN.get(policy.key, "open")


# ---------------------------------------------------------------------------
# Friction lines (conservative and conditional -- never a stated fact about the
# reader's team, its size, or its performance)
# ---------------------------------------------------------------------------

_FRICTION_BY_CAMPAIGN: Dict[str, Tuple[str, str]] = {
    # Approvals, not candidate supply, are what hold a multi-req product team up.
    "product": (
        FRICTION_APPROVAL_SEQUENCING,
        "Getting several approved at the same time can be the harder half of it.",
    ),
    # An ops title is a container for whatever the company piled into it.
    "operations": (
        FRICTION_TITLE_VS_SCOPE,
        "An ops title on its own may not say much about whether someone has run "
        "that particular mix.",
    ),
    # A controller reads a hire as an administrative and compliance obligation.
    "finance": (
        FRICTION_EMPLOYMENT_OBLIGATION,
        "Whoever fills it is also a payroll, tax and benefits obligation that "
        "someone has to administer.",
    ),
    "people_hr": (
        FRICTION_CROSS_FUNCTION_SCREENING,
        "Hiring across that many functions usually concentrates the screening load, "
        "wherever that sits.",
    ),
    # The closest to OPERATIONS, and knowingly so -- see the ECOMMERCE policy.
    "ecommerce": (
        FRICTION_TITLE_INFLATION,
        "Ecommerce titles can cover very different work depending on the size of "
        "the store behind them.",
    ),
    # The one reason only CX can use: the interview is the least diagnostic
    # signal precisely because talking to people is the job.
    "customer_experience": (
        FRICTION_INTERVIEW_WEAK_SIGNAL,
        "People who are good at these roles are often good in an interview too, "
        "which can make the interview itself a weaker signal than usual.",
    ),
    # A very wide marketing scope usually means one approved line covering several
    # jobs -- a budget shape, not a hiring preference.
    "marketing_creative": (
        FRICTION_SCOPE_COMPRESSION,
        "A scope that wide can mean one approved line is being asked to cover "
        "several jobs.",
    ),
    # In RevOps the parts are common and the join is rare.
    "gtm_systems": (
        FRICTION_RARE_INTERSECTION,
        "Each of those is common on its own. It is the combination that tends to "
        "be rarer, and the part a CV is least likely to evidence.",
    ),
    # Nothing in this category is old enough for tenure to mean anything.
    "ai_technical": (
        FRICTION_NO_TRACK_RECORD_YET,
        "Tooling this new has not been around long enough for track records in it "
        "to be long, so years of experience on a CV may say less than usual here.",
    ),
}

_ECONOMICS_FRICTION = (
    FRICTION_COST_COMPARISON,
    "Working out what that actually costs against a local hire is usually the slow "
    "part.",
)

_T2_FRICTION = (
    FRICTION_TIME_OPEN,
    "That usually means the shortlist has not produced the right fit yet, rather "
    "than that the search stopped.",
)

_T3_FRICTION = (
    FRICTION_PARALLEL_POOL,
    "If the shortlist has not produced the right fit yet, a second pool running "
    "alongside it is usually the cheapest way to keep moving.",
)

#: Frictions whose wording depends on the signal that fired. PRODUCT's approval
#: line reads off a multi-opening count and PEOPLE & HR's off a cross-bucket
#: count, so neither may be rendered behind a different signal.
_FRICTION_REQUIRES_SIGNAL: Dict[str, frozenset] = {
    FRICTION_APPROVAL_SEQUENCING: frozenset({SIGNAL_MULTI_OPENING}),
    FRICTION_CROSS_FUNCTION_SCREENING: frozenset({SIGNAL_MULTI_OPENING_CROSS_BUCKET}),
    FRICTION_TITLE_INFLATION: frozenset({SIGNAL_MULTI_OPENING}),
    FRICTION_INTERVIEW_WEAK_SIGNAL: frozenset({SIGNAL_MULTI_OPENING}),
    FRICTION_SCOPE_COMPRESSION: frozenset({SIGNAL_SCOPE_COMBINATION}),
    FRICTION_RARE_INTERSECTION: frozenset({SIGNAL_SCOPE_COMBINATION}),
    FRICTION_EMPLOYMENT_OBLIGATION: frozenset({SIGNAL_SCOPE_COMBINATION}),
    FRICTION_TITLE_VS_SCOPE: frozenset({SIGNAL_ROLE_FOCUS_MATCH}),
    FRICTION_NO_TRACK_RECORD_YET: frozenset({SIGNAL_ROLE_FOCUS_MATCH}),
}


def _friction(policy: CampaignPolicy, signal_type: str, proof_type: str) -> Tuple[str, str]:
    """Friction is resolved AFTER the proof, so a degrade changes the whole triple.

    When a campaign cannot support economics it does not merely swap the proof and
    the offer: the cost-framed friction goes with them, and the record gets the
    scope/bandwidth friction that actually leads into "we test candidates on
    role-specific work".
    """
    if proof_type == PROOF_ECONOMICS:
        return _ECONOMICS_FRICTION
    if signal_type == SIGNAL_JOB_AGE:
        return _T2_FRICTION
    if signal_type == SIGNAL_ACTIVE_REQ:
        return _T3_FRICTION
    campaign_friction = _FRICTION_BY_CAMPAIGN.get(policy.key)
    if campaign_friction is None:
        return _T3_FRICTION
    # A campaign friction that reads off the signal cannot be used when that
    # signal did not fire. PRODUCT's "getting them all approved" only makes sense
    # behind a multi-opening count, so a degraded signal takes the generic
    # friction instead of asserting something the record does not support.
    angle, _text = campaign_friction
    if angle in _FRICTION_REQUIRES_SIGNAL and signal_type not in _FRICTION_REQUIRES_SIGNAL[angle]:
        return _T3_FRICTION
    return campaign_friction


# ---------------------------------------------------------------------------
# Proof lines
# ---------------------------------------------------------------------------

#: The safe, source-free process statement. It describes what TGTC does; it does
#: NOT assert a proprietary, campaign-specific assessment product.
SAFE_TESTING_MECHANICS = (
    "We test candidates on role-specific work rather than relying on the title or "
    "tool list alone."
)

#: Placed people join the client's team full-time and are NOT on the client's
#: official headcount. A verified fact about the offer, stated plainly and with
#: no implied speed, price or quality claim.
HEADCOUNT_MODEL_PROOF = (
    "Ours join your team full-time without going onto your official headcount."
)

#: TGTC carries payroll, taxes, benefits, compliance and HR administration. Also
#: verified, and deliberately worded as what WE carry rather than what the reader
#: saves, so it states a fact and not an outcome.
EMPLOYMENT_ADMIN_PROOF = (
    "With ours that sits with us: payroll, taxes, benefits, and the compliance "
    "and HR administration around them."
)


def _proof_line(proof_type: str, data: RenderInputs) -> str:
    if proof_type == PROOF_ECONOMICS:
        role = data.economics_role or data.role
        if data.local_comparison_published:
            return (
                f"We publish the monthly cost for {role} against typical local cost, "
                "so you can see what the economics look like for this exact scope."
            )
        return (
            f"We publish the monthly cost for {role}, so you can see what the "
            "economics look like for this exact scope."
        )
    if proof_type == PROOF_REMOTE_READINESS:
        return (
            f"Every candidate clears a {data.verified_claim_text} before we send "
            "them."
        )
    if proof_type == PROOF_HEADCOUNT_MODEL:
        return HEADCOUNT_MODEL_PROOF
    if proof_type == PROOF_EMPLOYMENT_ADMIN:
        return EMPLOYMENT_ADMIN_PROOF
    # A campaign may state something stronger than the generic process claim only
    # when the claim registry carries a VERIFIED sentence for it.
    return data.strong_claim_text or SAFE_TESTING_MECHANICS


# ---------------------------------------------------------------------------
# Offer lines
# ---------------------------------------------------------------------------

def offer_noun(policy: CampaignPolicy, offer_type: str) -> str:
    """The exact words this campaign uses for this offer, resolved once."""
    return policy.offer_noun(offer_type)


def offer_question(noun: str) -> str:
    """E1 asks for the offer using the SAME words E2-E4 refer back to."""
    return f"Want me to send {noun}?"


# ---------------------------------------------------------------------------
# The four emails
# ---------------------------------------------------------------------------

def render_email_1(
    policy: CampaignPolicy,
    *,
    signal_type: str,
    proof_type: str,
    offer_type: str,
    data: RenderInputs,
    noun: str = "",
) -> RenderedEmail:
    signal = _signal_line(policy, signal_type, data)
    friction_angle, friction = _friction(policy, signal_type, proof_type)
    # A campaign may phrase a proof in its own words. The CLAIM is unchanged --
    # only the sentence carrying it differs, so the proof follows this campaign's
    # argument instead of reading as one template used nine times. A verified
    # registry claim still wins, because it carries its own source.
    proof = _proof_line(proof_type, data)
    if proof_type == PROOF_TESTING_MECHANICS and not data.strong_claim_text:
        proof = policy.proof_text(proof_type) or proof
    elif proof_type in (PROOF_HEADCOUNT_MODEL, PROOF_EMPLOYMENT_ADMIN):
        proof = policy.proof_text(proof_type) or proof
    offer = offer_question(noun or offer_noun(policy, offer_type))
    greeting = f"Hi {data.first_name},"
    body = _paragraphs(greeting, signal, friction, proof, offer)
    return RenderedEmail(
        subject=data.role,
        body=body,
        segments={
            "greeting": greeting,
            "signal": signal,
            "friction": friction,
            "proof": proof,
            "offer": offer,
            "friction_angle": friction_angle,
        },
    )


def render_email_2(noun: str) -> str:
    return _paragraphs(
        "Bumping this in case it slipped past.",
        f"Still happy to send {noun} if it's useful.",
    )


def render_email_3(noun: str) -> str:
    # The frozen sentence is "<noun> is still there if you want it first". A plural
    # noun ("the numbers for this role") disagrees with it, so the verb and the
    # pronoun are selected mechanically. Word choice is untouched.
    plural = noun in PLURAL_OFFER_NOUNS
    verb = "are" if plural else "is"
    pronoun = "them" if plural else "it"
    return _paragraphs(
        "One thing I left out.",
        "You don't pay anything until you've picked someone. If nobody on the "
        "shortlist clears your bar, you owe nothing and we keep looking.",
        f"{_cap(noun)} {verb} still there if you want {pronoun} first.",
    )


def render_email_4(noun: str, role: str) -> str:
    return _paragraphs(
        "Closing this out.",
        f"If the {role} search is handled, tell me and I'll stop.",
        f"If it's still open, say the word and I'll send {noun} today.",
    )


def render_sequence(
    policy: CampaignPolicy,
    *,
    signal_type: str,
    proof_type: str,
    offer_type: str,
    data: RenderInputs,
) -> RenderedSequence:
    """Render all four Challenger emails for one record."""
    noun = offer_noun(policy, offer_type)
    email_1 = render_email_1(
        policy,
        signal_type=signal_type,
        proof_type=proof_type,
        offer_type=offer_type,
        data=data,
        noun=noun,
    )
    return RenderedSequence(
        subject=email_1.subject,
        emails=(
            email_1.body,
            render_email_2(noun),
            render_email_3(noun),
            render_email_4(noun, data.role),
        ),
        friction_angle=str(email_1.segments.get("friction_angle") or ""),
        offer_noun=noun,
        e1_segments=dict(email_1.segments),
    )
