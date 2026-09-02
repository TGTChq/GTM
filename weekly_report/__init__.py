"""Reusable weekly / arbitrary-window reporting layer for the TGTC pipeline.

This package is **read-only**. Nothing here writes to Airtable or Instantly,
sends mail, enrolls a lead, touches a campaign, or mutates pipeline state. It
reads the immutable per-run artifacts the orchestrator already writes
(``<artifact-root>/run_artifacts/<run_id>/*.json``) and, when explicitly asked,
performs GET/list-only calls against Instantly and Airtable.

Design intent: the same data layer feeds a future real-time dashboard. A report
is therefore a *typed, versioned, provenance-carrying document*, not a formatted
string. Every metric records where it came from, which timestamp attributed it to
the window, and which runs contributed. A metric that cannot be reconstructed is
reported as ``unavailable`` with a reason -- it is never guessed, and never
silently coerced to zero.
"""

from __future__ import annotations

#: Bump only for a breaking change to the emitted document shape.
REPORT_SCHEMA = "tgtc-weekly-report/1"

#: Implementation version, recorded on every report for reproducibility.
REPORT_BUILDER_VERSION = "1.0.0"

__all__ = ["REPORT_SCHEMA", "REPORT_BUILDER_VERSION"]
