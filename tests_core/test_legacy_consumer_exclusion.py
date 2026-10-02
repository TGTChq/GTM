"""The legacy Approved Sync worker must classify a core-written row as LEGACY and skip it
without a write. Read-only import of the legacy module; nothing else is touched."""

from __future__ import annotations

import importlib
import os

import pytest

from tests_core.test_approval_gate import _inputs
from tgtc_core.domain import approval as ap


def test_legacy_approved_row_eligibility_skips_core_rows():
    os.environ.setdefault("PYTEST_CURRENT_TEST", "1")
    if os.environ.get("TGTC_REQUIRE_LEGACY_CONSUMER_TEST") != "1":
        pytest.importorskip("dotenv", reason="legacy dependency absent in core-only environment")
    # The required CI gate installs legacy dependencies. Import defects there,
    # and unexpected runtime defects anywhere, must fail instead of being skipped.
    legacy = importlib.import_module("airtable_client")
    out = ap.build_approved_lead(**_inputs())
    fields = ap.airtable_fields(out.lead, out.fingerprint)
    category, reason = legacy.approved_row_eligibility(fields)
    assert category == "legacy"
    assert reason == "validation_version_mismatch"
    ok, reason2 = legacy.send_safe_facts(fields)
    assert ok is False and reason2 == "validation_version_mismatch"
