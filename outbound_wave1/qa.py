"""Copy QA hard gates for Challenger B.

A failure here means the record is NOT enrolled in the challenger. The gates are
deliberately mechanical: each one reads the finished rendered text and the
structured resolution that produced it, and every failure names itself so the
dry-run artifact explains exactly why a record was withheld.

These gates apply to Challenger B only. Control A is the live campaign copy and
is never inspected, rewritten or failed by this module.
"""

from __future__ import annotations

import re
from typing import Dict, List, Sequence, Tuple

from .campaigns import (
    OFFER_CLASS_PUBLISHED,
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
    SIGNAL_MULTI_OPENING,
    TIER_1,
    TIER_2,
    VALID_OFFER_NOUNS,
    CampaignPolicy,
)
from .render import (
    FRICTION_APPROVAL_SEQUENCING,
    FRICTION_COMBINED_SCOPE_POOL,
    FRICTION_COST_COMPARISON,
    FRICTION_CROSS_FUNCTION_SCREENING,
    FRICTION_EMPLOYMENT_OBLIGATION,
    FRICTION_INTERVIEW_WEAK_SIGNAL,
    FRICTION_MULTI_AREA_SHORTLIST,
    FRICTION_NO_TRACK_RECORD_YET,
    FRICTION_PARALLEL_POOL,
    FRICTION_RARE_INTERSECTION,
    FRICTION_SCOPE_COMPRESSION,
    FRICTION_SCREENING_BANDWIDTH,
    FRICTION_TIME_OPEN,
    FRICTION_TITLE_INFLATION,
    FRICTION_TITLE_VS_SCOPE,
    html_to_text,
    offer_question,
    rendered_count,
    to_html,
)

#: Which frictions may accompany each (proof, offer) pair.
#:
#: A degrade has to move all three together. The cost-framed friction belongs to
#: the economics path and NOTHING else -- pairing it with "how we test for this
#: role" would leave the reader a budget problem and a testing answer. The
#: economics path likewise may not borrow a scope/bandwidth friction.
#: Frictions that describe a degraded signal rather than a campaign argument.
#: Any proof/offer pair may carry them, because T2/T3 records keep their
#: campaign's proof while losing its signal-specific reason.
_DEGRADED_FRICTIONS = frozenset({FRICTION_TIME_OPEN, FRICTION_PARALLEL_POOL})

#: Frictions that lead into "so we test on the work". They set up an EVIDENCE
#: problem -- the title, the CV or the interview is not telling the reader what
#: they need -- which is the only setup a testing proof answers.
_TESTING_FRICTIONS = frozenset({
    FRICTION_SCREENING_BANDWIDTH, FRICTION_CROSS_FUNCTION_SCREENING,
    FRICTION_MULTI_AREA_SHORTLIST, FRICTION_COMBINED_SCOPE_POOL,
    FRICTION_TITLE_VS_SCOPE, FRICTION_TITLE_INFLATION,
    FRICTION_INTERVIEW_WEAK_SIGNAL, FRICTION_RARE_INTERSECTION,
    FRICTION_NO_TRACK_RECORD_YET,
}) | _DEGRADED_FRICTIONS

COHERENT_FRICTIONS = {
    (PROOF_ECONOMICS, OFFER_ROLE_ECONOMICS): frozenset({FRICTION_COST_COMPARISON}),
    (PROOF_TESTING_MECHANICS, OFFER_TESTING_OVERVIEW): _TESTING_FRICTIONS,
    (PROOF_REMOTE_READINESS, OFFER_REMOTE_READINESS_OVERVIEW): frozenset({
        FRICTION_CROSS_FUNCTION_SCREENING, FRICTION_SCREENING_BANDWIDTH,
        FRICTION_MULTI_AREA_SHORTLIST, FRICTION_COMBINED_SCOPE_POOL,
    }) | _DEGRADED_FRICTIONS,
    # The headcount proof answers an APPROVAL problem: too many lines to get
    # signed off, or one line asked to cover too much. It must never be paired
    # with an evidence friction -- "a CV cannot show you that" followed by "they
    # are not on your headcount" answers a question the reader did not ask.
    (PROOF_HEADCOUNT_MODEL, OFFER_HEADCOUNT_OVERVIEW): frozenset({
        FRICTION_APPROVAL_SEQUENCING, FRICTION_SCOPE_COMPRESSION,
    }) | _DEGRADED_FRICTIONS,
    # The employment-administration proof answers an ADMINISTRATIVE BURDEN
    # problem, and nothing else.
    (PROOF_EMPLOYMENT_ADMIN, OFFER_EMPLOYMENT_ADMIN_OVERVIEW): frozenset({
        FRICTION_EMPLOYMENT_OBLIGATION,
    }) | _DEGRADED_FRICTIONS,
}

