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
#: Degraded-tier frictions. One shared friction could not work: T2/T3 keep the
#: campaign's proof, and a stalled-search friction does not lead into "they are
#: not on your headcount" or "we handle payroll". So the degraded friction is
#: chosen by the PROOF it has to hand off to, not by the tier.
FRICTION_MORE_CVS = "more_cvs"
FRICTION_SECOND_SEARCH_HEADCOUNT = "second_search_headcount"
FRICTION_ADMIN_ON_CLOSE = "admin_on_close"
#: Campaign-specific frictions. Each one states the reason THIS buyer has for
#: caring, which is what makes the nine arguments different rather than nine
#: wordings of one argument. All are hedged observations about the reader's own
#: situation ("usually", "almost nothing"), never asserted facts about their team.
FRICTION_HEADCOUNT_ADDITION = "headcount_addition"
FRICTION_TITLE_VS_SCOPE = "title_vs_scope"
FRICTION_EMPLOYMENT_OBLIGATION = "employment_obligation"
FRICTION_TITLE_INFLATION = "title_inflation"
FRICTION_INTERVIEW_WEAK_SIGNAL = "interview_weak_signal"
FRICTION_SCOPE_SPLIT = "scope_split"
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

#: Small counts read as words in a sentence; a numeral at the start of one is a
#: mail-merge tell. Anything larger stays a numeral, which is how people write.
_NUMBER_WORDS = {
    2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
    7: "seven", 8: "eight", 9: "nine", 10: "ten",
}


def rendered_count(value: int) -> str:
    """How a count appears in the copy. A QA gate uses this to prove the rendered
    number is the record's own, so the renderer and the gate cannot disagree."""
    return _NUMBER_WORDS.get(int(value or 0), str(value))


_count = rendered_count


def _signal_line(
    policy: CampaignPolicy, signal_type: str, data: RenderInputs
) -> str:
    """The one factual sentence: the real hiring event, read off the record.

    It deliberately does NOT say "the <role> opening at <their own company>".
    Naming someone's employer back at them is the most recognisable mail-merge
    shape there is, and "your <role> opening" is both shorter and what a person
    would actually type. The company name is kept only where the company itself
    is the subject of the observation.
    """
    if signal_type == SIGNAL_MULTI_OPENING:
        # The count is ALWAYS the record's real opening count -- never a
        # hard-coded "two".
        noun = data.audience or _campaign_role_noun(policy)
        return (
            f"{data.company} has {_count(data.opening_count)} {noun} roles open "
            "right now."
        )
    if signal_type == SIGNAL_MULTI_OPENING_CROSS_BUCKET:
        return (
            f"{data.company} has {_count(data.cross_function_openings)} roles open "
            f"across {_count(data.cross_function_count)} teams right now."
        )
    if signal_type == SIGNAL_ROLE_FOCUS_MATCH:
        return f"Your {data.role} opening reads like it covers {data.evidence_list}."
    if signal_type == SIGNAL_SCOPE_COMBINATION:
        return f"Your {data.role} opening looks like it combines {data.scope_list}."
    if signal_type == SIGNAL_JOB_AGE:
        return f"Your {data.role} opening has been up about {data.job_age_days} days."
    if signal_type == SIGNAL_ACTIVE_REQ:
        return f"Saw you're hiring for {data.role}."
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
    # Looks FORWARD, not back: the roles being open says nothing about whether
    # they were hard to approve, but it does mean more than one hire may land
    # at the same time, which is what the headcount proof answers.
    "product": (
        FRICTION_HEADCOUNT_ADDITION,
        "If {opening_subject} turn into hires, that's {opening_count} additions to headcount at once.",
    ),
    # An ops title is a container for whatever the company piled into it.
    "operations": (
        FRICTION_TITLE_VS_SCOPE,
        "The title on its own may not tell you much about who has actually run "
        "that mix.",
    ),
    # A controller reads a hire as an administrative and compliance obligation.
    "finance": (
        FRICTION_EMPLOYMENT_OBLIGATION,
        "A hire like that also comes with employment admin.",
    ),
    "people_hr": (
        FRICTION_CROSS_FUNCTION_SCREENING,
        "That's a lot of very different hires to run at the same time.",
    ),
    # The closest to OPERATIONS, and knowingly so -- see the ECOMMERCE policy.
    "ecommerce": (
        FRICTION_TITLE_INFLATION,
        "Ecommerce titles can mean very different work depending on the size of "
        "the store.",
    ),
    # An interview is incomplete evidence of day-to-day work. Note what this
    # does NOT say: nothing about how candidates for these roles usually
    # perform in interviews, which we have no basis to claim.
    "customer_experience": (
        FRICTION_INTERVIEW_WEAK_SIGNAL,
        "A good interview still doesn't tell you much about how someone "
        "handles the work day to day.",
    ),
    # States the hypothetical outright rather than inferring WHY the reader
    # scoped the role the way they did. The claim is scoped to our own
    # placements ("with us"), which is exactly what the verified fact covers.
    "marketing_creative": (
        FRICTION_SCOPE_SPLIT,
        "If you end up splitting that scope across more than one person, the "
        "second hire doesn't need another headcount slot with us.",
    ),
    # In RevOps the parts are common and the join is rare.
    "gtm_systems": (
        FRICTION_RARE_INTERSECTION,
        "Plenty of people do one of those. Fewer do the whole mix, and that's "
        "the part a CV struggles to show.",
    ),
    # Nothing in this category is old enough for tenure to mean anything.
    "ai_technical": (
        FRICTION_NO_TRACK_RECORD_YET,
        "The tooling is new enough that years on a CV may not tell you much "
        "here.",
    ),
}

