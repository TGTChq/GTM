"""Experiment measurement for Outbound Wave 1.

Primary metric: **positive replies / randomized eligible contacts**.

The denominator is the part that is easy to get wrong, so it is computed here
rather than assumed. A contact is *randomized eligible* when its record could
have received the challenger -- it is in a Wave 1 campaign, it has a resolvable
account key, and its CHALLENGER render passes every QA gate. That question is
answered identically for both arms, because ``resolve_challenger`` runs for
control records too. Restricting the denominator to rows that actually got B
would compare a QA-filtered treatment group against an unfiltered control group
and inflate the effect.

Also reported:

* positive replies per 1,000 enrolled (a volume-normalised view);
* the same numbers stratified by campaign, signal tier, proof, offer, and reply
  step, so a treatment effect can be read where it actually happened;
* delivered / reply / bounce rates, which are diagnostic and guardrail only --
  they never decide the experiment.

Outcomes arrive from outside (Instantly events, Airtable lifecycle, CRM). This
module joins them; it never fetches them.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .assignment import ARM_A, ARM_B

#: Strata every metric can be broken down by.
STRATA: Tuple[str, ...] = (
    "campaign",
    "signal_tier",
    "signal_type",
    "proof_type",
    "outbound_offer_type",
    "offer_class",
    "friction_angle",
    "role_page_match",
)

#: Outcome fields the join understands. Everything is optional; a missing field
#: is counted as "not observed", never as a zero that quietly shrinks a rate.
OUTCOME_FIELDS: Tuple[str, ...] = (
    "delivered",
    "bounced",
    "replied",
    "positive_reply",
    "reply_step",
    "meeting_booked",
    "opportunity_created",
    "fulfilled",
)


@dataclass
class RandomizationRow:
    """One contact's place in the experiment frame."""

    record_id: str
    lead_key: str
    contact_key: str
    company_assignment_key: str
    experiment_arm: str
    experiment_id: str
    campaign: str
    campaign_key: str
    signal_tier: str
    signal_type: str
    proof_type: str
    outbound_offer_type: str
    offer_class: str
    friction_angle: str
    role_page_match: bool
    copy_version: str
    #: Could this contact have received the challenger? Arm-independent.
    randomized_eligible: bool
    eligibility_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _stratum_value(row: RandomizationRow, name: str) -> str:
    value = getattr(row, name, "")
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value or "")


def randomization_row(
    resolution: Any,
    *,
    arm: Optional[str] = None,
    contact_key: str = "",
) -> RandomizationRow:
    """Build one frame row from a CHALLENGER resolution.

    ``arm`` overrides the resolution's arm, which is what the caller does for a
    control record: the strata come from the challenger render (so control rows
    are classified the same way treatment rows are), while the arm stays A.
    """
    payload = resolution.to_dict() if hasattr(resolution, "to_dict") else dict(resolution)
    eligible = bool(payload.get("eligible")) and bool(payload.get("qa_pass"))
    reasons = list(payload.get("qa_reasons") or [])
    if not payload.get("company_assignment_key"):
        eligible = False
        reasons.append("no_resolvable_company_assignment_key")
    return RandomizationRow(
        record_id=str(payload.get("record_id") or ""),
        lead_key=str(payload.get("lead_key") or ""),
        contact_key=contact_key or str(payload.get("lead_key") or payload.get("record_id") or ""),
        company_assignment_key=str(payload.get("company_assignment_key") or ""),
        experiment_arm=str(arm or payload.get("experiment_arm") or ARM_A),
        experiment_id=str(payload.get("experiment_id") or ""),
        campaign=str(payload.get("campaign") or ""),
        campaign_key=str(payload.get("campaign_key") or ""),
        signal_tier=str(payload.get("signal_tier") or ""),
        signal_type=str(payload.get("signal_type") or ""),
        proof_type=str(payload.get("proof_type") or ""),
        outbound_offer_type=str(payload.get("outbound_offer_type") or ""),
        offer_class=str(payload.get("offer_class") or ""),
        friction_angle=str(payload.get("friction_angle") or ""),
        role_page_match=bool(payload.get("role_page_match")),
        copy_version=str(payload.get("copy_version") or ""),
        randomized_eligible=eligible,
        eligibility_reasons=sorted(set(reasons)),
    )


def build_frame(
    resolutions: Sequence[Any],
    challenger_previews: Sequence[Any] = (),
) -> List[RandomizationRow]:
    """Assemble the experiment frame from a resolved batch.

    ``resolutions`` are the real per-record outcomes (control rows carry no copy);
    ``challenger_previews`` are the challenger renders for the control rows, which
    is what makes the eligibility rule arm-independent. A control row with no
    preview is still included, marked ineligible, so it can never be silently
    dropped from the denominator without a reason.
    """
    previews = {
        str(getattr(item, "record_id", "") or ""): item for item in challenger_previews
    }
    rows: List[RandomizationRow] = []
    for resolution in resolutions:
        arm = str(getattr(resolution, "experiment_arm", ARM_A) or ARM_A)
        record_id = str(getattr(resolution, "record_id", "") or "")
        source = resolution if arm == ARM_B else previews.get(record_id, resolution)
        row = randomization_row(source, arm=arm)
        if arm == ARM_A and record_id not in previews:
            row.randomized_eligible = False
            row.eligibility_reasons = sorted(
                set(row.eligibility_reasons) | {"no_challenger_preview_for_control_row"}
            )
        rows.append(row)
    return rows


