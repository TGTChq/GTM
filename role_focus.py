"""Generate a grammatically safe, auditable role-focus fragment from a job description.

The output is designed to be inserted after the words "focused on" in outbound
copy, for example:

    Looks like the role is especially focused on CRM automation, lead
    enrichment, and outbound infrastructure.

Important design choices:
- Uses only deterministic, role-specific signals found in the title/JD.
- Never copies raw JD sentences into the email.
- Never invents responsibilities that were not signaled in the posting.
- Produces canonical sentence fragments with controlled capitalization.
- Formats multi-part focus areas as natural English lists.
- Uses a safe role-level fallback when evidence is insufficient.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Pattern, Sequence, Tuple


@dataclass(frozen=True)
class FocusSignal:
    phrase: str
    patterns: Tuple[Pattern[str], ...]
    weight: int = 1


@dataclass(frozen=True)
class RoleFocusResult:
    text: str
    quality: str  # specific / manual_required
    evidence: List[str]


def _patterns(*items: str) -> Tuple[Pattern[str], ...]:
    return tuple(re.compile(item, re.I) for item in items)


SIGNALS: Dict[str, Tuple[FocusSignal, ...]] = {
    "GTM Engineer": (
        FocusSignal(
            "CRM automation",
            _patterns(
                r"\bcrm automation\b", r"\b(salesforce|hubspot)\b.{0,60}\b(workflow|automation|architecture|admin)\b",
                r"\b(workflow|automation|architecture|admin)\b.{0,60}\b(salesforce|hubspot)\b",
            ),
            5,
        ),
        FocusSignal(
            "lead enrichment and data orchestration",
            _patterns(
                r"\blead enrichment\b", r"\bdata enrichment\b", r"\bclay\b", r"\bapollo\b",
                r"\bdata orchestration\b", r"\bdata hygiene\b",
            ),
            5,
        ),
        FocusSignal(
            "lead routing and lifecycle automation",
            _patterns(
                r"\blead routing\b", r"\blifecycle automation\b", r"\blead lifecycle\b",
                r"\bterritory routing\b", r"\bround[- ]robin\b",
            ),
            4,
        ),
        FocusSignal(
            "outbound infrastructure and sequencing",
            _patterns(
                r"\boutbound (infrastructure|systems|automation)\b", r"\b(ai[- ]powered )?sequencing\b",
                r"\b(instantly|outreach|salesloft)\b", r"\bsales engagement platform\b",
            ),
            4,
        ),
        FocusSignal(
            "revenue data and reporting",
            _patterns(
                r"\brevenue reporting\b", r"\brevenue analytics\b", r"\bgtm reporting\b",
                r"\bpipeline reporting\b", r"\battribution\b", r"\bforecasting\b",
            ),
            3,
        ),
        FocusSignal(
            "GTM systems integrations",
            _patterns(
                r"\bgtm systems?\b", r"\bsales systems?\b", r"\bapi integrations?\b",
                r"\bwebhooks?\b", r"\bsystems integration\b",
            ),
            2,
        ),
    ),
    "AI Engineer": (
        FocusSignal(
            "production AI systems",
            _patterns(
                r"\bproduction[- ]grade ai\b", r"\bproduction ai (system|application|platform)s?\b",
                r"\bdeploy(ing|ment)?\b.{0,50}\b(ai|machine learning|ml)\b",
                r"\b(ai|machine learning|ml)\b.{0,50}\bdeploy(ing|ment)?\b",
            ),
            5,
        ),
        FocusSignal(
            "LLM applications and integrations",
            _patterns(
                r"\bllm(s)?\b", r"\blarge language models?\b", r"\bgenerative ai\b",
                r"\b(openai|anthropic|claude|gemini)\b",
            ),
            5,
        ),
        FocusSignal(
            "RAG and retrieval systems",
            _patterns(
                r"\brag\b", r"\bretrieval[- ]augmented generation\b", r"\bvector (database|store)s?\b",
                r"\bsemantic search\b", r"\bembeddings?\b",
            ),
            4,
        ),
        FocusSignal(
            "AI agents and workflow automation",
            _patterns(
                r"\bai agents?\b", r"\bagentic\b", r"\bagent workflows?\b", r"\bmulti[- ]agent\b",
            ),
            4,
        ),
        FocusSignal(
            "machine-learning model development",
            _patterns(
                r"\bmachine learning models?\b", r"\bmodel training\b", r"\bmodel development\b",
                r"\bpytorch\b", r"\btensorflow\b",
            ),
            3,
        ),
        FocusSignal(
            "AI APIs and product integrations",
            _patterns(
                r"\bai api(s)?\b", r"\bapi integrations?\b", r"\bintegrat(e|ing|ion)\b.{0,40}\b(ai|llm)\b",
                r"\b(ai|llm)\b.{0,40}\bintegrat(e|ing|ion)\b",
            ),
            2,
        ),
    ),
    "Automation Specialist": (
        FocusSignal(
            "AI-powered workflow automation",
            _patterns(
                r"\bai automation\b", r"\bai[- ]powered workflows?\b", r"\bllm workflows?\b",
                r"\bai agents?\b", r"\bagentic workflows?\b",
            ),
            5,
        ),
        FocusSignal(
            "no-code/low-code automation",
            _patterns(
                r"\bno[- ]code\b", r"\blow[- ]code\b", r"\bn8n\b", r"\bmake\.com\b",
                r"\bzapier\b", r"\bworkato\b", r"\btray\.io\b",
            ),
            5,
        ),
        FocusSignal(
            "CRM and revenue-workflow automation",
            _patterns(
                r"\bcrm automation\b", r"\brevenue workflow(s)?\b", r"\bgtm automation\b",
                r"\b(salesforce|hubspot)\b.{0,50}\b(automation|workflow)\b",
                r"\b(automation|workflow)\b.{0,50}\b(salesforce|hubspot)\b",
            ),
            5,
        ),
        FocusSignal(
            "API/webhook integrations",
            _patterns(r"\bapi integrations?\b", r"\bwebhooks?\b", r"\brest api\b", r"\bsystems integration\b"),
            4,
        ),
        FocusSignal(
            "business-process automation",
            _patterns(
                r"\bbusiness process automation\b", r"\bprocess automation\b", r"\bworkflow automation\b",
                r"\boperational workflows?\b",
            ),
            3,
        ),
        FocusSignal(
            "internal automation infrastructure",
            _patterns(r"\binternal tools?\b", r"\bautomation infrastructure\b", r"\bworkflow platform\b"),
            2,
        ),
    ),
    "Graphic Designer": (
        FocusSignal(
            "brand identity systems",
            _patterns(r"\bbrand systems?\b", r"\bvisual identity\b", r"\bbrand identity\b", r"\bbrand guidelines?\b"),
            5,
        ),
        FocusSignal(
            "campaign creative and marketing collateral",
            _patterns(r"\bcampaign assets?\b", r"\bmarketing collateral\b", r"\bdigital campaigns?\b", r"\bmarketing assets?\b"),
            5,
        ),
        FocusSignal(
            "high-volume creative production",
            _patterns(r"\bhigh[- ]volume\b.{0,30}\bcreative\b", r"\bcreative production\b", r"\bproduction design\b", r"\basset production\b"),
            4,
        ),
        FocusSignal(
            "social and paid-media creative",
            _patterns(r"\bsocial media graphics?\b", r"\bpaid social creative\b", r"\bpaid media assets?\b", r"\bad creative\b"),
            4,
        ),
        FocusSignal(
            "web and landing-page design",
            _patterns(r"\blanding pages?\b", r"\bweb design\b", r"\bwebsite assets?\b", r"\bemail design\b"),
            3,
        ),
        FocusSignal(
            "Figma and Adobe Creative Suite workflows",
            _patterns(r"\bfigma\b", r"\badobe creative suite\b", r"\bphotoshop\b", r"\billustrator\b", r"\bindesign\b"),
            2,
        ),
    ),
    "Video Editor": (
        FocusSignal(
            "short-form and social video",
            _patterns(r"\bshort[- ]form video\b", r"\bsocial video\b", r"\breels?\b", r"\btiktok\b", r"\byoutube shorts?\b"),
            5,
        ),
        FocusSignal(
            "post-production and motion graphics",
            _patterns(r"\bpost[- ]production\b", r"\bmotion graphics\b", r"\bafter effects\b", r"\banimation\b"),
            5,
        ),
        FocusSignal(
            "performance-ad creative",
            _patterns(r"\bperformance creative\b", r"\bvideo ads?\b", r"\bpaid social\b", r"\bdirect[- ]response video\b", r"\bad creative\b"),
            4,
        ),
        FocusSignal(
            "long-form and YouTube content",
            _patterns(r"\blong[- ]form video\b", r"\byoutube\b", r"\bpodcast video\b", r"\bwebinar\b"),
            3,
        ),
        FocusSignal(
            "rapid creative iteration",
            _patterns(r"\brapid iteration\b", r"\bhigh[- ]volume\b.{0,30}\bvideo\b", r"\bmultiple versions\b", r"\bcreative testing\b"),
            3,
        ),
        FocusSignal(
            "Premiere Pro and After Effects workflows",
            _patterns(r"\bpremiere pro\b", r"\bafter effects\b", r"\bdavinci resolve\b", r"\bfinal cut\b"),
            2,
        ),
    ),
    "Performance Marketing Manager": (
        FocusSignal(
            "paid acquisition and media buying",
            _patterns(r"\bpaid acquisition\b", r"\bmedia buying\b", r"\bpaid media\b", r"\bpaid social\b", r"\bppc\b"),
            5,
        ),
        FocusSignal(
            "campaign experimentation and creative testing",
            _patterns(r"\bcreative testing\b", r"\ba/b testing\b", r"\bexperimentation\b", r"\btest(ing)?\b.{0,30}\bcreative\b"),
            4,
        ),
        FocusSignal(
            "performance measurement across ROAS and CAC",
            _patterns(r"\broas\b", r"\bcac\b", r"\bcpa\b", r"\bperformance measurement\b", r"\bmarketing analytics\b"),
            5,
        ),
        FocusSignal(
            "Google and Meta Ads",
            _patterns(r"\bgoogle ads\b", r"\bmeta ads\b", r"\bfacebook ads\b", r"\bpaid search\b"),
            4,
        ),
        FocusSignal(
            "conversion-rate optimization",
            _patterns(r"\bconversion[- ]rate optimization\b", r"\bcro\b", r"\blanding page optimization\b"),
            3,
        ),
        FocusSignal(
            "budget ownership and channel scaling",
            _patterns(r"\bbudget management\b", r"\bbudget ownership\b", r"\bscale campaigns?\b", r"\bchannel scaling\b"),
            3,
        ),
    ),
    "Customer Support": (
        FocusSignal(
            "ticket resolution and customer communication",
            _patterns(r"\bticket resolution\b", r"\bcustomer inquiries\b", r"\bsupport tickets?\b", r"\bcustomer communication\b"),
            5,
        ),
        FocusSignal(
            "support-queue ownership and SLA performance",
            _patterns(r"\bsupport queues?\b", r"\bservice levels?\b", r"\bsla(s)?\b", r"\bresponse time\b", r"\bresolution time\b"),
            4,
        ),
        FocusSignal(
            "multichannel customer support",
            _patterns(r"\bmultichannel\b", r"\bomnichannel\b", r"\b(email|chat|phone) support\b", r"\blive chat\b"),
            4,
        ),
        FocusSignal(
            "customer issue triage and escalation",
            _patterns(r"\bissue triage\b", r"\bescalation management\b", r"\bescalat(e|ing|ions?)\b", r"\broot cause\b"),
            3,
        ),
        FocusSignal(
            "knowledge-base and support-process improvement",
            _patterns(r"\bknowledge base\b", r"\bsupport documentation\b", r"\bprocess improvement\b", r"\bsupport operations\b"),
            3,
        ),
        FocusSignal(
            "Zendesk, Intercom, and support-tool workflows",
            _patterns(r"\bzendesk\b", r"\bintercom\b", r"\bfreshdesk\b", r"\bgorgias\b"),
            2,
        ),
    ),
    "Customer Success Manager": (
        FocusSignal(
            "customer onboarding and adoption",
            _patterns(r"\bcustomer onboarding\b", r"\bonboarding\b", r"\bproduct adoption\b", r"\badoption\b"),
            5,
        ),
        FocusSignal(
            "retention and expansion",
            _patterns(r"\bretention\b", r"\brenewals?\b", r"\bexpansion\b", r"\bupsell\b", r"\bcross[- ]sell\b"),
            5,
        ),
        FocusSignal(
            "customer-health and risk management",
            _patterns(r"\bcustomer health\b", r"\bhealth score\b", r"\bchurn risk\b", r"\brisk management\b", r"\bchurn prevention\b"),
            4,
        ),
        FocusSignal(
            "strategic account management",
            _patterns(r"\bstrategic accounts?\b", r"\baccount management\b", r"\bbook of business\b", r"\bexecutive relationships?\b"),
            4,
        ),
        FocusSignal(
            "QBRs and value realization",
            _patterns(r"\bqbrs?\b", r"\bquarterly business reviews?\b", r"\bvalue realization\b", r"\bsuccess plans?\b"),
            3,
        ),
        FocusSignal(
            "customer lifecycle and success operations",
            _patterns(r"\bcustomer lifecycle\b", r"\bcustomer success operations\b", r"\bcustomer journey\b"),
            3,
        ),
    ),
}


def _first_match(patterns: Sequence[Pattern[str]], text: str) -> str:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(0).strip()
    return ""


# Some canonical signals contain an internal ``and``. Joining those signals
# directly can produce robotic copy such as ``A and B and C and D``. Expand
# them into semantically complete components before rendering the final list.
# This is deterministic and does not add responsibilities that were not
# already represented by the matched signal.
PHRASE_COMPONENTS: Dict[str, Tuple[str, ...]] = {
    "lead enrichment and data orchestration": ("lead enrichment", "data orchestration"),
    "lead routing and lifecycle automation": ("lead routing", "lifecycle automation"),
    "outbound infrastructure and sequencing": ("outbound infrastructure", "sequencing"),
    "revenue data and reporting": ("revenue reporting",),
    "LLM applications and integrations": ("LLM applications", "LLM integrations"),
    "RAG and retrieval systems": ("RAG", "retrieval systems"),
    "AI agents and workflow automation": ("AI agents", "workflow automation"),
    "AI APIs and product integrations": ("AI APIs", "product integrations"),
    "CRM and revenue-workflow automation": ("CRM automation", "revenue-workflow automation"),
    "campaign creative and marketing collateral": ("campaign creative", "marketing collateral"),
    "social and paid-media creative": ("social creative", "paid-media creative"),
    "web and landing-page design": ("web design", "landing-page design"),
    "Figma and Adobe Creative Suite workflows": ("Figma workflows", "Adobe Creative Suite workflows"),
    "short-form and social video": ("short-form video", "social video"),
    "post-production and motion graphics": ("post-production", "motion graphics"),
    "long-form and YouTube content": ("long-form video", "YouTube content"),
    "Premiere Pro and After Effects workflows": ("Premiere Pro workflows", "After Effects workflows"),
    "paid acquisition and media buying": ("paid acquisition", "media buying"),
    "campaign experimentation and creative testing": ("campaign experimentation", "creative testing"),
    "performance measurement across ROAS and CAC": ("ROAS measurement", "CAC measurement"),
    "Google and Meta Ads": ("Google Ads", "Meta Ads"),
    "budget ownership and channel scaling": ("budget ownership", "channel scaling"),
    "ticket resolution and customer communication": ("ticket resolution", "customer communication"),
    "support-queue ownership and SLA performance": ("support-queue ownership", "SLA performance"),
    "customer issue triage and escalation": ("customer issue triage", "escalation management"),
    "knowledge-base and support-process improvement": ("knowledge-base improvement", "support-process improvement"),
    "Zendesk, Intercom, and support-tool workflows": ("Zendesk workflows", "Intercom workflows", "support-tool workflows"),
    "customer onboarding and adoption": ("customer onboarding", "product adoption"),
    "retention and expansion": ("retention", "expansion"),
    "customer-health and risk management": ("customer-health monitoring", "risk management"),
    "QBRs and value realization": ("QBRs", "value realization"),
    "customer lifecycle and success operations": ("customer lifecycle management", "customer success operations"),
}


def _render_list(items: Sequence[str]) -> str:
    clean: List[str] = []
    seen = set()
    for item in items:
        value = re.sub(r"\s+", " ", str(item or "").strip(" ,.;"))
        key = value.casefold()
        if value and key not in seen:
            clean.append(value)
            seen.add(key)
    if not clean:
        return ""
    if len(clean) == 1:
        return clean[0]
    if len(clean) == 2:
        return f"{clean[0]} and {clean[1]}"
    return f"{', '.join(clean[:-1])}, and {clean[-1]}"


def _join_phrases(
    phrases: List[str], max_display_items: int = 4, max_chars: int | None = None
) -> str:
    if not phrases:
        return ""

    groups: List[List[str]] = [
        list(PHRASE_COMPONENTS.get(phrase, (phrase,))) for phrase in phrases
    ]

    # Reserve one component for every selected signal so lower-ranked but
    # distinct evidence is not lost. Then use remaining slots for additional
    # components in signal-priority order.
    slots = max(max_display_items, len(groups))
    allocations = [1 for _ in groups]
    remaining = slots - len(groups)
    for index, group in enumerate(groups):
        if remaining <= 0:
            break
        extra = min(max(0, len(group) - 1), remaining)
        allocations[index] += extra
        remaining -= extra

    items: List[str] = []
    for group, count in zip(groups, allocations):
        items.extend(group[:count])

    rendered = _render_list(items)
    while max_chars and len(rendered) > max_chars and len(items) > 1:
        items.pop()
        rendered = _render_list(items)
    return rendered


# Safe role-level fallbacks used only when the posting lacks enough explicit
# detail for a specific extraction. Airtable still receives a usable fragment,
# while ``manual_required`` makes it clear that a reviewer should confirm/edit it.
ROLE_FOCUS_FALLBACKS: Dict[str, str] = {
    "GTM Engineer": "GTM systems, workflow automation, and revenue operations",
    "AI Engineer": "AI systems, LLM integrations, and production automation",
    "Automation Specialist": "workflow automation, systems integrations, and process optimization",
    "Graphic Designer": "brand design, campaign creative, and digital asset production",
    "Video Editor": "video editing, post-production, and social content",
    "Performance Marketing Manager": "paid acquisition, campaign optimization, and creative testing",
    "Customer Support": "customer support, issue resolution, and service operations",
    "Customer Success Manager": "customer onboarding, retention, and expansion",
}


def _fallback_focus(job: Dict, matched_role: str) -> str:
    title = (job.get("job_title") or "").lower()
    description = (job.get("job_description") or "")[:3000].lower()
    text = f"{title}\n{description}"

    # Preserve an explicit AI signal in GTM roles without inventing a specific
    # tool or responsibility that the posting did not mention.
    if matched_role == "GTM Engineer" and re.search(
        r"\b(ai|artificial intelligence|llm|agentic|ai[- ]powered)\b", text, re.I
    ):
        return "AI-powered GTM systems, workflow automation, and revenue operations"
    return ROLE_FOCUS_FALLBACKS.get(matched_role, "")


def extract_role_focus(
    job: Dict, matched_role: str, max_items: int = 3, max_chars: int = 125
) -> RoleFocusResult:
    """Return a controlled noun-phrase fragment derived from explicit JD signals."""
    rules = SIGNALS.get(matched_role, ())
    title = (job.get("job_title") or "").strip()
    description = (job.get("job_description") or "")[:16000]
    text = f"{title}\n{description}"

    matches: List[Tuple[int, int, str, str]] = []
    for order, signal in enumerate(rules):
        evidence = _first_match(signal.patterns, text)
        if evidence:
            # Higher weight first; declaration order breaks ties predictably.
            matches.append((-signal.weight, order, signal.phrase, evidence))

    matches.sort()
    selected = matches[:max_items]
    if not selected:
        fallback = _fallback_focus(job, matched_role)
        return RoleFocusResult(
            text=fallback,
            quality="manual_required",
            evidence=[f"fallback_from_role:{matched_role}"] if fallback else [],
        )

    phrases = [item[2] for item in selected]
    evidence = [item[3] for item in selected]
    while len(phrases) > 1 and len(_join_phrases(phrases, max_chars=max_chars)) > max_chars:
        phrases.pop()
        evidence.pop()
    return RoleFocusResult(
        text=_join_phrases(phrases, max_chars=max_chars),
        quality="specific",
        evidence=evidence,
    )
