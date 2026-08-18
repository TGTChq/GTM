from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

import config
import execute_role_display_v2_migration as ex
from prepare_role_display_v2_migration import (
    PROTECTED_CANONICAL_FIELDS,
    ROLE_PATCH_CANDIDATES,
    _fingerprint,
    build_airtable_patch,
    build_manifests,
)

SIGNING_KEY = "unit-test-signing-key"


def _record(rid, *, status="Pending", open_role, matched, email, campaign, outbound_role=""):
    return {
        "id": rid,
        "fields": {
            "Status": status,
            "Open Role": open_role,
            "Open Roles": open_role,
            "Matched Role": matched,
            "Role Bucket": "engineering",
            "Role Focus": "",
            "Email": email,
            "Campaign ID": campaign,
            "Company": "Acme",
            "Website": "acme.com",
            "Outbound Role": outbound_role,
            "Outbound Roles": outbound_role,
        },
    }


def _write_manifest(tmp: Path, summary, rows) -> tuple[Path, str]:
    payload = {"summary": summary, "allowed_patch_fields": list(ROLE_PATCH_CANDIDATES), "rows": rows}
    path = tmp / "reviewed.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


class PureLogicTests(unittest.TestCase):
    def test_reconcile_rows_detects_missing_new_and_material(self):
        reviewed = {"a": {"x": 1}, "b": {"x": 2}}
        fresh = {"a": {"x": 1}, "c": {"x": 9}}
        diffs = ex.reconcile_rows(reviewed, fresh, ("x",))
        reasons = {d["key"]: d["reason"] for d in diffs}
        self.assertEqual(reasons["b"], "missing_from_fresh_replan")
        self.assertEqual(reasons["c"], "new_record")
        self.assertNotIn("a", reasons)

    def test_reconcile_rows_flags_changed_field(self):
        diffs = ex.reconcile_rows({"a": {"x": 1}}, {"a": {"x": 2}}, ("x",))
        self.assertEqual(diffs[0], {"key": "a", "reason": "material_change", "fields": ["x"]})

    def test_instantly_patch_is_role_only_and_preserves_others(self):
        before = {"open_role": "Old", "open_roles": "Old", "first_name": "Sam", "company_name": "Acme"}
        patch, corrected = ex.build_instantly_role_patch(before, "Software Engineer")
        self.assertEqual(set(patch), {"custom_variables"})
        cv = patch["custom_variables"]
        self.assertEqual(cv["open_role"], "Software Engineer")
        self.assertEqual(cv["open_roles"], "Software Engineer")
        self.assertEqual(cv["first_name"], "Sam")            # unrelated preserved
        self.assertEqual(cv["company_name"], "Acme")          # company never rewritten
        self.assertEqual(sorted(corrected), ["custom_variables.open_role", "custom_variables.open_roles"])

    def test_instantly_patch_noop_when_already_at_target(self):
        patch, corrected = ex.build_instantly_role_patch(
            {"open_role": "Software Engineer", "open_roles": "Software Engineer"}, "Software Engineer")
        self.assertEqual(patch, {})
        self.assertEqual(corrected, [])

    def test_unrelated_custom_vars_change_detection(self):
        self.assertFalse(ex.unrelated_custom_vars_changed({"first_name": "A", "open_role": "x"},
                                                          {"first_name": "A", "open_role": "y"}))
        self.assertTrue(ex.unrelated_custom_vars_changed({"first_name": "A"}, {"first_name": "B"}))

    def test_canonical_drift_detects_protected_change(self):
        field = sorted(PROTECTED_CANONICAL_FIELDS)[0]
        self.assertEqual(ex.canonical_drift({field: "x"}, {field: "y"}), [field])
        self.assertEqual(ex.canonical_drift({field: "x"}, {field: "x"}), [])


class AirtablePatchContractTests(unittest.TestCase):
    def test_patch_is_role_only_never_canonical_and_fingerprint_valid(self):
        rec = _record("rec1", open_role="Software Engineer - Conversational AI",
                      matched="Software Engineer", email="a@x.com", campaign="c1")
        patch, meta = build_airtable_patch(rec, generated_at="2026-08-18T00:00:00+00:00", signing_key=SIGNING_KEY)
        self.assertFalse(meta["role_hold"])
        self.assertEqual(meta["proposed_outbound_role"], "Software Engineer")
        self.assertTrue(set(patch).issubset(set(ROLE_PATCH_CANDIDATES)))
        self.assertTrue(set(patch).isdisjoint(PROTECTED_CANONICAL_FIELDS))
        # Fingerprint recomputes over the signed record.
        signed = dict(rec["fields"])
        signed.update(patch)
        self.assertEqual(patch["Validation Fingerprint"], _fingerprint(signed, SIGNING_KEY))


class _FakeAirtableAPI:
    def __init__(self, store):
        self.store = store

    def patch_batch(self, batch):
        ids = []
        for item in batch:
            self.store[item["id"]] = item["fields"]
            ids.append(item["id"])
        return ids


