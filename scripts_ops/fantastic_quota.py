"""Read the Fantastic.jobs plan quota with ONE count request (zero job credits).

Prints the remaining-quota headers only. Never prints the API key.
"""
from __future__ import annotations

import os
import sys

import requests

base = os.environ.get("FANTASTIC_JOBS_BASE_URL", "https://data.fantastic.jobs").rstrip("/")
key = os.environ.get("FANTASTIC_JOBS_API_KEY", "")
if not key:
    sys.exit("FANTASTIC_JOBS_API_KEY not present")
resp = requests.get(f"{base}/v1/active-jb-count", headers={"Authorization": f"Bearer {key}"},
                    params={"time_frame": "24h"}, timeout=30)
print("status", resp.status_code)
for name, value in sorted(resp.headers.items()):
    if name.lower().startswith(("x-api-", "x-ratelimit", "ratelimit")):
        print(f"{name}: {value}")
try:
    print("body", str(resp.json())[:300])
except ValueError:
    print("body", resp.text[:300])
