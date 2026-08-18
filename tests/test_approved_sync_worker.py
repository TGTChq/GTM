"""Approved-sync worker: manual-approval gating, idempotency, containment.

The worker (``python -u run_approved.py``) enrolls ONLY Airtable rows an operator
manually set to ``Status = Approved`` into Instantly. Everything the pipeline
writes is ``Status = Pending`` (auto-approval disabled), so Pending / NEEDS_CHECK
/ UNVERIFIED / REJECT / REROUTE can never enroll without an explicit manual
approval. Enrollment is idempotent and per-record contained; a record advances to
``Enrolled`` only after confirmed Instantly success. The worker never runs
acquisition, ATS, JSearch or free-feed collection.

Zero-network: the Airtable and Instantly HTTP seams (``request_with_retry``) are
faked, and provider revalidation is patched, so no live call is made.
"""

from __future__ import annotations

import ast
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

import airtable_client
import config
import instantly_client
import run_approved
from validation_integrity import validation_fingerprint


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.url = "https://api.airtable.test/x"
        self.headers = {}

        class _Req:
            method = "GET"
        self.request = _Req()

    @property
    def text(self):
        return json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeAirtable:
    """A minimal Airtable that honors ``{Status} = 'X'`` and records PATCH writes."""

    def __init__(self, records):
        self.records = records
        self.patches = []  # list of {record_id: fields} written

    def request(self, method, url, *, headers=None, params=None, json_body=None, **kw):
        if method == "GET":
            formula = dict(params or []).get("filterByFormula", "")
            m = re.search(r"\{Status\} = '(.*)'", formula)
            want = m.group(1) if m else None
            recs = [r for r in self.records
                    if str((r.get("fields") or {}).get("Status")) == want]
            return _Resp({"records": recs})
        if method == "PATCH":
            for rec in json_body["records"]:
                self.patches.append({rec["id"]: rec["fields"]})
            return _Resp({"records": json_body["records"]})
        raise AssertionError(f"unexpected Airtable {method}")

    def status_writes(self):
        return [(rid, f.get("Status")) for p in self.patches for rid, f in p.items()]


def _rec(rid, *, status="Approved", final="FINAL_PASS", **overrides):
    fields = {
        "Status": status,
        "Final Decision": final,
        # Current, authorized version -- the hardened worker enrolls ONLY rows
        # whose Validation Version matches the running pipeline exactly.
        "Validation Version": config.VALIDATION_VERSION,
        "Email": "jane@acme.com",
        "Apollo Email Status": "verified",
        "Email Validation": "PASS",
        "Contact Alignment": "PASS",
        "Company": "Acme",
        "Outbound Company": "Acme",
        "Outbound Company Confidence": "high",
        "Outbound Company Identity": "domain:acme.com",
        "Outbound Hold": False,
        "Open Role": "VP Marketing",
        "Outbound Role": "VP Marketing",
        "Outbound Roles": "VP Marketing",
        "Outbound Role Confidence": "medium",
        "Role Focus": "demand gen",
        "Role Bucket": "marketing",
        "Job URL Status": "verified",
        "Campaign ID": "camp-123",
        "Hiring Manager": "Jane Doe",
        "HM Title": "VP Marketing",
        "Website": "https://acme.com",
    }
    fields.update(overrides)
    # Sign the record exactly as the pipeline does, so the fail-closed
    # fingerprint revalidation accepts an unmodified approved row.
    fields["Validation Fingerprint"] = validation_fingerprint(fields)
    return {"id": rid, "fields": fields}


class _Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: getattr(config, k, None) for k in
                       ("AIRTABLE_TOKEN", "AIRTABLE_BASE_ID", "AIRTABLE_TABLE_NAME",
                        "INSTANTLY_API_KEY", "AIRTABLE_RATE_LIMIT_DELAY",
                        "INSTANTLY_RATE_LIMIT_DELAY", "FINAL_PASS_PIPELINE_ENABLED",
                        "APPROVED_SYNC_REVALIDATE_PROVIDERS", "LOG_DIR")}
        config.AIRTABLE_TOKEN = "SECRET-AIRTABLE-TOKEN"
        config.AIRTABLE_BASE_ID = "appTEST"
        config.AIRTABLE_TABLE_NAME = "Leads"
        config.INSTANTLY_API_KEY = "SECRET-INSTANTLY-KEY"
        config.AIRTABLE_RATE_LIMIT_DELAY = 0
        config.INSTANTLY_RATE_LIMIT_DELAY = 0
        config.FINAL_PASS_PIPELINE_ENABLED = True
        config.APPROVED_SYNC_REVALIDATE_PROVIDERS = False  # zero-network for tests
        config.LOG_DIR = tempfile.mkdtemp()

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(config, k, v)


