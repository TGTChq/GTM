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
    OFFER_REMOTE_READINESS_OVERVIEW,
    OFFER_ROLE_ECONOMICS,
    OFFER_TESTING_OVERVIEW,
    PROOF_ECONOMICS,
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
    "product": (
        FRICTION_SCREENING_BANDWIDTH,
        "Running that many product searches at once usually stretches whoever is "
        "doing the first pass.",
    ),
    "ecommerce": (
        FRICTION_SCREENING_BANDWIDTH,
        "Running that many ecommerce searches at once usually stretches whoever is "
        "doing the first pass.",
    ),
    "customer_experience": (
        FRICTION_SCREENING_BANDWIDTH,
        "Running that many searches at once usually stretches whoever is doing the "
        "first pass.",
    ),
    "people_hr": (
        FRICTION_CROSS_FUNCTION_SCREENING,
        "Hiring across that many functions usually concentrates the screening load, "
        "wherever that sits.",
    ),
    "operations": (
        FRICTION_MULTI_AREA_SHORTLIST,
        "Postings that span more than one area usually take longer to fill, because "
        "the shortlist has to clear all of it.",
    ),
    "ai_technical": (
        FRICTION_MULTI_AREA_SHORTLIST,
        "Postings that span more than one area usually take longer to fill, because "
        "the shortlist has to clear all of it.",
    ),
    "finance": (
        FRICTION_COMBINED_SCOPE_POOL,
        "A combined scope like that usually narrows the pool more than the title "
        "suggests.",
    ),
    "marketing_creative": (
        FRICTION_COMBINED_SCOPE_POOL,
        "A combined scope like that usually narrows the pool more than the title "
        "suggests.",
    ),
    "gtm_systems": (
        FRICTION_COMBINED_SCOPE_POOL,
        "A combined scope like that usually narrows the pool more than the title "
        "suggests.",
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
    return _FRICTION_BY_CAMPAIGN.get(policy.key, _T3_FRICTION)


# ---------------------------------------------------------------------------
# Proof lines
# ---------------------------------------------------------------------------

#: The safe, source-free process statement. It describes what TGTC does; it does
#: NOT assert a proprietary, campaign-specific assessment product.
SAFE_TESTING_MECHANICS = (
    "We test candidates on role-specific work rather than relying on the title or "
    "tool list alone."
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
    proof = _proof_line(proof_type, data)
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
