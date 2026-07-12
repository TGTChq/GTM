"""Manually push a Step 3 enriched JSON file into Airtable."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import airtable_client
import config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "input_path",
        nargs="?",
        default=str(Path(config.STEP3_OUTPUT_DIR) / f"jobs_enriched_{datetime.now():%Y-%m-%d}.json"),
    )
    args = parser.parse_args()
    payload = json.loads(Path(args.input_path).read_text(encoding="utf-8"))
    result = airtable_client.push_leads(payload.get("jobs", []))
    print(json.dumps(result, indent=2))
    return 1 if result.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
