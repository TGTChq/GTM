"""Build (and, only when explicitly told to, create) the nine Wave 1 Challenger campaigns.

DRY RUN BY DEFAULT. Without ``--execute`` this script performs GET requests only
and writes the exact ``POST /campaigns`` bodies it would send to a JSON file, so
the whole configuration can be reviewed before anything exists in the workspace.

Control A is never touched. Every write this script can make is a *create* of a
new, separate campaign; it issues no PATCH, no DELETE, and it adds no leads. A
newly created campaign starts in ``draft`` and sends nothing until someone
activates it in the Instantly UI.

Each Challenger mirrors its Control's operational settings exactly -- sender
pool, schedule, tracking, stop conditions, daily limits -- so the only thing that
differs between the two arms is the copy. Sharing the Control's sender pool is
deliberate: a dedicated pool would confound the arm with mailbox reputation, and
a mailbox can belong to several campaigns without any change to the Control.

The four step bodies are the rendered Challenger emails, delivered as Instantly
custom variables by ``instantly_client.wave1_enrollment_overlay``:

    step 1  subject {{rendered_subject}}   body {{rendered_email_1_html}}
    step 2  subject "" (same thread)       body {{rendered_email_2_html}}
    step 3  subject "" (same thread)       body {{rendered_email_3_html}}
    step 4  subject "" (same thread)       body {{rendered_email_4_html}}

with the account signature appended in the same shape the live Control bodies
use. Step delays are 3 / 4 / 5 days, i.e. the frozen Day 1 -> 4 -> 8 -> 13
sequence, on the Control's own business-day schedule.

Usage::

    python create_wave1_challenger_campaigns.py --out reports/wave1_challenger_plan.json
    python create_wave1_challenger_campaigns.py --buckets finance --execute   # creates
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
from outbound_wave1.campaigns import CAMPAIGNS, CampaignPolicy  # noqa: E402
from outbound_wave1.timing import SEQUENCE_DAY_LABELS, SEQUENCE_OFFSET_DAYS  # noqa: E402

#: Prefix that makes a Challenger campaign unmistakable in the campaign list.
CHALLENGER_NAME_PREFIX = "WAVE1 CHALLENGER"

#: Instantly renders campaign bodies as HTML, and the live Control bodies are
#: ``<div>para</div><div><br /></div>...``. The rendered ``_html`` variables are
#: produced in exactly that shape by ``outbound_wave1.render.to_html``.
SIGNATURE_BLOCK = "<div><br /></div><div>{{accountSignature}}</div>"

#: Trailing delay on the final step. Instantly reads a step's delay as "wait this
#: long before the NEXT step", so the last one is never used; the live Control
#: campaigns carry 1, and the Challenger matches them.
TRAILING_DELAY_DAYS = 1

#: Operational settings copied verbatim from the Control campaign, so the arms
#: differ in copy and nothing else.
MIRRORED_SETTINGS: Tuple[str, ...] = (
    "campaign_schedule",
    "email_list",
    "email_tag_list",
    "daily_limit",
    "daily_max_leads",
    "text_only",
    "first_email_text_only",
    "link_tracking",
    "open_tracking",
    "stop_on_reply",
    "stop_on_auto_reply",
    "stop_for_company",
    "insert_unsubscribe_header",
    "match_lead_esp",
    "allow_risky_contacts",
    "prioritize_new_leads",
    "cc_list",
    "bcc_list",
    "provider_routing_rules",
)

#: Custom variables the Challenger bodies and reporting rely on, in addition to
#: whatever the Control already declares.
CHALLENGER_CUSTOM_VARIABLES: Tuple[str, ...] = (
    "rendered_subject",
    "rendered_email_1_html",
    "rendered_email_2_html",
    "rendered_email_3_html",
    "rendered_email_4_html",
    "rendered_email_1",
    "rendered_email_2",
    "rendered_email_3",
    "rendered_email_4",
    "experiment_id",
    "experiment_arm",
    "company_assignment_key",
    "wave1_campaign",
    "signal_tier",
    "signal_type",
    "friction_angle",
    "proof_type",
    "claim_source",
    "outbound_offer_type",
    "offer_noun",
    "offer_class",
    "offer_fallback_type",
    "copy_version",
    "role_page_match",
)


def _base() -> str:
    return (os.getenv("INSTANTLY_BASE_URL") or config.INSTANTLY_BASE_URL).rstrip("/")


def _headers() -> Dict[str, str]:
    key = os.getenv("INSTANTLY_API_KEY") or config.INSTANTLY_API_KEY
    if not key:
        raise SystemExit("INSTANTLY_API_KEY is required")
    return {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "TGTC-wave1-challenger-builder/1.0",
    }


def _request(method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{_base()}/{path.lstrip('/')}"
    data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
    last: Exception | None = None
    for attempt in range(4):
        request = Request(url, method=method, data=data, headers=_headers())
        try:
            with urlopen(request, timeout=90) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:400]
            except Exception:  # noqa: BLE001
                pass
            if exc.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(1.5 * (attempt + 1))
                last = exc
                continue
            raise RuntimeError(f"{method} {path} -> {exc.code} {detail}") from exc
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"{method} {path} failed: {last}")


def control_campaign_id(policy: CampaignPolicy) -> str:
    """The live Control campaign id for a policy, from the campaign env vars.

    A policy that covers two buckets (CUSTOMER EXPERIENCE) routes both to one
    live campaign, so the env keys must agree; disagreement is a configuration
    error rather than something to pick a winner from.
    """
    values = []
    for key in policy.env_keys:
        value = str(os.getenv(key) or getattr(config, key, "") or "").strip()
        if value:
            values.append(value)
    unique = sorted(set(values))
    if not unique:
        raise RuntimeError(f"{policy.name}: no control campaign id in {policy.env_keys}")
    if len(unique) > 1:
        raise RuntimeError(
            f"{policy.name}: control campaign ids disagree across {policy.env_keys}: {unique}"
        )
    return unique[0]


def challenger_name(control_name: str) -> str:
    return f"{CHALLENGER_NAME_PREFIX} - {control_name}"


def build_sequence() -> List[Dict[str, Any]]:
    """The four Challenger steps: Day 1 / 4 / 8 / 13, all on one thread."""
    delays = list(SEQUENCE_OFFSET_DAYS) + [TRAILING_DELAY_DAYS]
    steps: List[Dict[str, Any]] = []
    for index, delay in enumerate(delays, start=1):
        steps.append({
            "type": "email",
            "delay": delay,
            "delay_unit": "days",
            "pre_delay_unit": "days",
            "variants": [{
                # Only E1 carries a subject; E2-E4 reply on the same thread, which
                # is how the live Control sequences are built.
                "subject": "{{rendered_subject}}" if index == 1 else "",
                "body": "{{rendered_email_%d_html}}%s" % (index, SIGNATURE_BLOCK),
                "v_disabled": False,
            }],
        })
    return [{"steps": steps}]


def build_payload(policy: CampaignPolicy, control: Dict[str, Any]) -> Dict[str, Any]:
    """The exact ``POST /campaigns`` body for one Challenger campaign."""
    payload: Dict[str, Any] = {
        "name": challenger_name(str(control.get("name") or policy.name)),
        "sequences": build_sequence(),
    }
    for key in MIRRORED_SETTINGS:
        if key in control and control[key] is not None:
            payload[key] = control[key]

    variables = dict(control.get("custom_variables") or {})
    for name in CHALLENGER_CUSTOM_VARIABLES:
        variables[name] = True
    payload["custom_variables"] = variables
    if control.get("core_variables"):
        payload["core_variables"] = control["core_variables"]
    return payload


def _sequence_day_labels() -> List[int]:
    return list(SEQUENCE_DAY_LABELS)


def plan(policies: Sequence[CampaignPolicy]) -> Dict[str, Any]:
    """Read every Control and build its Challenger payload. GET requests only."""
    entries: List[Dict[str, Any]] = []
    for policy in policies:
        control_id = control_campaign_id(policy)
        control = _request("GET", f"campaigns/{quote(control_id, safe='')}")
        payload = build_payload(policy, control)
        entries.append({
            "campaign_key": policy.key,
            "campaign_name": policy.name,
            "role_buckets": list(policy.buckets),
            "control_campaign_id": control_id,
            "control_campaign_name": control.get("name"),
            "control_status": control.get("status"),
            "control_step_count": sum(
                len(seq.get("steps") or []) for seq in (control.get("sequences") or [])
            ),
            "control_sender_pool_size": len(control.get("email_list") or []),
            "challenger_name": payload["name"],
            "challenger_step_count": len(payload["sequences"][0]["steps"]),
            "challenger_day_labels": _sequence_day_labels(),
            "post_body": payload,
        })
        time.sleep(0.3)
    return {
        "operation": "POST /campaigns (one per entry)",
        "control_campaigns_touched": "none - read only",
        "created_status": "draft (sends nothing until activated in the UI)",
        "entries": entries,
    }


def execute(entries: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Create the campaigns. Called only under ``--execute``."""
    created: List[Dict[str, Any]] = []
    for entry in entries:
        response = _request("POST", "campaigns", entry["post_body"])
        created.append({
            "campaign_key": entry["campaign_key"],
            "role_buckets": entry["role_buckets"],
            "challenger_campaign_id": response.get("id"),
            "challenger_name": response.get("name"),
            "status": response.get("status"),
        })
        print(f"created {entry['campaign_key']}: {response.get('id')}")
        time.sleep(0.5)
    return created


