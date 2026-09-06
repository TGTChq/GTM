"""Is Apollo willing to serve credit-consuming calls again? READ-ONLY.

One GET to ``organizations/enrich`` -- the same endpoint the run stops on. While
Apollo is refusing, the call costs nothing: it returns 422 before doing any work,
and the body carries the account's own numbers. When Apollo is serving again it
returns 200, which is the signal to resume.

Run it before restoring ``FANTASTIC_JOBS_ENABLED=1``. Resume on this, never on a
date: ``next_billing_date`` is what Apollo reports, not a promise. A cycle can roll
over without restoring lead credits if the plan allotment is the binding limit, if
an additional-usage cap is set, or if a payment is failing.

    PYTHONPATH=. railway run --no-local --service GTM -- python acceptance/apollo_readiness.py

Exit codes:  0 READY   1 STILL REFUSING   2 UNEXPECTED (look before resuming)
"""

from __future__ import annotations

import json
import sys

import requests

import config
from apollo_errors import ApolloErrorCategory, classify_apollo_error

BASE = "https://api.apollo.io/api/v1"
PROBE_DOMAIN = "stripe.com"  # any well-known domain; the point is the account, not the row


def main() -> int:
    key = config.APOLLO_API_KEY or ""
    if not key:
        print("UNEXPECTED: no APOLLO_API_KEY in this environment")
        return 2
    print("apollo key tail4=%s (never printed in full)" % key[-4:])

    headers = {"X-Api-Key": key, "Content-Type": "application/json",
               "Accept": "application/json", "Cache-Control": "no-cache"}
    try:
        response = requests.get(f"{BASE}/organizations/enrich", headers=headers,
                                params={"domain": PROBE_DOMAIN}, timeout=45)
    except Exception as exc:  # noqa: BLE001
        print("UNEXPECTED: transport error %s" % type(exc).__name__)
        return 2

    request_id = response.headers.get("x-request-id", "-")
    print("HTTP %s   x-request-id %s" % (response.status_code, request_id))

    if response.status_code == 200:
        print("READY: Apollo served a credit-consuming call.")
        print("Next: confirm pending_work is on the running commit, then restore")
        print("      FANTASTIC_JOBS_ENABLED=1 (see INCIDENT_2026-09-06_apollo_credits.md).")
        return 0

    exc = requests.HTTPError(response=response)
    exc.response = response
    classification = classify_apollo_error(exc)
    if classification.category is ApolloErrorCategory.CREDIT_EXHAUSTED:
        print("STILL REFUSING: %s" % (classification.error_code or "credit stop"))
        print("  %s" % classification.message)
        for name in ("credit_type", "credit_balance", "next_billing_date"):
            if name in classification.context:
                print("  %-18s %s" % (name, classification.context[name]))
        print("Do NOT resume acquisition: a run would buy postings it cannot enrich.")
        return 1

    print("UNEXPECTED: classified as %s (HTTP %s)"
          % (classification.category.value, classification.status))
    print("  %s" % classification.message)
    print("  raw: %s" % json.dumps((response.text or "")[:400]))
    print("Investigate before resuming -- this is not the known credit stop.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
