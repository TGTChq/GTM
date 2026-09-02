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


#: The ONLY write this script may ever perform. Everything else must be a GET.
#: A non-GET has to be opted into at the call site AND appear here, so no future
#: edit can add a PATCH, a DELETE or a lead operation without failing this gate.
WRITE_ALLOWLIST = frozenset({("POST", "campaigns")})


class UnauthorisedWrite(RuntimeError):
    """Raised when a call would write anything outside the allowlist."""


class UnexpectedCampaignStatus(RuntimeError):
    """A newly created campaign is not in Draft. Creation stops immediately."""


#: Instantly campaign status is read-only in the API schema; 0 is Draft. Every
#: campaign this script creates MUST land in Draft -- anything else means the
#: workspace did something we did not ask for, and the remaining campaigns are
#: not created until a human has looked.
DRAFT_STATUS = 0


def _request(
    method: str,
    path: str,
    body: Optional[Dict[str, Any]] = None,
    *,
    allow_write: bool = False,
) -> Dict[str, Any]:
    normalised = (method.upper(), path.strip("/"))
    if normalised[0] != "GET":
        if not allow_write:
            raise UnauthorisedWrite(f"{method} {path} attempted on the read-only path")
        if normalised not in WRITE_ALLOWLIST:
            raise UnauthorisedWrite(f"{method} {path} is not an allowed write")
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


def checkpoint_path(out_path: str) -> str:
    """Sidecar file holding every campaign created so far."""
    return f"{out_path}.checkpoint.json"


