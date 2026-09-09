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
    try:
        legacy = importlib.import_module("airtable_client")
    except Exception as exc:  # noqa: BLE001 - environment-dependent legacy import
        pytest.skip(f"legacy airtable_client not importable here: {exc}")
    out = ap.build_approved_lead(**_inputs())
    fields = ap.airtable_fields(out.lead, out.fingerprint)
    category, reason = legacy.approved_row_eligibility(fields)
    assert category == "legacy"
    assert reason == "validation_version_mismatch"
    ok, reason2 = legacy.send_safe_facts(fields)
    assert ok is False and reason2 == "validation_version_mismatch"