# --------------------------------------------------------------------------
# (1)(2)(3)(4)(5) Manual-approval gating end-to-end
# --------------------------------------------------------------------------
class SelectionGatingTests(_Base):
    def _run_over(self, records):
        fake = FakeAirtable(records)
        instantly = {"created": []}

        def fake_instantly(method, url, *, headers=None, params=None, json_body=None, **kw):
            instantly["created"].append(json_body.get("email"))
            return _Resp({"id": "lead-1"})

        with mock.patch.object(airtable_client, "request_with_retry", fake.request), \
             mock.patch.object(instantly_client, "request_with_retry", fake_instantly):
            result = run_approved.run(revalidate_providers=False)
        return result, instantly["created"], fake

    def test_only_manually_approved_rows_enroll(self):
        records = [
            _rec("pending_fp", status="Pending", final="FINAL_PASS"),
            _rec("pending_nc", status="Pending", final="NEEDS_CHECK"),
            _rec("pending_uv", status="Pending", final="UNVERIFIED"),
            _rec("rejected", status="Rejected", final="REJECT"),
            _rec("enrolled_already", status="Enrolled", final="FINAL_PASS"),
            _rec("approved_ok", status="Approved", final="FINAL_PASS", Email="ok@acme.com"),
        ]
        result, enrolled_emails, _ = self._run_over(records)
        # Exactly one row -- the manually Approved one -- reaches Instantly.
        self.assertEqual(enrolled_emails, ["ok@acme.com"])
        self.assertEqual(result["approved"], 1)
        self.assertEqual(result["enrolled"], 1)

    def test_final_pass_pending_is_not_sufficient(self):
        # A FINAL_PASS row the operator has NOT approved stays Pending -> excluded.
        records = [_rec("fp_pending", status="Pending", final="FINAL_PASS")]
        result, enrolled_emails, _ = self._run_over(records)
        self.assertEqual(enrolled_emails, [])
        self.assertEqual(result["approved"], 0)

    def test_selection_formula_is_exactly_approved_status(self):
        captured = {}

        def fake(method, url, *, headers=None, params=None, json_body=None, **kw):
            captured["formula"] = dict(params or []).get("filterByFormula")
            return _Resp({"records": []})

        with mock.patch.object(airtable_client, "request_with_retry", fake):
            airtable_client.get_approved_leads()
        self.assertEqual(captured["formula"],
                         f"{{Status}} = '{config.AIRTABLE_STATUS_APPROVED}'")

    def test_pipeline_writes_pending_never_approved(self):
        # Auto-approval disabled: every pipeline-written row is Pending.
        fields = airtable_client._job_to_fields({
            "lead_key": "k", "employer_name": "Acme", "hiring_manager_email": "a@acme.com",
            "_final_state": "FINAL_PASS",
        })
        self.assertEqual(fields["Status"], config.AIRTABLE_STATUS_PENDING)
        self.assertNotEqual(fields["Status"], config.AIRTABLE_STATUS_APPROVED)

    def test_builder_rejects_non_actionable_final_decision(self):
        for bad in ("REJECT", "REROUTE"):
            with self.assertRaises(ValueError):
                instantly_client.airtable_record_to_lead(
                    _rec("x", final=bad), probe=False)