#: Anything that still looks like a template placeholder.
_MERGE_VARIABLE_RE = re.compile(r"\{\{[^}]*\}\}|\{[A-Za-z_][A-Za-z0-9_]*\}")

#: Phrases the challenger may never contain, with the reason each is banned.
BANNED_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r"\btwo[- ]week\b", "speed_claim_two_week"),
    (r"\b2[- ]week", "speed_claim_two_week"),
    (r"\b14[- ]?days?\b", "speed_claim_14_day"),
    (r"\bwithin 14\b", "speed_claim_14_day"),
    (r"\b30[- ]?days?\b", "speed_claim_30_day"),
    (r"\bai[- ]fluent\b", "banned_ai_fluent"),
    (r"\btop talent\b", "banned_top_talent"),
    (r"\bpre[- ]?vetted\b", "banned_pre_vetted"),
    (r"\bworld[- ]class\b", "banned_world_class"),
    (r"\bcurated\b", "banned_curated"),
    (r"\$\s?3[.,]5\s?[-–]\s?4\s?k\b", "banned_universal_price_claim"),
    (r"\$\s?3[.,]5\s?k\b", "banned_universal_price_claim"),
    (r"\$\s?4\s?k\b", "banned_universal_price_claim"),
    (r"\$\s?3,?500\b", "banned_universal_price_claim"),
    # Landing-page vocabulary. This list is deliberately SHORT: it catches words
    # that are always wrong in this voice, not an attempt to encode "sounds
    # human" as a phrase list. Naturalness is an editorial judgement and stays
    # one -- these are just words no draft of this copy should ever contain.
    (r"\bsolutions?\b", "buzzword_solution"),
    (r"\bplatform\b", "buzzword_platform"),
    (r"\btransform", "buzzword_transform"),
    (r"\bunlock\b", "buzzword_unlock"),
    (r"\bleverage\b", "buzzword_leverage"),
    (r"\bstreamlin", "buzzword_streamline"),
    (r"\brevolutioni", "buzzword_revolutionize"),
    (r"\bsynerg", "buzzword_synergy"),
    (r"\bbest[- ]in[- ]class\b", "buzzword_best_in_class"),
    (r"\bgame[- ]chang", "buzzword_game_changer"),
    (r"\bcutting[- ]edge\b", "buzzword_cutting_edge"),
    (r"\bseamless", "buzzword_seamless"),
    (r"\bsupercharge", "buzzword_supercharge"),
    (r"\broi\b", "hype_roi"),
    (r"\b10x\b", "hype_10x"),
)

#: Link / image / attachment markers.
_LINK_RE = re.compile(r"https?://|\bwww\.[a-z0-9-]+\.|\[[^\]]+\]\([^)]+\)", re.I)
_IMAGE_RE = re.compile(r"<img\b|!\[[^\]]*\]\(|\bcid:", re.I)
_ATTACHMENT_RE = re.compile(r"\battach(?:ed|ment)\b|\bsee the attached\b", re.I)

#: Any currency figure. Only a published-economics claim may carry one, and the
#: shipped economics wording carries none at all.
_PRICE_RE = re.compile(r"\$\s?\d")

_MAX_FOCUS_ITEMS = 3


def _find_banned(text: str) -> List[str]:
    lowered = text.lower()
    hits: List[str] = []
    for pattern, label in BANNED_PATTERNS:
        if re.search(pattern, lowered):
            hits.append(label)
    return sorted(set(hits))