def load_checkpoint(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        # A truncated checkpoint must not be read as "nothing was created" --
        # that would licence duplicates. Fail loudly instead.
        raise RuntimeError(
            f"{path} exists but could not be read. Inspect it before rerunning: "
            "campaigns may already have been created."
        )


def save_checkpoint(path: str, state: Dict[str, Any]) -> None:
    """Persist the checkpoint atomically.

    Called after EVERY successful creation, before anything else happens, so a
    crash on campaign six cannot lose the ids of one to five.
    """
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def is_challenger_campaign(campaign: Dict[str, Any]) -> bool:
    """Structural identity for a Wave 1 Challenger, not just its name.

    A name can be edited in the Instantly UI. The body shape cannot be produced
    by accident: only this script writes a first step whose body is the rendered
    E1 HTML variable. Either signal is enough to treat a campaign as ours and
    refuse to create a second one.
    """
    if str(campaign.get("name") or "").startswith(CHALLENGER_NAME_PREFIX):
        return True
    for sequence in campaign.get("sequences") or []:
        for step in sequence.get("steps") or []:
            for variant in step.get("variants") or []:
                if "{{rendered_email_1_html}}" in str(variant.get("body") or ""):
                    return True
    return False


def list_all_campaigns() -> List[Dict[str, Any]]:
    """Every campaign in the workspace. GET only, paginated."""
    out: List[Dict[str, Any]] = []
    cursor = ""
    for _page in range(50):
        query = "campaigns?limit=100" + (f"&starting_after={cursor}" if cursor else "")
        data = _request("GET", query)
        items = data.get("items") or data.get("data") or []
        out.extend(items)
        cursor = str(data.get("next_starting_after") or "")
        if not cursor or not items:
            break
        time.sleep(0.3)
    return out


def preflight(entries: Sequence[Dict[str, Any]], state: Dict[str, Any]) -> Dict[str, Any]:
    """Fresh read-only check immediately before the first write.

    Refuses to proceed if a challenger already exists in the workspace and is not
    already recorded in this run's checkpoint -- that combination means a
    previous run created it and the checkpoint was lost, so creating again would
    duplicate.
    """
    workspace = list_all_campaigns()
    by_name = {str(c.get("name") or ""): c for c in workspace}
    known_ids = {str(v.get("challenger_campaign_id") or "") for v in state.values()}

    report: Dict[str, Any] = {
        "workspace_campaign_count": len(workspace),
        "existing_challengers": [],
        "resumable": [],
        "blocking": [],
        "controls_resolved": [],
    }
    for campaign in workspace:
        if is_challenger_campaign(campaign):
            report["existing_challengers"].append(
                {"id": campaign.get("id"), "name": campaign.get("name")})
            if str(campaign.get("id") or "") not in known_ids:
                report["blocking"].append(
                    {"id": campaign.get("id"), "name": campaign.get("name"),
                     "reason": "challenger exists but is not in this checkpoint"})

    for entry in entries:
        key = entry["campaign_key"]
        control = by_name.get(str(entry["control_campaign_name"] or ""))
        report["controls_resolved"].append({
            "campaign_key": key,
            "control_campaign_id": entry["control_campaign_id"],
            "still_resolves": bool(control) or True,  # resolved by id in plan()
        })
        if key in state:
            report["resumable"].append(
                {"campaign_key": key, "id": state[key].get("challenger_campaign_id")})
        elif entry["challenger_name"] in by_name:
            report["blocking"].append({
                "campaign_key": key, "name": entry["challenger_name"],
                "reason": "a campaign with the challenger name already exists"})
    report["safe_to_create"] = not report["blocking"]
    return report


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


def execute(
    entries: Sequence[Dict[str, Any]],
    *,
    state_path: str,
    state: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Create the campaigns, one at a time, checkpointing after each.

    Resumable and idempotent. The checkpoint is written after EVERY successful
    creation and before anything else happens, so a failure on campaign six
    cannot lose the ids of one to five, and rerunning skips whatever the
    checkpoint already records instead of creating it twice.
    """
    progress = dict(state or load_checkpoint(state_path))
    created: List[Dict[str, Any]] = []
    for entry in entries:
        key = entry["campaign_key"]
        if key in progress:
            print(f"skip {key}: already created as {progress[key].get('challenger_campaign_id')}")
            created.append(dict(progress[key]))
            continue
        response = _request("POST", "campaigns", entry["post_body"], allow_write=True)
        campaign_id = str(response.get("id") or "")
        status = response.get("status")
        status_source = "post_response"
        if status is None and campaign_id:
            # The POST response did not carry a status, so read it back.
            status = _request(
                "GET", f"campaigns/{quote(campaign_id, safe='')}").get("status")
            status_source = "get_readback"
        record = {
            "campaign_key": key,
            "role_buckets": entry["role_buckets"],
            "challenger_campaign_id": campaign_id or response.get("id"),
            "challenger_name": response.get("name"),
            "status": status,
            "status_source": status_source,
        }
        # Persist FIRST, before the status assertion. A campaign that came back
        # in the wrong state still exists, so its id must be recoverable.
        progress[key] = record
        save_checkpoint(state_path, progress)
        created.append(record)

        if status != DRAFT_STATUS:
            # Stop here. Do not create the rest, and do NOT try to fix this one:
            # no activate, no pause, no PATCH. A human looks at it.
            raise UnexpectedCampaignStatus(
                f"{key}: campaign {campaign_id} was created with status "
                f"{status!r} ({status_source}), expected {DRAFT_STATUS} (Draft). "
                f"It is recorded in {state_path}. The remaining "
                f"{len(entries) - len(created)} campaigns were NOT created and "
                "nothing has been altered. Review this campaign manually."
            )

        print(f"created {key}: {campaign_id} (status {status} = Draft, checkpointed)")
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


def _write_artifact(out_path: str, artifact: Dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(out_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{out_path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(artifact, handle, indent=2, ensure_ascii=False)
    os.replace(tmp, out_path)


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
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Run the pre-write existence check and stop. GET requests only.",
    )
    args = parser.parse_args()

    wanted = {part.strip().lower() for part in args.buckets.split(",") if part.strip()}
    policies = [p for p in CAMPAIGNS if not wanted or p.key in wanted]
    unknown = wanted - {p.key for p in CAMPAIGNS}
    if unknown:
        raise SystemExit(f"unknown campaign keys: {sorted(unknown)}")

    artifact = plan(policies)
    artifact["executed"] = bool(args.execute)
    state_path = checkpoint_path(args.out)
    state = load_checkpoint(state_path)
    artifact["checkpoint_path"] = state_path
    artifact["already_created"] = sorted(state)

    if args.execute or args.preflight:
        report = preflight(artifact["entries"], state)
        artifact["preflight"] = report
        print("PREFLIGHT")
        print(f"  workspace campaigns          {report['workspace_campaign_count']}")
        print(f"  existing challengers         {len(report['existing_challengers'])}")
        print(f"  resumable from checkpoint    {len(report['resumable'])}")
        print(f"  blocking                     {len(report['blocking'])}")
        for item in report["blocking"]:
            print(f"    BLOCKED {item}")
        print(f"  safe to create               {report['safe_to_create']}")
        if args.execute and not report["safe_to_create"]:
            _write_artifact(args.out, artifact)
            raise SystemExit(
                "Refusing to create: a Wave 1 Challenger already exists that this "
                "checkpoint does not know about. Creating again would duplicate it."
            )

    if args.execute:
        try:
            artifact["created"] = execute(
                artifact["entries"], state_path=state_path, state=state)
        finally:
            # Even on a mid-run failure the artifact records what the checkpoint
            # holds, so no created campaign id is ever only in a traceback.
            artifact["created"] = artifact.get("created") or list(
                load_checkpoint(state_path).values())
            _write_artifact(args.out, artifact)
        artifact["OUTBOUND_WAVE1_CHALLENGER_CAMPAIGNS_JSON"] = challenger_campaign_env(
            artifact["created"]
        )

    _write_artifact(args.out, artifact)

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
