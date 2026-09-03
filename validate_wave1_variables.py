"""Staged validation of the Instantly custom-variable defect, on ONE campaign.

WHAT WENT WRONG
---------------
The nine Challenger campaigns were created with bodies containing
``{{rendered_email_N_html}}`` and a ``custom_variables`` declaration. Instantly
returned 200 with status 0 (Draft) but stored neither: every body came back as
just the signature block, and ``custom_variables`` came back empty on all nine.

The v2 documentation explains why. Custom variables are LEAD-scoped, not
campaign-scoped. ``POST /api/v2/leads`` says of ``custom_variables``:

    "Custom variables can include any metadata about the lead that is relevant
    to the campaign, the campaign will be updated to allow all the other leads
    in the campaign to have the same custom variables."

So a campaign learns a variable name from a lead that carries it. At creation
time our campaigns had no leads, every ``{{rendered_*}}`` token was therefore
unknown, and the body sanitiser dropped it. The subject kept its token because
subjects are not run through that sanitiser.

WHAT THIS SCRIPT DOES
---------------------
Three stages, resumable, each requiring ``--execute``:

    lead    POST one SYNTHETIC lead carrying every Wave 1 variable name
    patch   PATCH the campaign's four bodies back to the intended shape
    verify  GET the campaign and the lead and report what persisted

SAFETY
------
This script can write to exactly two places, both hard-bound to the PRODUCT
Challenger and neither of them a Control:

    POST  /api/v2/leads                  (body.campaign must equal PRODUCT_ID)
    PATCH /api/v2/campaigns/<PRODUCT_ID>

Every other write raises. There is no campaign-id argument: the target is a
module constant, so no invocation can point this at another campaign. It never
activates or pauses anything -- those are separate endpoints in the v2 API
(``POST /campaigns/{id}/activate`` and ``/pause``), and neither is reachable
from here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, Optional
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402

#: The ONLY campaign this script may ever touch. Not an argument, on purpose.
PRODUCT_CHALLENGER_ID = "7b9aa5f3-fe46-49fa-b2ac-fee1da346ed0"
PRODUCT_CHALLENGER_NAME = "WAVE1 CHALLENGER - PRODUCT"

#: Every live Control. Belt and braces: if any of these ever appeared as a
#: write target the guard raises before a request is built.
CONTROL_CAMPAIGN_IDS = frozenset({
    "45ac1e03-67e7-4bdd-b372-808042104e4c",  # PRODUCT
    "4effab2f-9073-46a9-b7ae-986ccc8f49c6",  # OPERATIONS
    "1db88bbe-b2cf-4574-a5b7-1cb948151a86",  # FINANCE
    "cf01e56b-e5ad-489e-a02c-c35c93cf3b53",  # PEOPLE & HR
    "0f0f57d5-fab1-436d-b0d8-8cb43b031f03",  # ECOMMERCE
    "1747c87e-12e9-4477-bc4d-048223d39513",  # CUSTOMER EXPERIENCE
    "165c9e87-c3e7-4e9c-9ccb-a8dbf5779726",  # MARKETING & CREATIVE
    "917973f3-c282-4a84-8da4-525a7a91819b",  # GTM SYSTEMS
    "04670c6a-828b-42cd-9dad-904592a63d9b",  # AI & TECHNICAL
})

#: Exactly two permitted writes, both bound to the one campaign.
ALLOWED_WRITES = frozenset({
    ("POST", "leads"),
    ("PATCH", f"campaigns/{PRODUCT_CHALLENGER_ID}"),
})

SIGNATURE_BLOCK = "<div><br /></div><div>{{accountSignature}}</div>"

#: Sentinels chosen so the four possible outcomes are told apart at a glance:
#:   A rendered as markup   -> two separate lines, "&" shown as an ampersand
#:   B escaped literally    -> the tags are visible as text
#:   C variable removed     -> no sentinel anywhere
#:   D replaced by blank    -> an empty gap where the sentinel should be
HTML_SENTINELS = {
    1: "<div>WAVE1_HTML_ALPHA</div><div><br /></div><div>WAVE1_HTML_BETA &amp; GAMMA</div>",
    2: "<div>WAVE1_HTML_DELTA</div>",
    3: "<div>WAVE1_HTML_EPSILON</div>",
    4: "<div>WAVE1_HTML_ZETA</div>",
}

#: Every variable name a Wave 1 enrolment sends, so the campaign registers all
#: of them from this one lead.
WAVE1_VARIABLE_NAMES = (
    "rendered_subject",
    "rendered_email_1_html", "rendered_email_2_html",
    "rendered_email_3_html", "rendered_email_4_html",
    "rendered_email_1", "rendered_email_2", "rendered_email_3", "rendered_email_4",
    "experiment_id", "experiment_arm", "company_assignment_key", "wave1_campaign",
    "signal_tier", "signal_type", "friction_angle", "proof_type", "claim_source",
    "outbound_offer_type", "offer_noun", "offer_class", "offer_fallback_type",
    "copy_version", "role_page_match",
)


class UnauthorisedWrite(RuntimeError):
    """A write outside the two permitted operations, or aimed at another campaign."""


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
        "User-Agent": "TGTC-wave1-variable-validation/1.0",
    }


def _guard(method: str, path: str, body: Optional[Dict[str, Any]]) -> None:
    """Refuse anything that is not one of the two permitted writes."""
    normalised = (method.upper(), path.strip("/").split("?")[0])
    if normalised[0] == "GET":
        return
    if normalised not in ALLOWED_WRITES:
        raise UnauthorisedWrite(f"{method} {path} is not permitted by this script")
    if normalised[0] == "POST":
        target = str((body or {}).get("campaign") or "")
        if target != PRODUCT_CHALLENGER_ID:
            raise UnauthorisedWrite(
                f"lead would be created in campaign {target!r}, not the PRODUCT "
                f"Challenger {PRODUCT_CHALLENGER_ID}")
    if normalised[1].removeprefix("campaigns/") in CONTROL_CAMPAIGN_IDS:
        raise UnauthorisedWrite("refusing to write to a Control campaign")


def _request(
    method: str,
    path: str,
    body: Optional[Dict[str, Any]] = None,
    *,
    execute: bool = False,
) -> Dict[str, Any]:
    _guard(method, path, body)
    if method.upper() != "GET" and not execute:
        raise UnauthorisedWrite(f"{method} {path} attempted without --execute")
    url = f"{_base()}/{path.lstrip('/')}"
    data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
    last: Exception | None = None
    for attempt in range(4):
        request = Request(url, method=method.upper(), data=data, headers=_headers())
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
                time.sleep(2.0 * (attempt + 1))
                last = exc
                continue
            raise RuntimeError(f"{method} {path} -> {exc.code} {detail}") from exc
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{method} {path} failed: {last}")


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------

def synthetic_variables() -> Dict[str, str]:
    """Sentinel values for every Wave 1 variable name.

    Values are strings only: the docs restrict custom variable values to string,
    number, boolean or null.
    """
    values: Dict[str, str] = {
        "rendered_subject": "WAVE1_SUBJECT_SENTINEL",
        "experiment_id": "wave1_variable_validation",
        "experiment_arm": "B",
        "company_assignment_key": "domain:wave1-validation.invalid",
        "wave1_campaign": "PRODUCT",
        "signal_tier": "T3",
        "signal_type": "active_req",
        "friction_angle": "headcount_is_a_factor",
        "proof_type": "headcount_model",
        "claim_source": "WAVE1_NO_CLAIM_SENTINEL",
        "outbound_offer_type": "headcount_overview",
        "offer_noun": "how the headcount side works",
        "offer_class": "process_explainer",
        "offer_fallback_type": "none",
        "copy_version": "wave1-validation/1",
        "role_page_match": "0",
    }
    for index in (1, 2, 3, 4):
        values[f"rendered_email_{index}_html"] = HTML_SENTINELS[index]
        values[f"rendered_email_{index}"] = f"WAVE1_PLAIN_SENTINEL_{index}"
    missing = set(WAVE1_VARIABLE_NAMES) - set(values)
    assert not missing, f"variable names missing from the synthetic lead: {sorted(missing)}"
    return values


def synthetic_lead_payload(email: str) -> Dict[str, Any]:
    """The exact POST /api/v2/leads body. Only documented fields are used."""
    return {
        "campaign": PRODUCT_CHALLENGER_ID,
        "email": email,
        "first_name": "Wave1",
        "last_name": "Validation",
        "company_name": "WAVE1 VALIDATION (synthetic)",
        "website": "https://wave1-validation.invalid",
        # Do not create a duplicate if this stage is retried.
        "skip_if_in_campaign": True,
        # Verification would bill and would flag a synthetic address.
        "verify_leads_on_import": False,
        "custom_variables": synthetic_variables(),
    }


def intended_bodies() -> Dict[int, str]:
    return {
        index: f"{{{{rendered_email_{index}_html}}}}{SIGNATURE_BLOCK}"
        for index in (1, 2, 3, 4)
    }


def campaign_patch_payload(current: Dict[str, Any]) -> Dict[str, Any]:
    """Rebuild the campaign's sequences with the intended bodies restored.

    Everything else about the campaign is left alone: this sends ONLY the
    ``sequences`` field, so name, sender pool, schedule, timezone, limits,
    tracking, stop conditions and status are untouched by construction rather
    than by being carefully re-sent. Delays and subjects are carried over from
    what is live, so the patch cannot silently change them either.
    """
    sequences = json.loads(json.dumps(current.get("sequences") or []))
    if not sequences:
        raise RuntimeError("campaign has no sequences to patch")
    steps = sequences[0].get("steps") or []
    if len(steps) != 4:
        raise RuntimeError(f"expected 4 steps, found {len(steps)}")
    bodies = intended_bodies()
    for index, step in enumerate(steps, start=1):
        variants = step.get("variants") or []
        if len(variants) != 1:
            raise RuntimeError(f"step {index} has {len(variants)} variants, expected 1")
        variants[0]["body"] = bodies[index]
    return {"sequences": sequences}


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

def _state_path(out: str) -> str:
    return f"{out}.state.json"


def _load(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _save(path: str, state: Dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def get_campaign() -> Dict[str, Any]:
    return _request("GET", f"campaigns/{quote(PRODUCT_CHALLENGER_ID, safe='')}")


def describe_campaign(campaign: Dict[str, Any]) -> Dict[str, Any]:
    steps = (campaign.get("sequences") or [{}])[0].get("steps") or []
    variables = campaign.get("custom_variables")
    return {
        "name": campaign.get("name"),
        "status": campaign.get("status"),
        "sender_count": len(campaign.get("email_list") or []),
        "step_count": len(steps),
        "delays": [s.get("delay") for s in steps],
        "subjects": [(s.get("variants") or [{}])[0].get("subject") for s in steps],
        "bodies": [(s.get("variants") or [{}])[0].get("body") for s in steps],
        "body_tokens_present": [
            f"{{{{rendered_email_{i}_html}}}}" in str(
                (s.get("variants") or [{}])[0].get("body") or "")
            for i, s in enumerate(steps, start=1)
        ],
        "custom_variable_count": len(variables) if isinstance(variables, (dict, list)) else 0,
        "custom_variables": variables,
    }


def stage_lead(email: str, state: Dict[str, Any], *, execute: bool) -> Dict[str, Any]:
    payload = synthetic_lead_payload(email)
    if not execute:
        return {"would_post": payload}
    response = _request("POST", "leads", payload, execute=True)
    state["lead"] = {
        "id": response.get("id"),
        "email": response.get("email"),
        "campaign": response.get("campaign"),
        "custom_variables": response.get("custom_variables"),
    }
    return state["lead"]


def stage_patch(state: Dict[str, Any], *, execute: bool) -> Dict[str, Any]:
    current = get_campaign()
    payload = campaign_patch_payload(current)
    if not execute:
        return {"would_patch": payload}
    response = _request(
        "PATCH", f"campaigns/{PRODUCT_CHALLENGER_ID}", payload, execute=True)
    state["patch"] = {"status_after": response.get("status")}
    return state["patch"]


#: Literal marker for the discriminating probe. It contains no variable syntax,
#: so it cannot be removed by a merge-variable sanitiser -- only by the sequence
#: body not being written at all.
PATCH_PROBE_MARKER = "WAVE1_PATCH_PROBE"


def probe_patch_payload(current: Dict[str, Any]) -> Dict[str, Any]:
    """Change STEP 1 ONLY, carrying both a literal marker and the variable.

    Steps 2-4 are passed through byte-identical to what is stored, so the only
    thing this patch can alter is step one's body. Three outcomes tell apart the
    two hypotheses we could not separate before:

        marker + token   PATCH writes sequences AND the registered variable is
                         now accepted
        marker only      PATCH writes sequences, but the variable is still
                         stripped despite the campaign knowing its name
        neither          PATCH is not applying the sequence body at all

    The previous attempt could not distinguish these, because a stripped token
    left the body byte-identical to what was already there.
    """
    sequences = json.loads(json.dumps(current.get("sequences") or []))
    if not sequences:
        raise RuntimeError("campaign has no sequences to patch")
    steps = sequences[0].get("steps") or []
    if len(steps) != 4:
        raise RuntimeError(f"expected 4 steps, found {len(steps)}")
    stored = [(s.get("variants") or [{}])[0].get("body") for s in steps]
    steps[0]["variants"][0]["body"] = (
        f"<div>{PATCH_PROBE_MARKER}</div><div><br /></div>"
        f"{{{{rendered_email_1_html}}}}{SIGNATURE_BLOCK}"
    )
    # Steps 2-4 must be byte-identical to what is live.
    for index in (1, 2, 3):
        assert (steps[index].get("variants") or [{}])[0].get("body") == stored[index], (
            f"step {index + 1} body was altered; the probe must touch step 1 only")
    return {"sequences": sequences}


def stage_probe(state: Dict[str, Any], *, execute: bool) -> Dict[str, Any]:
    current = get_campaign()
    payload = probe_patch_payload(current)
    if not execute:
        return {"would_patch": payload}
    response = _request(
        "PATCH", f"campaigns/{PRODUCT_CHALLENGER_ID}", payload, execute=True)
    state["probe"] = {"patch_response": response}
    return state["probe"]


def classify_probe(campaign: Dict[str, Any]) -> Dict[str, Any]:
    """A / B / C, read off the STORED step-1 body."""
    steps = (campaign.get("sequences") or [{}])[0].get("steps") or []
    body = str((steps[0].get("variants") or [{}])[0].get("body") or "") if steps else ""
    marker = PATCH_PROBE_MARKER in body
    token = "{{rendered_email_1_html}}" in body
    if marker and token:
        verdict, meaning = "A", "PATCH writes sequences and the registered variable is accepted"
    elif marker:
        verdict, meaning = "B", "PATCH writes sequences, but Instantly still strips the variable token"
    else:
        verdict, meaning = "C", "PATCH is not applying the sequence body"
    return {"verdict": verdict, "meaning": meaning,
            "marker_present": marker, "token_present": token, "step_1_body": body}


def stage_verify(state: Dict[str, Any]) -> Dict[str, Any]:
    campaign = describe_campaign(get_campaign())
    result: Dict[str, Any] = {"campaign": campaign}
    lead_id = ((state.get("lead") or {}).get("id")) or ""
    if lead_id:
        lead = _request("GET", f"leads/{quote(str(lead_id), safe='')}")
        result["lead"] = {
            "id": lead.get("id"),
            "email": lead.get("email"),
            "campaign": lead.get("campaign"),
            "custom_variables": lead.get("custom_variables"),
        }
    print("VERIFY")
    print(f"  name                  {campaign['name']}")
    print(f"  status                {campaign['status']} "
          f"({'draft' if campaign['status'] == 0 else 'NOT DRAFT'})")
    print(f"  senders / steps       {campaign['sender_count']} / {campaign['step_count']}")
    print(f"  delays                {campaign['delays']}")
    print(f"  subjects              {campaign['subjects']}")
    print(f"  body tokens present   {campaign['body_tokens_present']} "
          f"({sum(campaign['body_tokens_present'])}/4)")
    print(f"  campaign variables    {campaign['custom_variable_count']}")
    if "lead" in result:
        variables = result["lead"].get("custom_variables") or {}
        print(f"  lead variables        {len(variables)}")
        missing = [n for n in WAVE1_VARIABLE_NAMES if n not in variables]
        print(f"  lead missing names    {missing or 'none'}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True,
                        choices=("lead", "patch", "probe", "verify"))
    parser.add_argument("--email", default="", help="Synthetic lead address (stage=lead).")
    parser.add_argument("--out", default="reports/wave1_variable_validation.json")
    parser.add_argument(
        "--execute", action="store_true",
        help="Perform the write. Without it the stage only prints what it would send.")
    args = parser.parse_args()

    state_path = _state_path(args.out)
    state = _load(state_path)
    artifact: Dict[str, Any] = {
        "campaign_id": PRODUCT_CHALLENGER_ID,
        "campaign_name": PRODUCT_CHALLENGER_NAME,
        "stage": args.stage,
        "executed": bool(args.execute),
    }

    if args.stage == "lead":
        if not args.email:
            raise SystemExit("--email is required for stage=lead")
        artifact["result"] = stage_lead(args.email, state, execute=args.execute)
    elif args.stage == "probe":
        artifact["result"] = stage_probe(state, execute=args.execute)
        if args.execute:
            artifact["classification"] = classify_probe(get_campaign())
            print("CLASSIFICATION:", json.dumps(artifact["classification"], indent=2))
    elif args.stage == "patch":
        if args.execute and not (state.get("lead") or {}).get("id"):
            raise SystemExit(
                "Refusing to patch: no synthetic lead recorded. Run --stage lead "
                "first, so the campaign has learned the variable names.")
        artifact["result"] = stage_patch(state, execute=args.execute)
    else:
        artifact["result"] = stage_verify(state)

    if args.execute:
        _save(state_path, state)
    _save(args.out, artifact)
    print(json.dumps(artifact["result"], indent=2, ensure_ascii=False)[:3000])
    print(f"\nartifact: {args.out}")
    if not args.execute and args.stage != "verify":
        print("DRY RUN: nothing was written. Re-run with --execute.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