def authored_text(text: str, *values: str) -> str:
    """``text`` with the record's own values removed.

    The banned-language gates judge what WE wrote, not what the prospect is
    called. A real company named "CBX Solutions, LLC" or a posting titled
    "... Platform Operations Specialist" would otherwise trip the buzzword
    list, and a company named "Top Talent Inc" would trip the older one --
    suppressing a perfectly good record over a name we did not choose.
    """
    out = text
    for value in values:
        cleaned = str(value or "").strip()
        if len(cleaned) > 2:
            out = re.sub(re.escape(cleaned), " ", out, flags=re.I)
    return out


#: A send-safe role display is a short noun phrase. Anything that still reads as a
#: raw posting title -- a pipe-delimited dump, an appended qualifier, a whole
#: sentence, a mojibake character -- would be interpolated straight into the copy,
#: so it fails closed and the account simply stays on Control A.
#:
#: The pipeline's own ``role-display/2`` resolver already produces clean displays
#: and holds ambiguous ones; this is a last-line check for rows written before it
#: shipped, and for anything that slipped past.
_MAX_ROLE_DISPLAY_CHARS = 48
_SEND_SAFE_CONFIDENCE = frozenset({"high", "medium"})

_ROLE_UNSAFE_CHARS = re.compile(
    r'[|:;,"\[\]{}()]|[\x00-\x1f]|[\ufffd$@#]|https?://'
)
#: A spaced dash almost always introduces an appended qualifier
#: ("AI Engineer - W2 ONLY", or an en-dash variant of the same shape).
_ROLE_APPENDIX = re.compile(r'\s[-\u2010-\u2015]\s')
#: Sentence and job-board furniture that is not part of a role name.
_ROLE_SENTENCE = re.compile(
    r'\b(?:is hiring|hiring|needed|wanted|apply now|urgent|w2|c2c|hybrid)\b', re.I
)


def role_display_send_safe(role: str) -> Tuple[bool, str]:
    """Is this stored role display safe to interpolate into outbound copy?"""
    value = str(role or "").strip()
    if not value:
        return False, "role_display_empty"
    if not re.search(r"[A-Za-z]", value):
        return False, "role_display_has_no_letters"
    if len(value) > _MAX_ROLE_DISPLAY_CHARS:
        return False, f"role_display_longer_than_{_MAX_ROLE_DISPLAY_CHARS}_chars"
    if _ROLE_UNSAFE_CHARS.search(value):
        return False, "role_display_contains_unsafe_characters"
    if _ROLE_APPENDIX.search(value):
        return False, "role_display_carries_an_appended_qualifier"
    if _ROLE_SENTENCE.search(value):
        return False, "role_display_reads_as_a_posting_headline"
    return True, "role_display_send_safe"


