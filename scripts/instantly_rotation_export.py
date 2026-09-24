"""Back up, and judge one by one, every contact rotation could remove.

An aggregate count is not proof that a row is safe. This walks the eligible campaigns
lead by lead, writes the FULL record (so the contact could be reconstructed), and
decides per row whether it may be removed, with the reason recorded either way.

A row is removable only when ALL of these hold:

* its campaign is `completed` and is not one of the nine Challenger campaigns, nor the
  active Control campaign;
* the contact itself finished the sequence (lead status 3): nothing is scheduled for it;
* it never replied (no reply timestamp and no reply count);
* it is not in our suppression list for a reason that implies unresolved ownership,
  and it is not an approval of ours still awaiting delivery.

Everything else is recorded with `removable: false` and the reason.

Output (personal data -- never the repository):

    <out>/leads/<campaign_id>.jsonl      every lead of an eligible campaign, verbatim
    <out>/challenger_emails.txt          emails live in the nine campaigns, for overlap
    <out>/candidates.jsonl               one decision row per lead
    <out>/manifest.json                  counts, sha256 of each file, totals

Run it with the workspace credentials:

    railway run -s <service with INSTANTLY_API_KEY> -- python scripts/instantly_rotation_export.py --out <dir>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

import requests

BASE = "https://api.instantly.ai/api/v2"
CHALLENGER = {
    "269cd138-00b1-48c3-9093-16c36120a20e": "CUSTOMER EXPERIENCE",
    "c3e81c21-db44-40f5-addc-d9945a78394b": "ECOMMERCE",
    "8bfa0769-4b9a-4346-8e93-17ac8b726dce": "AI & TECHNICAL",
    "7b319c7a-cc55-4e08-8a47-7058c345d8ae": "FINANCE",
    "8f25abd5-568a-4e88-b310-9acf85161c6c": "GTM SYSTEMS",
    "1feb6344-6065-49d7-9764-d125985fb9c9": "MARKETING",
    "69def27c-7799-41a2-9ba8-205e54ab071b": "OPERATIONS",
    "d2326028-e312-405b-9e16-526bd309d4dd": "PEOPLE & HR",
    "7b9aa5f3-fe46-49fa-b2ac-fee1da346ed0": "PRODUCT",
}
CONTROL = {
    "45ac1e03-67e7-4bdd-b372-808042104e4c", "4effab2f-9073-46a9-b7ae-986ccc8f49c6",
    "1db88bbe-b2cf-4574-a5b7-1cb948151a86", "cf01e56b-e5ad-489e-a02c-c35c93cf3b53",
    "0f0f57d5-fab1-436d-b0d8-8cb43b031f03", "1747c87e-12e9-4477-bc4d-048223d39513",
    "165c9e87-c3e7-4e9c-9ccb-a8dbf5779726", "917973f3-c282-4a84-8da4-525a7a91819b",
    "04670c6a-828b-42cd-9dad-904592a63d9b",
}
CAMPAIGN_COMPLETED, LEAD_FINISHED = 3, 3


def headers() -> Dict[str, str]:
    key = os.environ.get("INSTANTLY_API_KEY", "").strip()
    if not key:
        sys.exit("INSTANTLY_API_KEY is not set")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def campaigns(h) -> List[Dict[str, Any]]:
    out, after = [], None
    while True:
        params: Dict[str, Any] = {"limit": 100}
        if after:
            params["starting_after"] = after
        r = requests.get(BASE + "/campaigns", headers=h, params=params, timeout=60)
        r.raise_for_status()
        body = r.json()
        items = body.get("items", body if isinstance(body, list) else [])
        out.extend(items)
        after = body.get("next_starting_after") if isinstance(body, dict) else None
        if not after or not items:
            return out


def all_leads(h, campaign_id: str) -> Iterable[Dict[str, Any]]:
    after = None
    while True:
        payload: Dict[str, Any] = {"campaign": campaign_id, "limit": 100}
        if after:
            payload["starting_after"] = after
        r = requests.post(BASE + "/leads/list", headers=h, json=payload, timeout=90)
        r.raise_for_status()
        body = r.json()
        items = body.get("items", [])
        for lead in items:
            yield lead
        after = body.get("next_starting_after")
        if not after or not items:
            return


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def decide(lead: Dict[str, Any], *, campaign_id: str, campaign_name: str,
           challenger_emails: Set[str], held_emails: Set[str], suppressed: Set[str]) -> Dict[str, Any]:
    email = str(lead.get("email") or "").strip().lower()
    reasons: List[str] = []
    if campaign_id in CHALLENGER:
        reasons.append("live_challenger_campaign")
    if lead.get("status") != LEAD_FINISHED:
        reasons.append(f"sequence_not_finished:{lead.get('status')}")
    if lead.get("timestamp_last_reply") or int(lead.get("email_reply_count") or 0):
        reasons.append("has_reply")
    if email and email in held_emails:
        reasons.append("awaiting_delivery_for_us")
    if email and email in suppressed:
        reasons.append("suppressed_unresolved")
    return {
        "lead_id": str(lead.get("id") or ""), "campaign": campaign_id, "campaign_name": campaign_name,
        "email": email, "email_sha256": hashlib.sha256(email.encode()).hexdigest() if email else "",
        "status": lead.get("status"), "created": lead.get("timestamp_created"),
        "last_contact": lead.get("timestamp_last_contact"), "reply_count": lead.get("email_reply_count"),
        "last_reply": lead.get("timestamp_last_reply"),
        "also_live_in_challenger": bool(email and email in challenger_emails),
        "removable": not reasons, "reasons": reasons,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="private directory, NEVER inside the repository")
    ap.add_argument("--held-emails", default="", help="file of emails awaiting delivery for us (one per line)")
    ap.add_argument("--suppressed-emails", default="", help="file of suppressed emails (one per line)")
    args = ap.parse_args()

    out = Path(args.out)
    (out / "leads").mkdir(parents=True, exist_ok=True)
    h = headers()

    def read_set(path: str) -> Set[str]:
        if not path:
            return set()
        return {line.strip().lower() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()}

    held, suppressed = read_set(args.held_emails), read_set(args.suppressed_emails)

    # 1. the nine live campaigns: emails only, for the overlap flag
    challenger_emails: Set[str] = set()
    live_counts: Dict[str, int] = {}
    for cid, label in CHALLENGER.items():
        n = 0
        for lead in all_leads(h, cid):
            email = str(lead.get("email") or "").strip().lower()
            if email:
                challenger_emails.add(email)
            n += 1
        live_counts[label] = n
        print(f"live {label}: {n}", file=sys.stderr)
    (out / "challenger_emails.txt").write_text("\n".join(sorted(challenger_emails)), encoding="utf-8")

    # 2. every eligible campaign: full backup + a decision per lead
    manifest: Dict[str, Any] = {"at": datetime.now(timezone.utc).isoformat(), "campaigns": [],
                                "live_campaign_counts": live_counts, "files": {}}
    totals = {"leads": 0, "removable": 0, "also_live": 0}
    with (out / "candidates.jsonl").open("w", encoding="utf-8") as cand:
        for c in campaigns(h):
            cid, name, status = str(c.get("id") or ""), str(c.get("name") or "")[:60], c.get("status")
            if cid in CHALLENGER or status != CAMPAIGN_COMPLETED:
                continue
            group = "control_completed" if cid in CONTROL else "legacy_completed"
            path = out / "leads" / f"{cid}.jsonl"
            n, removable, also = 0, 0, 0
            with path.open("w", encoding="utf-8") as fh:
                for lead in all_leads(h, cid):
                    fh.write(json.dumps(lead, default=str) + "\n")
                    row = decide(lead, campaign_id=cid, campaign_name=name, challenger_emails=challenger_emails,
                                 held_emails=held, suppressed=suppressed)
                    row["group"] = group
                    cand.write(json.dumps(row) + "\n")
                    n += 1
                    removable += int(row["removable"])
                    also += int(row["also_live_in_challenger"])
            manifest["campaigns"].append({"id": cid, "name": name, "group": group, "leads": n,
                                          "removable": removable, "also_live_in_challenger": also,
                                          "file": path.name, "sha256": sha256(path)})
            totals["leads"] += n
            totals["removable"] += removable
            totals["also_live"] += also
            print(f"{group} {name}: {n} leads, {removable} removable, {also} also live", file=sys.stderr)

    for name in ("candidates.jsonl", "challenger_emails.txt"):
        manifest["files"][name] = {"sha256": sha256(out / name), "bytes": (out / name).stat().st_size}
    manifest["totals"] = totals
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(json.dumps({"totals": totals, "campaigns": len(manifest["campaigns"]),
                      "out": str(out)}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
