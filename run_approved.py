"""Poll Airtable for Approved leads and enroll them in Instantly.

Designed for a short-lived scheduler/cron process. The process exits with a
non-zero status when one or more leads fail so the hosting platform can mark
that execution as failed and surface it in monitoring.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import airtable_client
import config
import instantly_client

Path(config.LOG_DIR).mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(
            Path(config.LOG_DIR) / f"approved_{datetime.now():%Y-%m-%d}.log",
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


def run() -> dict:
    approved = airtable_client.get_approved_leads()
    if not approved:
        result = {"approved": 0, "enrolled": 0, "duplicates": 0, "failed": 0}
        logger.info("No approved leads waiting")
        return result

    result = instantly_client.enroll_approved_leads(approved)
    if result["enrolled_record_ids"]:
        airtable_client.mark_enrolled(result["enrolled_record_ids"])

    for failure in result["failures"]:
        airtable_client.mark_error([failure["record_id"]], failure["error"])

    result["approved"] = len(approved)
    logger.info("Enrollment result: %s", json.dumps(result, indent=2))
    return result


def main() -> int:
    try:
        result = run()
    except Exception:
        logger.exception("Approved-lead sync crashed")
        return 1

    return 1 if int(result.get("failed", 0)) > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