# --------------------------------------------------------------------------
# (6)(8)(10) Idempotency, failure handling, status transitions
# --------------------------------------------------------------------------
class EnrollmentSafetyTests(_Base):
    def _dup_response(self):
        r = requests.Response()
        r.status_code = 409
        r._content = b'{"error":"lead already exists"}'
        r.url = "https://api.instantly.test/leads"
        r.request = requests.Request("POST", r.url).prepare()
        return requests.HTTPError("409", response=r)

    def test_existing_instantly_lead_is_duplicate_not_recreated(self):
        with mock.patch.object(instantly_client, "request_with_retry",
                               side_effect=self._dup_response()):
            res = instantly_client.enroll_record(_rec("r1"))
        self.assertTrue(res.success)
        self.assertEqual(res.status, "duplicate")   # skipped safely, counts as success

    def test_failed_enrollment_is_not_marked_successful(self):
        r = requests.Response()
        r.status_code = 500
        r._content = b"server error"
        r.url = "https://api.instantly.test/leads"
        r.request = requests.Request("POST", r.url).prepare()
        with mock.patch.object(instantly_client, "request_with_retry",
                               side_effect=requests.HTTPError("500", response=r)):
            res = instantly_client.enroll_record(_rec("r1"))
        self.assertFalse(res.success)
        self.assertEqual(res.status, "failed")

    def test_status_advances_only_after_confirmed_enrollment(self):
        records = [_rec("good", Email="good@acme.com"),
                   _rec("bad", Email="bad@acme.com")]
        fake = FakeAirtable(records)

        def fake_instantly(method, url, *, headers=None, params=None, json_body=None, **kw):
            if json_body.get("email") == "bad@acme.com":
                r = requests.Response(); r.status_code = 500
                r._content = b"boom"; r.url = url
                r.request = requests.Request("POST", url).prepare()
                raise requests.HTTPError("500", response=r)
            return _Resp({"id": "lead-good"})

        with mock.patch.object(airtable_client, "request_with_retry", fake.request), \
             mock.patch.object(instantly_client, "request_with_retry", fake_instantly):
            result = run_approved.run(revalidate_providers=False)

        writes = dict(fake.status_writes())
        self.assertEqual(writes.get("good"), config.AIRTABLE_STATUS_ENROLLED)   # success -> Enrolled
        self.assertEqual(writes.get("bad"), config.AIRTABLE_STATUS_ERROR)       # failure -> Error, not Enrolled
        self.assertEqual(result["enrolled"], 1)
        self.assertEqual(result["failed"], 1)


# --------------------------------------------------------------------------
# (7)(9)(11) Missing email, batch containment, reconciliation
# --------------------------------------------------------------------------
class ContainmentReconciliationTests(_Base):
    def test_missing_email_is_skipped_fail_closed(self):
        rec = _rec("noemail", Email="")
        ok, reason = run_approved._delivery_precheck(rec)
        self.assertFalse(ok)
        # And the pure preflight classifier buckets it without a network call.
        self.assertEqual(run_approved._classify_for_preflight(rec), "blocked_missing_email")

    def test_one_record_failure_does_not_terminate_batch(self):
        records = [_rec("a", Email="a@acme.com"),
                   _rec("b", Email="b@acme.com"),
                   _rec("c", Email="c@acme.com")]
        fake = FakeAirtable(records)

        def fake_instantly(method, url, *, headers=None, params=None, json_body=None, **kw):
            if json_body.get("email") == "b@acme.com":
                raise RuntimeError("transient instantly error")
            return _Resp({"id": "lead"})

        with mock.patch.object(airtable_client, "request_with_retry", fake.request), \
             mock.patch.object(instantly_client, "request_with_retry", fake_instantly):
            result = run_approved.run(revalidate_providers=False)

        self.assertEqual(result["enrolled"], 2)   # a and c still enrolled
        self.assertEqual(result["failed"], 1)     # b failed, did not abort

    def test_reconciliation_holds(self):
        records = [_rec("a", Email="a@acme.com"), _rec("b", Email="b@acme.com")]
        fake = FakeAirtable(records)
        with mock.patch.object(airtable_client, "request_with_retry", fake.request), \
             mock.patch.object(instantly_client, "request_with_retry",
                               lambda *a, **k: _Resp({"id": "lead"})):
            result = run_approved.run(revalidate_providers=False)
        approved = result["approved"]
        accounted = result["enrolled"] + result["duplicates"] + result["failed"]
        self.assertEqual(accounted, approved)


