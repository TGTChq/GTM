#!/usr/bin/env python
"""Read Outbound Wave 1 outcomes from Instantly. Read-only, by construction.

    python -u run_wave1_outcomes.py
    python -u run_wave1_outcomes.py --json reports/wave1_outcomes.json

Listing reads only (``POST /leads/list``). This entry point cannot enroll a lead,
change a campaign, pause a sequence, patch an Airtable row or send anything. It
imports one function that performs one kind of request.

It prints counts and rates. It never prints an email address, a name, or any lead
text -- ``--json`` writes the full outcome map, keyed by email, to a file under
``reports/`` (gitignored) for the analysis step to join against the randomisation
frame.

Exit status is 0 whenever a report was produced, including a partial one: a
reporting job must never take down whatever chains it. ``--strict`` opts into a
non-zero status when any campaign failed or was truncated.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Optional, Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read Wave 1 outcomes from Instantly (read-only).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--campaign-id", action="append", default=[],
        help="Restrict to these campaign ids (repeatable). Defaults to every "
             "Control and Challenger campaign this deployment can deliver into.")
    parser.add_argument(
        "--since", default="",
        help="Ignore leads created before this ISO-8601 instant. Defaults to "
             "OUTBOUND_WAVE1_MIN_RECORD_CREATED_AT, the experiment watermark.")
    parser.add_argument(
        "--max-seconds", type=int, default=0,
        help="Wall-clock budget for provider reads (0 = unlimited). Campaigns not "
             "reached are named rather than silently counted as empty.")
    parser.add_argument("--json", dest="json_out",
                        help="Write the full outcome map here (keyed by email).")
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero when any campaign failed or was truncated.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    import config
    from outbound_wave1 import outcomes as outcomes_mod

    deadline = None
    if args.max_seconds:
        import time

        deadline = time.monotonic() + float(args.max_seconds)

    kwargs = {"deadline": deadline}
    if args.since:
        kwargs["since"] = args.since
    if args.campaign_id:
        collection = outcomes_mod.collect_outcomes(
            args.campaign_id,
            api_key=str(getattr(config, "INSTANTLY_API_KEY", "") or ""),
            base_url=str(getattr(config, "INSTANTLY_BASE_URL", "")
                         or "https://api.instantly.ai/api/v2"),
            since=str(kwargs.get("since", getattr(
                config, "OUTBOUND_WAVE1_MIN_RECORD_CREATED_AT", "") or "")),
            deadline=deadline,
        )
    else:
        collection = outcomes_mod.collect_for_experiment(config, **kwargs)

    described = collection.to_dict()
    rows = list(collection.rows.values())
    tally = Counter()
    for row in rows:
        for field in outcomes_mod.OUTCOME_FIELDS:
            if field == "reply_step":
                continue
            if row.get(field):
                tally[field] += 1
    steps = Counter(str(r.get("reply_step")) for r in rows if r.get("reply_step") is not None)

    print("=========== WAVE 1 OUTCOMES (read-only) ===========")
    print(f"{'contacts':<34}{described['contacts']}")
    print(f"{'leads scanned':<34}{described['leads_scanned']}")
    print(f"{'campaigns read':<34}{len(described['campaigns_read'])}")
    for field in outcomes_mod.OUTCOME_FIELDS:
        if field == "reply_step":
            continue
        print(f"{'  ' + field:<34}{tally.get(field, 0)}")
    if steps:
        print(f"{'  reply steps':<34}{dict(sorted(steps.items()))}")
    print(f"{'replies w/ unread interest':<34}"
          f"{described['replies_with_unclassified_interest']}"
          "   (counted in NO outcome, never as a zero)")
    if described["campaigns_failed"]:
        print(f"{'campaigns FAILED':<34}{described['campaigns_failed']}")
    if described["campaigns_truncated"]:
        print(f"{'campaigns TRUNCATED':<34}{described['campaigns_truncated']}")
    for error in described["errors"]:
        print(f"  error: {error}")
    print(f"{'complete':<34}{described['ok']}")
    print(f"{'writes performed':<34}none")
    print("===================================================")

    if args.json_out:
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"summary": described, "outcomes": collection.rows}, indent=1),
            encoding="utf-8")
        print(f"wrote {path}")

    if args.strict and not collection.ok:
        print("strict: the collection is incomplete", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