class _SignedEnvMixin:
    def setUp(self):
        self._prev_key = os.environ.get("VALIDATION_SIGNING_KEY")
        os.environ["VALIDATION_SIGNING_KEY"] = SIGNING_KEY
        super().setUp()

    def tearDown(self):
        if self._prev_key is None:
            os.environ.pop("VALIDATION_SIGNING_KEY", None)
        else:
            os.environ["VALIDATION_SIGNING_KEY"] = self._prev_key
        super().tearDown()


class AirtableExecutionTests(_SignedEnvMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.records = [
            _record("recSafe", open_role="Software Engineer - Conversational AI",
                    matched="Software Engineer", email="safe@x.com", campaign="c1"),
            _record("recHold", open_role="Payroll Administrator/General Ledger Accountant",
                    matched="", email="hold@x.com", campaign="c1"),
        ]

    def _plan(self, **kw):
        result = build_manifests(copy.deepcopy(self.records), [], {},
                                 generated_at=kw["generated_at"], signing_key=kw["signing_key"])
        result["_records_by_id"] = {r["id"]: copy.deepcopy(r) for r in self.records}
        result["_campaigns"] = []
        return result

    def _reviewed(self, tmp):
        base = build_manifests(copy.deepcopy(self.records), [], {},
                               generated_at="2026-08-18T00:00:00+00:00", signing_key=SIGNING_KEY)
        return _write_manifest(Path(tmp), base["summary"], base["airtable_safe"])

    def test_reconcile_only_reports_would_write_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, sha = self._reviewed(tmp)
            store = {}
            result = ex.execute_airtable(manifest_path=path, expected_sha256=sha, do_write=False,
                                         api_factory=lambda: _FakeAirtableAPI(store), plan_fn=self._plan)
            self.assertFalse(result["aborted"])
            self.assertEqual(result["would_write"], 1)     # only recSafe
            self.assertEqual(result["external_writes"], 0)
            self.assertEqual(store, {})

    def test_sha_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, _ = self._reviewed(tmp)
            with self.assertRaises(ex.GuardFailure):
                ex.execute_airtable(manifest_path=path, expected_sha256="deadbeef", do_write=False,
                                    plan_fn=self._plan)

    def test_stale_record_aborts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, sha = self._reviewed(tmp)

            def drifted_plan(**kw):
                plan = self._plan(**kw)
                for row in plan["airtable_safe"]:
                    row["proposed_outbound_role"] = "Something Else"
                return plan

            result = ex.execute_airtable(manifest_path=path, expected_sha256=sha, do_write=True,
                                         api_factory=lambda: _FakeAirtableAPI({}), plan_fn=drifted_plan)
            self.assertTrue(result["aborted"])
            self.assertEqual(result["reason"], "reviewed_manifest_stale")
            self.assertEqual(result["external_writes"], 0)

    def test_write_applies_role_only_patch_and_verifies_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, sha = self._reviewed(tmp)
            store = {}
            # Read-back returns original fields merged with the applied patch.
            def fake_readback():
                out = []
                for rec in self.records:
                    merged = dict(rec["fields"])
                    if rec["id"] in store:
                        merged.update(store[rec["id"]])
                    out.append({"id": rec["id"], "fields": merged})
                return out

            original_readback = ex._airtable_records
            ex._airtable_records = fake_readback
            try:
                result = ex.execute_airtable(manifest_path=path, expected_sha256=sha, do_write=True,
                                             api_factory=lambda: _FakeAirtableAPI(store), plan_fn=self._plan)
            finally:
                ex._airtable_records = original_readback

            self.assertFalse(result["aborted"])
            self.assertEqual(result["rows_patched"], 1)
            self.assertEqual(result["readback_verified"], 1)
            self.assertEqual(result["readback_failed"], 0)
            self.assertEqual(result["canonical_field_drift"], 0)
            # recHold never written; recSafe patched with role-only fields.
            self.assertIn("recSafe", store)
            self.assertNotIn("recHold", store)
            self.assertEqual(store["recSafe"]["Outbound Role"], "Software Engineer")
            self.assertTrue(set(store["recSafe"]).issubset(set(ROLE_PATCH_CANDIDATES)))
            self.assertEqual(store["recSafe"]["Validation Version"], config.VALIDATION_VERSION)


class _FakeInstantlyAPI:
    def __init__(self, leads):
        self.leads = leads          # lead_id -> mutable lead dict
        self.patched = []

    def get_lead(self, lead_id):
        return copy.deepcopy(self.leads[lead_id])

    def patch_lead(self, lead_id, patch):
        self.patched.append((lead_id, patch))
        if "custom_variables" in patch:
            self.leads[lead_id]["payload"] = patch["custom_variables"]


class _LaggyInstantlyAPI:
    """Fake whose read-after-write is stale for ``lag`` reads (propagation lag)."""

    def __init__(self, leads, lag=1):
        self.leads = leads
        self.lag = lag
        self.pending = {}
        self.patched = []

    def get_lead(self, lead_id):
        if lead_id in self.pending:
            patch, remaining = self.pending[lead_id]
            if remaining <= 0:
                self.leads[lead_id]["payload"] = patch["custom_variables"]
                del self.pending[lead_id]
            else:
                self.pending[lead_id] = (patch, remaining - 1)
        return copy.deepcopy(self.leads[lead_id])

    def patch_lead(self, lead_id, patch):
        self.patched.append((lead_id, patch))
        self.pending[lead_id] = (patch, self.lag)


class InstantlyExecutionTests(_SignedEnvMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.records = [
            _record("recU", open_role="Software Engineer - Conversational AI",
                    matched="Software Engineer", email="u@x.com", campaign="c1"),
            _record("recC", open_role="Data Scientist - Predictive Maintenance",
                    matched="Data Scientist", email="c@x.com", campaign="c1"),
        ]
        self.campaigns = [{"id": "c1", "name": "Engineering", "status": 2}]
        self.leads = {
            "leadU": {"id": "leadU", "campaign": "c1", "email": "u@x.com", "status": 1,
                      "payload": {"open_role": "OLD", "open_roles": "OLD", "first_name": "Uma"}},
            "leadC": {"id": "leadC", "campaign": "c1", "email": "c@x.com", "status": 1,
                      "payload": {"open_role": "OLD", "open_roles": "OLD"}},
        }
        self.campaign_leads = {"c1": [dict(v, payload=dict(v["payload"])) for v in self.leads.values()]}

    def _plan(self, **kw):
        result = build_manifests(copy.deepcopy(self.records), copy.deepcopy(self.campaigns),
                                 copy.deepcopy(self.campaign_leads),
                                 generated_at=kw["generated_at"], signing_key=kw["signing_key"])
        result["_records_by_id"] = {r["id"]: copy.deepcopy(r) for r in self.records}
        result["_campaigns"] = copy.deepcopy(self.campaigns)
        return result

    def _reviewed(self, tmp):
        base = build_manifests(copy.deepcopy(self.records), copy.deepcopy(self.campaigns),
                               copy.deepcopy(self.campaign_leads),
                               generated_at="2026-08-18T00:00:00+00:00", signing_key=SIGNING_KEY)
        return _write_manifest(Path(tmp), base["summary"], base["instantly_safe_updates"])

    def test_active_campaign_aborts_before_any_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, sha = self._reviewed(tmp)

            def active_plan(**kw):
                plan = self._plan(**kw)
                plan["_campaigns"][0]["status"] = 1     # active
                return plan

            api = _FakeInstantlyAPI(copy.deepcopy(self.leads))
            result = ex.execute_instantly(manifest_path=path, expected_sha256=sha, do_write=True,
                                          api_factory=lambda: api, plan_fn=active_plan)
            self.assertTrue(result["aborted"])
            self.assertEqual(result["reason"], "campaigns_active_pause_first")
            self.assertEqual(api.patched, [])

    def test_write_patches_uncontacted_and_excludes_contacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, sha = self._reviewed(tmp)
            live = copy.deepcopy(self.leads)
            live["leadC"]["timestamp_last_contact"] = "2026-08-18T00:30:00+00:00"  # became contacted
            api = _FakeInstantlyAPI(live)
            result = ex.execute_instantly(manifest_path=path, expected_sha256=sha, do_write=True,
                                          api_factory=lambda: api, plan_fn=self._plan)
            self.assertFalse(result["aborted"])
            self.assertEqual(result["leads_patched"], 1)
            self.assertEqual(result["excluded_became_contacted"], 1)
            # Only the uncontacted lead was patched, role-only.
            self.assertEqual([lid for lid, _ in api.patched], ["leadU"])
            self.assertEqual(live["leadU"]["payload"]["open_role"], "Software Engineer")
            self.assertEqual(live["leadU"]["payload"]["first_name"], "Uma")  # preserved
            # Contacted lead never mutated.
            self.assertEqual(live["leadC"]["payload"]["open_role"], "OLD")

    def test_readback_retries_through_propagation_lag(self):
        import unittest.mock as mock
        with tempfile.TemporaryDirectory() as tmp:
            path, sha = self._reviewed(tmp)
            live = copy.deepcopy(self.leads)
            live["leadC"]["timestamp_last_contact"] = "2026-08-18T00:30:00+00:00"  # excluded
            api = _LaggyInstantlyAPI(live, lag=1)   # first post-patch read is stale
            with mock.patch.object(ex.time, "sleep", lambda *_: None):
                result = ex.execute_instantly(manifest_path=path, expected_sha256=sha, do_write=True,
                                              api_factory=lambda: api, plan_fn=self._plan)
            self.assertFalse(result["aborted"])
            self.assertEqual(result["leads_patched"], 1)
            self.assertEqual(live["leadU"]["payload"]["open_role"], "Software Engineer")
            self.assertEqual(live["leadU"]["payload"]["first_name"], "Uma")


if __name__ == "__main__":
    unittest.main()
