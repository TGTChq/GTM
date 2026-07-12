"""Backfill blank job-freshness and URL-quality fields in Airtable."""

from __future__ import annotations

import json

import airtable_client


if __name__ == "__main__":
    print(json.dumps(airtable_client.repair_missing_job_signals(), indent=2))
