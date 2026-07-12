"""One-time safe backfill for blank Role Focus fields in Airtable."""

from __future__ import annotations

import json
import logging

import airtable_client


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = airtable_client.repair_missing_role_focus()
    print(json.dumps(result, indent=2), flush=True)
    return 1 if result.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