# --------------------------------------------------------------------------
# (12) Zero-write preflight
# --------------------------------------------------------------------------
class PreflightTests(_Base):
    def test_preflight_makes_zero_writes_and_no_instantly_call(self):
        records = [
            _rec("ok", Email="ok@acme.com"),
            _rec("noemail", Email=""),
            _rec("badcampaign", Campaign_ID="", Role_Bucket="unknownbucket"),
        ]
        # normalize the two odd keys into real field names
        records[2]["fields"].pop("Campaign_ID", None)
        records[2]["fields"].pop("Role_Bucket", None)
        records[2]["fields"]["Campaign ID"] = ""
        records[2]["fields"]["Role Bucket"] = "unknownbucket"

        fake = FakeAirtable(records + [_rec("enr", status="Enrolled")])

        def no_instantly(*a, **k):
            raise AssertionError("preflight must not call Instantly")

        saved_campaign = config.INSTANTLY_CAMPAIGN_ID
        config.INSTANTLY_CAMPAIGN_ID = ""  # so badcampaign has no fallback
        try:
            with mock.patch.object(airtable_client, "request_with_retry", fake.request), \
                 mock.patch.object(instantly_client, "request_with_retry", no_instantly):
                summary = run_approved.preflight()
        finally:
            config.INSTANTLY_CAMPAIGN_ID = saved_campaign

        self.assertEqual(fake.patches, [])                 # ZERO writes
        self.assertFalse(summary["instantly_called"])
        self.assertEqual(summary["counts"]["approved_total"], 3)
        self.assertEqual(summary["counts"]["already_enrolled"], 1)
        self.assertEqual(summary["counts"]["eligible"], 1)
        self.assertEqual(summary["counts"]["blocked_missing_email"], 1)
        self.assertEqual(summary["counts"]["blocked_no_campaign"], 1)


# --------------------------------------------------------------------------
# (13)(14)(15)(16) Worker isolation, secret hygiene, preserved guarantees
# --------------------------------------------------------------------------
class WorkerIsolationTests(_Base):
    _FORBIDDEN = {"multi_source_acquisition", "jsearch_scraper", "free_job_sources",
                  "ats_board_registry", "ats_public_adapters", "run_orchestrator"}

    def test_worker_never_imports_acquisition_modules(self):
        source = Path("run_approved.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported & self._FORBIDDEN, set())

    def test_full_run_touches_no_acquisition_seam(self):
        records = [_rec("a", Email="a@acme.com")]
        fake = FakeAirtable(records)
        with mock.patch.object(airtable_client, "request_with_retry", fake.request), \
             mock.patch.object(instantly_client, "request_with_retry",
                               lambda *a, **k: _Resp({"id": "lead"})), \
             mock.patch("free_job_sources.build_adapters",
                        side_effect=AssertionError("acquisition!")), \
             mock.patch("jsearch_scraper.scrape_jobs",
                        create=True, side_effect=AssertionError("jsearch!")):
            result = run_approved.run(revalidate_providers=False)
        self.assertEqual(result["enrolled"], 1)

    def test_secrets_never_appear_in_logs(self):
        records = [_rec("a", Email="a@acme.com")]
        fake = FakeAirtable(records)
        with self.assertLogs("run_approved", level="INFO") as cm, \
             mock.patch.object(airtable_client, "request_with_retry", fake.request), \
             mock.patch.object(instantly_client, "request_with_retry",
                               lambda *a, **k: _Resp({"id": "lead"})):
            run_approved.run(revalidate_providers=False)
        blob = "\n".join(cm.output)
        self.assertNotIn(config.INSTANTLY_API_KEY, blob)
        self.assertNotIn(config.AIRTABLE_TOKEN, blob)

    def test_railway_json_does_not_control_start_command(self):
        # The GTM Start Command is deliberately service-managed (editable from the
        # Railway UI), NOT config-as-code: railway.json must not define it, so an
        # operator can set/restore it (or a maintenance 'sleep infinity') without a
        # Git change. The image CMD is the safe fallback if no Start Command is set.
        rc = json.loads(Path("railway.json").read_text(encoding="utf-8"))
        self.assertNotIn("startCommand", rc.get("deploy", {}))
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
        self.assertIn("--preflight-only", dockerfile)  # safe, zero-network fallback

    def test_package_integrity_passes(self):
        import run_orchestrator as R
        if not Path("orchestrator.MANIFEST.sha256").is_file():
            self.skipTest("manifest not present in CWD")
        res, _ = R._preflight_checks(
            R.build_parser().parse_args(["--artifact-root", tempfile.mkdtemp()]))
        self.assertTrue(res["integrity_ok"])


if __name__ == "__main__":
    unittest.main()