def _rate(numerator: int, denominator: int) -> Optional[float]:
    """A rate, or ``None`` when there is nothing to divide by.

    Returning ``None`` rather than 0.0 keeps "no data" visibly distinct from
    "measured zero".
    """
    return (numerator / denominator) if denominator else None


@dataclass
class ArmResult:
    arm: str
    randomized_eligible: int = 0
    enrolled: int = 0
    delivered: int = 0
    bounced: int = 0
    replied: int = 0
    positive_replies: int = 0
    meetings: int = 0
    opportunities: int = 0
    fulfilled: int = 0
    reply_steps: Dict[str, int] = field(default_factory=dict)

    @property
    def primary_metric(self) -> Optional[float]:
        """Positive replies per randomized eligible contact."""
        return _rate(self.positive_replies, self.randomized_eligible)

    @property
    def positive_replies_per_1000_enrolled(self) -> Optional[float]:
        rate = _rate(self.positive_replies, self.enrolled)
        return None if rate is None else rate * 1000

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["primary_metric_positive_replies_per_randomized_eligible"] = self.primary_metric
        payload["positive_replies_per_1000_enrolled"] = self.positive_replies_per_1000_enrolled
        # Guardrail only. Deliverability decides whether the test is READABLE,
        # never which arm won.
        payload["guardrails"] = {
            "delivered_rate": _rate(self.delivered, self.enrolled),
            "bounce_rate": _rate(self.bounced, self.enrolled),
            "reply_rate": _rate(self.replied, self.delivered),
        }
        return payload


def _accumulate(result: ArmResult, outcome: Mapping[str, Any]) -> None:
    result.enrolled += 1
    result.delivered += 1 if outcome.get("delivered") else 0
    result.bounced += 1 if outcome.get("bounced") else 0
    result.replied += 1 if outcome.get("replied") else 0
    result.positive_replies += 1 if outcome.get("positive_reply") else 0
    result.meetings += 1 if outcome.get("meeting_booked") else 0
    result.opportunities += 1 if outcome.get("opportunity_created") else 0
    result.fulfilled += 1 if outcome.get("fulfilled") else 0
    if outcome.get("positive_reply") and outcome.get("reply_step") is not None:
        key = f"step_{outcome.get('reply_step')}"
        result.reply_steps[key] = result.reply_steps.get(key, 0) + 1


def analyze(
    frame: Sequence[RandomizationRow],
    outcomes: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]] = (),
    *,
    strata: Sequence[str] = STRATA,
) -> Dict[str, Any]:
    """Compute the primary metric overall and by stratum.

    ``outcomes`` is keyed by ``contact_key`` (or an iterable of dicts carrying
    one). A contact with no outcome row is still counted in the denominator --
    that is the point of an intention-to-treat denominator.
    """
    if not isinstance(outcomes, Mapping):
        outcomes = {
            str(item.get("contact_key") or ""): item
            for item in outcomes
            if isinstance(item, Mapping)
        }

    overall: Dict[str, ArmResult] = {ARM_A: ArmResult(ARM_A), ARM_B: ArmResult(ARM_B)}
    by_stratum: Dict[str, Dict[str, Dict[str, ArmResult]]] = {
        name: defaultdict(lambda: {ARM_A: ArmResult(ARM_A), ARM_B: ArmResult(ARM_B)})
        for name in strata
    }
    withheld: Dict[str, int] = defaultdict(int)

    for row in frame:
        if not row.randomized_eligible:
            for reason in row.eligibility_reasons or ["unspecified"]:
                withheld[reason] += 1
            continue
        arm = row.experiment_arm if row.experiment_arm in overall else ARM_A
        outcome = outcomes.get(row.contact_key, {})
        overall[arm].randomized_eligible += 1
        _accumulate(overall[arm], outcome)
        for name in strata:
            value = _stratum_value(row, name)
            cell = by_stratum[name][value][arm]
            cell.randomized_eligible += 1
            _accumulate(cell, outcome)

    return {
        "primary_metric": "positive_replies / randomized_eligible_contacts",
        "overall": {arm: result.to_dict() for arm, result in overall.items()},
        "lift": _lift(overall[ARM_A], overall[ARM_B]),
        "by_stratum": {
            name: {
                value: {
                    "A": arms[ARM_A].to_dict(),
                    "B": arms[ARM_B].to_dict(),
                    "lift": _lift(arms[ARM_A], arms[ARM_B]),
                }
                for value, arms in sorted(values.items())
            }
            for name, values in by_stratum.items()
        },
        "withheld_from_frame": dict(sorted(withheld.items(), key=lambda kv: -kv[1])),
        "frame_size": len(frame),
        "randomized_eligible": sum(r.randomized_eligible for r in overall.values()),
    }


def _lift(control: ArmResult, challenger: ArmResult) -> Dict[str, Any]:
    a = control.primary_metric
    b = challenger.primary_metric
    absolute = None if (a is None or b is None) else b - a
    relative = None if (a in (None, 0) or b is None) else (b - a) / a
    return {
        "control": a,
        "challenger": b,
        "absolute": absolute,
        "relative": relative,
        "control_n": control.randomized_eligible,
        "challenger_n": challenger.randomized_eligible,
    }