_ECONOMICS_FRICTION = (
    FRICTION_COST_COMPARISON,
    "Working out what that actually costs against a local hire is usually the slow "
    "part.",
)

#: Degraded-tier friction by the proof it must hand off to.
#:
#: Job age is a FACT and the signal states it. Any reading of what that age
#: means is conditional here ("If the right person hasn't turned up yet"),
#: because a posting sitting open for 50 days does not prove a shortlist failed,
#: or that one exists. The old wording asserted exactly that, and the old T3
#: friction called a parallel search "usually the cheapest way to keep moving",
#: which is an economics claim nothing in this repository supports.
_DEGRADED_FRICTION_BY_PROOF: Dict[str, Tuple[str, str]] = {
    # -> "they don't go on your official headcount"
    PROOF_HEADCOUNT_MODEL: (
        FRICTION_SECOND_SEARCH_HEADCOUNT,
        "Running a second search alongside it usually means finding the headcount "
        "for it too.",
    ),
    # -> "we handle payroll, taxes, benefits and compliance"
    PROOF_EMPLOYMENT_ADMIN: (
        FRICTION_ADMIN_ON_CLOSE,
        "Whenever it does close, there's employment admin behind it.",
    ),
}

#: Used for the evidence proofs (role-specific testing, remote readiness), which
#: both answer "you have not seen the right person yet".
_DEGRADED_EVIDENCE_FRICTION = (
    FRICTION_MORE_CVS,
    "If the right person hasn't turned up yet, more CVs may not be what's missing.",
)

#: Campaign proof phrasings that refer back to the signal. GTM says "the
#: combination" and AI ends on a bare "instead" -- each only makes sense when
#: that campaign's own T1 signal fired.
#: Campaigns absent from this map phrase their proof self-containedly and may use
#: it at any tier.
_PROOF_TEXT_REQUIRES_SIGNAL: Dict[str, frozenset] = {
    "gtm_systems": frozenset({SIGNAL_SCOPE_COMBINATION}),
    "ai_technical": frozenset({SIGNAL_ROLE_FOCUS_MATCH}),
}


def _campaign_proof_text_applies(policy: CampaignPolicy, signal_type: str) -> bool:
    allowed = _PROOF_TEXT_REQUIRES_SIGNAL.get(policy.key)
    return allowed is None or signal_type in allowed


#: Frictions whose wording depends on the signal that fired. PRODUCT's approval
#: line reads off a multi-opening count and PEOPLE & HR's off a cross-bucket
#: count, so neither may be rendered behind a different signal.
_FRICTION_REQUIRES_SIGNAL: Dict[str, frozenset] = {
    FRICTION_HEADCOUNT_ADDITION: frozenset({SIGNAL_MULTI_OPENING}),
    FRICTION_CROSS_FUNCTION_SCREENING: frozenset({SIGNAL_MULTI_OPENING_CROSS_BUCKET}),
    FRICTION_TITLE_INFLATION: frozenset({SIGNAL_MULTI_OPENING}),
    FRICTION_INTERVIEW_WEAK_SIGNAL: frozenset({SIGNAL_MULTI_OPENING}),
    FRICTION_SCOPE_SPLIT: frozenset({SIGNAL_SCOPE_COMBINATION}),
    FRICTION_RARE_INTERSECTION: frozenset({SIGNAL_SCOPE_COMBINATION}),
    FRICTION_EMPLOYMENT_OBLIGATION: frozenset({SIGNAL_SCOPE_COMBINATION}),
    FRICTION_TITLE_VS_SCOPE: frozenset({SIGNAL_ROLE_FOCUS_MATCH}),
    FRICTION_NO_TRACK_RECORD_YET: frozenset({SIGNAL_ROLE_FOCUS_MATCH}),
}