def run_qa_gates(resolution: Dict, *, policy: CampaignPolicy) -> Tuple[bool, List[str]]:
    """Return ``(pass, reasons)`` for one resolved Challenger record."""
    reasons: List[str] = []

    emails: Sequence[str] = (
        resolution.get("rendered_email_1") or "",
        resolution.get("rendered_email_2") or "",
        resolution.get("rendered_email_3") or "",
        resolution.get("rendered_email_4") or "",
    )
    subject = resolution.get("rendered_subject") or ""
    all_text = "\n\n".join([subject, *emails])

    # --- eligibility / completeness ----------------------------------------
    if not resolution.get("eligible", True):
        reasons.append(str(resolution.get("ineligible_reason") or "record_not_wave1_eligible"))
    for index, body in enumerate(emails, start=1):
        if not body.strip():
            reasons.append(f"empty_rendered_email_{index}")
    if not subject.strip():
        reasons.append("empty_rendered_subject")

    # --- unresolved merge variables ----------------------------------------
    if _MERGE_VARIABLE_RE.search(all_text):
        reasons.append("unresolved_merge_variable")

    # --- signal evidence ----------------------------------------------------
    tier = str(resolution.get("signal_tier") or "")
    evidence = resolution.get("signal_evidence")
    if tier in (TIER_1, TIER_2) and not evidence:
        reasons.append("tier_without_supporting_evidence")
    if resolution.get("signal_evidence_required") and not evidence:
        reasons.append("signal_evidence_empty_when_required")

    # --- evidence count for the selected T1 template ------------------------
    if tier == TIER_1 and policy.t1_min_evidence:
        usable = int(resolution.get("usable_evidence_count") or 0)
        if usable < policy.t1_min_evidence:
            reasons.append(
                f"insufficient_evidence_for_t1_template_{usable}_lt_{policy.t1_min_evidence}"
            )

    # --- max three focus items in an email ----------------------------------
    # Counted structurally from the item lists the resolver built. Re-parsing the
    # rendered sentence would mis-count a facet that itself contains "and"
    # ("routing and lifecycle automation").
    for key in ("evidence_item_count", "scope_item_count"):
        if int(resolution.get(key) or 0) > _MAX_FOCUS_ITEMS:
            reasons.append(f"more_than_{_MAX_FOCUS_ITEMS}_focus_items_in_email")

    # --- the rendered role must be a send-safe display ----------------------
    role_safe, role_reason = role_display_send_safe(resolution.get("role") or "")
    if not role_safe:
        reasons.append(role_reason)
    role_confidence = str(resolution.get("role_confidence") or "").strip().lower()
    if role_confidence and role_confidence not in _SEND_SAFE_CONFIDENCE:
        reasons.append(f"role_display_confidence_{role_confidence}")
    company_confidence = str(resolution.get("company_confidence") or "").strip().lower()
    if company_confidence and company_confidence not in _SEND_SAFE_CONFIDENCE:
        reasons.append(f"company_display_confidence_{company_confidence}")

    # --- economics gates ----------------------------------------------------
    proof = str(resolution.get("proof_type") or "")
    claim_source = str(resolution.get("claim_source") or "")
    if proof == PROOF_ECONOMICS:
        if not resolution.get("role_page_match"):
            reasons.append("economics_without_exact_role_page_match")
        if not claim_source:
            reasons.append("economics_without_claim_source")
        economics_role = str(resolution.get("economics_role") or "")
        display_role = str(resolution.get("role") or "")
        if economics_role.strip().casefold() != display_role.strip().casefold():
            reasons.append("economics_role_does_not_match_rendered_role")
    if str(resolution.get("offer_class") or "") == OFFER_CLASS_PUBLISHED and not claim_source:
        reasons.append("published_offer_without_claim_source")

    # --- a price may never appear without licensed economics ---------------
    if _PRICE_RE.search(all_text) and not (proof == PROOF_ECONOMICS and claim_source):
        reasons.append("price_rendered_without_licensed_economics")

    # --- verified-claim gate ------------------------------------------------
    if proof == PROOF_REMOTE_READINESS and not resolution.get("verified_claim_text"):
        reasons.append("remote_readiness_proof_without_verified_claim")
    if resolution.get("unverified_claim_rendered"):
        reasons.append("unsupported_claim_rendered")

    # --- Product must use the record's real opening count -------------------
    # The count renders as a word ("two product roles") rather than a numeral,
    # because a sentence opening with a digit is a mail-merge tell. The guarantee
    # is unchanged: the rendered count must be THIS record's, so the gate asks the
    # renderer which form it used and still rejects a hard-coded "two".
    if policy.key == "product" and str(resolution.get("signal_type") or "") == SIGNAL_MULTI_OPENING:
        count = int(resolution.get("opening_count") or 0)
        signal_line = str((resolution.get("e1_segments") or {}).get("signal") or "")
        spoken = re.escape(rendered_count(count))
        if count < 2:
            reasons.append("product_multi_opening_without_two_openings")
        elif not re.search(rf"\b(?:{count}|{spoken})\s+\S+\s+roles\b", signal_line, re.I):
            reasons.append("product_opening_count_not_rendered_from_record")
        if count != 2 and re.search(r"\b(?:two|2)\s+product roles\b", signal_line, re.I):
            reasons.append("product_hardcoded_opening_count")

    # --- Customer Experience must not conflate Support and Success ----------
    if policy.audience_by_bucket:
        bucket = str(resolution.get("role_bucket") or "")
        expected = policy.audience_by_bucket.get(bucket, "")
        if not expected:
            reasons.append("customer_experience_audience_unresolved")
        else:
            wrong = [
                value for key, value in policy.audience_by_bucket.items()
                if key != bucket
            ]
            segments = resolution.get("e1_segments") or {}
            generated = " ".join(
                str(segments.get(key) or "") for key in ("signal", "friction")
            ).lower()
            for value in wrong:
                if value in generated:
                    reasons.append("customer_experience_mislabels_support_vs_success")
                    break

    # --- friction -> proof -> offer must degrade together -------------------
    friction_angle = str(resolution.get("friction_angle") or "")
    allowed = COHERENT_FRICTIONS.get((proof, str(resolution.get("outbound_offer_type") or "")))
    if allowed is None:
        reasons.append("proof_offer_pair_is_not_a_wave1_combination")
    elif friction_angle and friction_angle not in allowed:
        reasons.append("friction_incoherent_with_proof_and_offer")

    # --- offer wording is identical in all four emails ----------------------
    # The noun is resolved once and is the literal text the reader sees, so this
    # compares rendered strings, not semantic labels.
    offer_type = str(resolution.get("outbound_offer_type") or "")
    noun = str(resolution.get("offer_noun") or "")
    if noun not in VALID_OFFER_NOUNS:
        reasons.append("offer_noun_outside_frozen_vocabulary")
    if noun and noun != policy.offer_noun(offer_type):
        reasons.append("offer_noun_is_not_this_campaigns_noun_for_the_offer_type")
    e1_offer = str((resolution.get("e1_segments") or {}).get("offer") or "")
    if noun and e1_offer and e1_offer != offer_question(noun):
        reasons.append("email_1_does_not_ask_for_the_resolved_offer_noun")
    if noun:
        for index in (1, 2, 3, 4):
            body = str(resolution.get(f"rendered_email_{index}") or "")
            if noun.lower() not in body.lower():
                reasons.append(f"offer_noun_missing_from_email_{index}")
        thread = " ".join(emails).lower()
        for other in VALID_OFFER_NOUNS:
            if other != noun and other.lower() in thread:
                reasons.append("offer_changed_within_thread")
                break

    # --- banned challenger language ----------------------------------------
    # Judged over what we authored: the record's own company and role strings
    # are removed first so a prospect's name can never fail our copy gate.
    for label in _find_banned(authored_text(
        all_text, resolution.get("company") or "", resolution.get("role") or "",
        resolution.get("canonical_role") or "",
    )):
        reasons.append(label)

    # --- the HTML body must be a faithful transport of the plain text -------
    # It is what Instantly actually sends, so it is gated as hard as the text: it
    # must round-trip back to the exact plain-text email, which makes it
    # impossible for a link, an image or a stray word to enter via the HTML.
    for index, body in enumerate(emails, start=1):
        markup = str(resolution.get(f"rendered_email_{index}_html") or "")
        if not body.strip():
            continue
        if not markup.strip():
            reasons.append(f"missing_html_body_for_email_{index}")
        elif html_to_text(markup) != body.strip():
            reasons.append(f"html_body_does_not_match_email_{index}")
        elif markup != to_html(body):
            reasons.append(f"html_body_was_not_produced_by_the_converter_{index}")

    # --- links / images / attachments --------------------------------------
    for index in (1, 2):
        if _LINK_RE.search(emails[index - 1]):
            reasons.append(f"link_in_email_{index}")
    for index, body in enumerate(emails, start=1):
        if _IMAGE_RE.search(body):
            reasons.append(f"image_in_email_{index}")
        if _ATTACHMENT_RE.search(body):
            reasons.append(f"attachment_reference_in_email_{index}")

    ordered = sorted(set(reasons))
    return (not ordered), ordered


def audit_arm_consistency(resolutions: Sequence[Dict]) -> List[str]:
    """Fail if any company key appears under both arms in one batch.

    Suppressed records carry no arm, so they are skipped: a company may well have
    one opportunity suppressed and another in B, and that is not a split -- it is
    the shared eligibility gate doing its job per opportunity.
    """
    by_key: Dict[str, set] = {}
    for item in resolutions:
        key = str(item.get("company_assignment_key") or "")
        arm = str(item.get("experiment_arm") or "")
        if not key or arm not in {"A", "B"}:
            continue
        by_key.setdefault(key, set()).add(arm)
    return sorted(
        f"company_received_both_arms:{key}"
        for key, arms in by_key.items()
        if len(arms) > 1
    )