def challenger_campaign_env(created: Sequence[Dict[str, Any]]) -> str:
    """The ``OUTBOUND_WAVE1_CHALLENGER_CAMPAIGNS_JSON`` value for what was created."""
    mapping: Dict[str, str] = {}
    for item in created:
        for bucket in item.get("role_buckets") or []:
            if item.get("challenger_campaign_id"):
                mapping[bucket] = str(item["challenger_campaign_id"])
    return json.dumps(mapping, separators=(",", ":"), sort_keys=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--buckets",
        default="",
        help="Comma-separated campaign keys to build (default: all nine). "
             "Use this to create the rollout's first campaigns only.",
    )
    parser.add_argument("--out", default="reports/wave1_challenger_plan.json")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually POST the campaigns. Without this the script only reads.",
    )
    args = parser.parse_args()

    wanted = {part.strip().lower() for part in args.buckets.split(",") if part.strip()}
    policies = [p for p in CAMPAIGNS if not wanted or p.key in wanted]
    unknown = wanted - {p.key for p in CAMPAIGNS}
    if unknown:
        raise SystemExit(f"unknown campaign keys: {sorted(unknown)}")

    artifact = plan(policies)
    artifact["executed"] = bool(args.execute)
    if args.execute:
        artifact["created"] = execute(artifact["entries"])
        artifact["OUTBOUND_WAVE1_CHALLENGER_CAMPAIGNS_JSON"] = challenger_campaign_env(
            artifact["created"]
        )

    directory = os.path.dirname(os.path.abspath(args.out))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(artifact, handle, indent=2, ensure_ascii=False)

    for entry in artifact["entries"]:
        print(
            f"{entry['campaign_key']:22} control={entry['control_campaign_id']} "
            f"steps {entry['control_step_count']}->{entry['challenger_step_count']} "
            f"senders={entry['control_sender_pool_size']}  {entry['challenger_name']}"
        )
    print(f"\nartifact: {args.out}")
    if not args.execute:
        print("DRY RUN: nothing was created. Re-run with --execute to create.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