def _fill(text: str, data: RenderInputs) -> str:
    """Substitute the record's own values into a friction template.

    Only PRODUCT's needs it today: its line counts the openings, so it has to be
    rendered from the record rather than fixed at "two". Anything with no
    placeholder is returned untouched, and a leftover placeholder would be caught
    by the unresolved-merge-variable gate.
    """
    if "{" not in text:
        return text
    count = int(data.opening_count or 0)
    return text.format(
        opening_count=rendered_count(count),
        opening_subject=(
            "both searches" if count == 2 else f"all {rendered_count(count)} searches"
        ),
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
    if signal_type in (SIGNAL_JOB_AGE, SIGNAL_ACTIVE_REQ):
        # The campaign's T1 reason is unavailable, but its PROOF still is, so the
        # degraded friction is the one that hands off to that proof.
        return _DEGRADED_FRICTION_BY_PROOF.get(proof_type, _DEGRADED_EVIDENCE_FRICTION)
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
    "People we place join your team full-time but don't go on your official "
    "headcount."
)

#: TGTC carries payroll, taxes, benefits, compliance and HR administration. Also
#: verified, and deliberately worded as what WE carry rather than what the reader
#: saves, so it states a fact and not an outcome.
EMPLOYMENT_ADMIN_PROOF = (
    "We handle payroll, taxes, benefits and compliance."
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
        # The claim text itself is verbatim from the verified registry entry.
        return f"Everyone we send has been through a {data.verified_claim_text}."
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

def offer_noun(policy: CampaignPolicy, offer_type: str, *, signal_type: str = "") -> str:
    """The exact words this campaign uses for this offer, resolved once.

    Two campaigns name their offer after their own T1 signal -- OPERATIONS asks
    for "how we test for a scope like this", GTM for "how we test the
    combination". Behind a degraded signal the email never describes a scope or a
    combination, so those CTAs point at nothing. Such a campaign may declare a
    tier-safe noun instead.

    The noun is still resolved ONCE per record and reused verbatim in E1-E4, so
    one lead's thread stays internally consistent. It is only across TIERS that
    the noun can differ, which no single reader ever sees.
    """
    if signal_type and signal_type != policy.t1_signal:
        degraded = policy.degraded_offer_noun(offer_type)
        if degraded:
            return degraded
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
    friction = _fill(friction, data)
    # A campaign may phrase a proof in its own words. The CLAIM is unchanged --
    # only the sentence carrying it differs, so the proof follows this campaign's
    # argument instead of reading as one template used nine times. A verified
    # registry claim still wins, because it carries its own source.
    #
    # Some of those phrasings point back at the signal ("the rest of that scope",
    # "the combination", a bare "instead"). Behind a degraded signal there is
    # nothing for them to point at, so they fall back to the shared wording
    # rather than referring to something the reader was never shown.
    proof = _proof_line(proof_type, data)
    if _campaign_proof_text_applies(policy, signal_type):
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
    # "Bumping this" is what people actually type; "in case it slipped past" was
    # filler that made it read as a sequence step rather than a nudge.
    return _paragraphs(
        "Just bumping this one.",
        f"Happy to send {noun} if it's useful.",
    )


def render_email_3(noun: str) -> str:
    # E3 introduces the commercial terms -- the two verified facts deliberately
    # held back from E1 -- and re-offers. "you owe nothing" restated "you don't
    # pay anything", so it went; the re-offer no longer needs plural agreement
    # because nothing is described as being "still there".
    return _paragraphs(
        "One thing I left out.",
        "You don't pay anything until you've picked someone. If nobody on the "
        "shortlist works, we keep looking.",
        f"Happy to send {noun} whenever.",
    )


def render_email_4(noun: str, role: str) -> str:
    # "Closing this out" is a recognisable sequence marker. "Last one from me" is
    # the same message and is what a person says.
    return _paragraphs(
        "Last one from me.",
        f"If the {role} search is already handled, tell me and I'll stop.",
        f"If it's still open, say the word and I'll send {noun} over.",
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
    noun = offer_noun(policy, offer_type, signal_type=signal_type)
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
