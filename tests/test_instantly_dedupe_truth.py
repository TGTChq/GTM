"""A 2xx from Instantly is not proof of delivery -- classify it honestly.

Instantly v2 ``POST /leads`` returns 200 "The Lead" whether it created a new lead or
the email already existed in the workspace. Its OpenAPI contract defines no 409/422
for this operation and no created-vs-existing flag, so the old duplicate detection
(409/422 + "already"/"duplicate"/"exists") could never fire -- every pre-existing
lead was silently counted as net-new.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import airtable_client
import config
import instantly_client as IC
import validation_integrity as vi

TARGET = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


def _now():
    return datetime.now(timezone.utc)


class ClassifyMembershipTests(unittest.TestCase):
    def test_lead_created_by_this_request_is_net_new(self):
        started = _now()
        data = {"id": "lead-1", "campaign": TARGET,
                "timestamp_created": started.isoformat().replace("+00:00", "Z")}
        m, lead_id, camp, _ = IC.classify_membership(
            data, target_campaign=TARGET, request_started_at=started)
        self.assertEqual(m, IC.NEWLY_CREATED)
        self.assertEqual((lead_id, camp), ("lead-1", TARGET))

    def test_preexisting_lead_in_another_campaign_is_not_net_new(self):
        started = _now()
        old = (started - timedelta(days=78)).isoformat().replace("+00:00", "Z")
        m, *_ = IC.classify_membership(
            {"id": "lead-2", "campaign": OTHER, "timestamp_created": old},
            target_campaign=TARGET, request_started_at=started)
        self.assertEqual(m, IC.EXISTING_OTHER_CAMPAIGN)

    def test_preexisting_lead_already_in_target_campaign(self):
        started = _now()
        old = (started - timedelta(days=3)).isoformat().replace("+00:00", "Z")
        m, *_ = IC.classify_membership(
            {"id": "lead-3", "campaign": TARGET, "timestamp_created": old},
            target_campaign=TARGET, request_started_at=started)
        self.assertEqual(m, IC.ALREADY_IN_TARGET_CAMPAIGN)

    def test_preexisting_lead_with_no_campaign_is_workspace_level(self):
        started = _now()
        old = (started - timedelta(days=3)).isoformat().replace("+00:00", "Z")
        m, *_ = IC.classify_membership(
            {"id": "lead-4", "campaign": None, "timestamp_created": old},
            target_campaign=TARGET, request_started_at=started)
        self.assertEqual(m, IC.ALREADY_EXISTS_WORKSPACE)

    def test_missing_timestamp_is_unknown_never_net_new(self):
        m, *_ = IC.classify_membership(
            {"id": "lead-5", "campaign": TARGET}, target_campaign=TARGET,
            request_started_at=_now())
        self.assertEqual(m, IC.MEMBERSHIP_UNKNOWN)

    def test_unparseable_timestamp_is_unknown_never_net_new(self):
        m, *_ = IC.classify_membership(
            {"id": "x", "campaign": TARGET, "timestamp_created": "not-a-date"},
            target_campaign=TARGET, request_started_at=_now())
        self.assertEqual(m, IC.MEMBERSHIP_UNKNOWN)

    def test_non_dict_response_is_unknown(self):
        self.assertEqual(
            IC.classify_membership(None, target_campaign=TARGET,
                                   request_started_at=_now())[0],
            IC.MEMBERSHIP_UNKNOWN)

    def test_naive_timestamp_is_treated_as_utc(self):
        started = _now()
        naive = started.replace(tzinfo=None).isoformat()
        m, *_ = IC.classify_membership(
            {"id": "x", "campaign": TARGET, "timestamp_created": naive},
            target_campaign=TARGET, request_started_at=started)
        self.assertEqual(m, IC.NEWLY_CREATED)

    def test_small_clock_skew_still_counts_as_created(self):
        started = _now()
        slightly_before = (started - timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
        m, *_ = IC.classify_membership(
            {"id": "x", "campaign": TARGET, "timestamp_created": slightly_before},
            target_campaign=TARGET, request_started_at=started)
        self.assertEqual(m, IC.NEWLY_CREATED)

    def test_only_newly_created_is_net_new(self):
        for name in IC.MEMBERSHIP_CLASSES:
            with self.subTest(membership=name):
                r = IC.EnrollmentResult(True, "enrolled", "rec", "e@x.com", TARGET,
                                        membership=name)
                self.assertEqual(r.net_new, name == IC.NEWLY_CREATED)


class MembershipVerificationTests(unittest.TestCase):
    """/campaigns/search-by-contact is the only authoritative membership answer."""

    def _search(self, campaign_ids=None, raise_exc=None, payload=None):
        def fake(method, url, headers=None, params=None, **kw):
            if raise_exc:
                raise raise_exc
            return mock.Mock()
        data = payload if payload is not None else {
            "items": [{"id": c} for c in (campaign_ids or [])]}
        return (mock.patch.object(IC, "request_with_retry", side_effect=fake),
                mock.patch.object(IC, "safe_json", return_value=data))

    def _run(self, **kw):
        p1, p2 = self._search(**kw)
        with p1, p2:
            return IC.verify_membership_via_search("hm@acme.com", TARGET)

    def test_target_present_is_already_in_target_campaign(self):
        m, camps = self._run(campaign_ids=[OTHER, TARGET])
        self.assertEqual(m, IC.ALREADY_IN_TARGET_CAMPAIGN)
        self.assertIn(TARGET, camps)

    def test_only_other_campaigns_is_existing_other_campaign(self):
        m, camps = self._run(campaign_ids=[OTHER])
        self.assertEqual(m, IC.EXISTING_OTHER_CAMPAIGN)
        self.assertEqual(camps, (OTHER,))

    def test_no_campaigns_is_workspace_only(self):
        self.assertEqual(self._run(campaign_ids=[])[0], IC.ALREADY_EXISTS_WORKSPACE)

    def test_lookup_error_fails_closed_as_unknown(self):
        self.assertEqual(self._run(raise_exc=RuntimeError("boom"))[0], IC.MEMBERSHIP_UNKNOWN)

    def test_malformed_payload_fails_closed_as_unknown(self):
        self.assertEqual(self._run(payload={"unexpected": True})[0], IC.MEMBERSHIP_UNKNOWN)

    def test_blank_email_is_unknown_without_calling_the_api(self):
        with mock.patch.object(IC, "request_with_retry") as req:
            self.assertEqual(IC.verify_membership_via_search("", TARGET)[0],
                             IC.MEMBERSHIP_UNKNOWN)
            req.assert_not_called()


class EnrollRecordStateTests(unittest.TestCase):
    """Airtable Enrolled must follow DELIVERY, never a bare 2xx."""

    def _enroll(self, lead_payload, search_campaigns=None, search_error=None):
        calls = {"posts": 0, "searches": 0, "body": None}

        def fake_request(method, url, headers=None, params=None, json_body=None, **kw):
            if url.endswith("/leads"):
                calls["posts"] += 1
                calls["body"] = json_body
            else:
                calls["searches"] += 1
                if search_error:
                    raise search_error
            return mock.Mock()

        def fake_json(resp):
            if calls["searches"] and calls["body"] is not None and resp is not None:
                pass
            return fake_json.queue.pop(0)

        fake_json.queue = []
        with mock.patch.object(IC, "airtable_record_to_lead",
                               return_value={"email": "hm@acme.com", "campaign": TARGET}), \
             mock.patch.object(IC, "validate_preflight"), \
             mock.patch.object(IC, "enrollment_block_reason", return_value=""), \
             mock.patch.object(IC, "request_with_retry", side_effect=fake_request), \
             mock.patch.object(IC, "safe_json", side_effect=[
                 lead_payload,
                 {"items": [{"id": c} for c in (search_campaigns or [])]},
             ]):
            result = IC.enroll_record(
                {"id": "rec1", "fields": {"Email": "hm@acme.com"}})
        return result, calls

    def test_new_lead_is_delivered_and_net_new(self):
        now = _now().isoformat().replace("+00:00", "Z")
        r, calls = self._enroll({"id": "L", "campaign": TARGET, "timestamp_created": now})
        self.assertEqual(r.membership, IC.NEWLY_CREATED)
        self.assertTrue(r.delivered)
        self.assertTrue(r.net_new)
        self.assertEqual(r.status, "enrolled")
        self.assertEqual(calls["searches"], 0)   # no N+1 on the normal path

    def test_existing_in_target_campaign_is_delivered_but_not_net_new(self):
        old = (_now() - timedelta(days=9)).isoformat().replace("+00:00", "Z")
        r, calls = self._enroll({"id": "L", "campaign": TARGET, "timestamp_created": old},
                                search_campaigns=[TARGET])
        self.assertEqual(r.membership, IC.ALREADY_IN_TARGET_CAMPAIGN)
        self.assertTrue(r.delivered)
        self.assertFalse(r.net_new)
        self.assertEqual(r.status, "duplicate")
        self.assertEqual(calls["searches"], 1)   # exceptional path consulted

    def test_existing_in_other_campaign_is_never_marked_enrolled(self):
        """The exact historical defect: 2xx for a lead in someone else's campaign."""
        old = (_now() - timedelta(days=78)).isoformat().replace("+00:00", "Z")
        r, _ = self._enroll({"id": "L", "campaign": OTHER, "timestamp_created": old},
                            search_campaigns=[OTHER])
        self.assertEqual(r.membership, IC.EXISTING_OTHER_CAMPAIGN)
        self.assertFalse(r.delivered)
        self.assertFalse(r.success)
        self.assertFalse(r.net_new)
        self.assertEqual(r.status, "not_delivered")
        self.assertTrue(r.api_accepted)          # accepted, but NOT delivered
        self.assertIn("Not delivered", r.error)

    def test_workspace_only_lead_is_never_marked_enrolled(self):
        old = (_now() - timedelta(days=9)).isoformat().replace("+00:00", "Z")
        r, _ = self._enroll({"id": "L", "campaign": None, "timestamp_created": old},
                            search_campaigns=[])
        self.assertEqual(r.membership, IC.ALREADY_EXISTS_WORKSPACE)
        self.assertFalse(r.delivered)

    def test_membership_verification_error_fails_closed(self):
        old = (_now() - timedelta(days=9)).isoformat().replace("+00:00", "Z")
        r, _ = self._enroll({"id": "L", "campaign": OTHER, "timestamp_created": old},
                            search_error=RuntimeError("network"))
        self.assertEqual(r.membership, IC.MEMBERSHIP_UNKNOWN)
        self.assertFalse(r.delivered)
        self.assertFalse(r.success)

    def test_missing_timestamp_is_verified_and_never_auto_net_new(self):
        r, calls = self._enroll({"id": "L", "campaign": TARGET}, search_campaigns=[TARGET])
        self.assertFalse(r.net_new)
        self.assertEqual(calls["searches"], 1)

    def test_skip_if_in_campaign_is_always_sent(self):
        now = _now().isoformat().replace("+00:00", "Z")
        _r, calls = self._enroll({"id": "L", "campaign": TARGET, "timestamp_created": now})
        self.assertIs(calls["body"]["skip_if_in_campaign"], True)

    def test_skip_if_in_workspace_follows_the_canonical_policy_flag(self):
        now = _now().isoformat().replace("+00:00", "Z")
        for policy in (False, True):
            with self.subTest(policy=policy), \
                 mock.patch.object(config, "ENROLLMENT_PERSON_EMPLOYER_UNIQUENESS", policy):
                _r, calls = self._enroll(
                    {"id": "L", "campaign": TARGET, "timestamp_created": now})
                self.assertIs(calls["body"]["skip_if_in_workspace"], policy)


class EnrollAggregationTests(unittest.TestCase):
    """The regression that motivated this: 201 accepted, only 199 truly delivered."""

    def _record(self, rid):
        return {"id": rid, "fields": {}}

    def _run(self, responses):
        made = iter(responses)

        def fake_enroll(record):
            data = next(made)
            m, lid, camp, created = IC.classify_membership(
                data, target_campaign=TARGET, request_started_at=_now())
            delivered = m in IC.DELIVERED_MEMBERSHIPS
            status = ("enrolled" if m == IC.NEWLY_CREATED
                      else "duplicate" if m == IC.ALREADY_IN_TARGET_CAMPAIGN
                      else "not_delivered")
            return IC.EnrollmentResult(delivered, status, record["id"], "e@x.com", TARGET,
                                       membership=m, lead_id=lid, lead_campaign=camp,
                                       created_at=created, api_accepted=True)

        with mock.patch.object(IC, "enroll_record", side_effect=fake_enroll), \
             mock.patch.object(config, "INSTANTLY_RATE_LIMIT_DELAY", 0):
            return IC.enroll_approved_leads([self._record(f"r{i}") for i in range(len(responses))])

    def test_accepted_count_and_net_new_diverge(self):
        now = _now().isoformat().replace("+00:00", "Z")
        old = (_now() - timedelta(days=78)).isoformat().replace("+00:00", "Z")
        out = self._run([
            {"id": "a", "campaign": TARGET, "timestamp_created": now},
            {"id": "b", "campaign": TARGET, "timestamp_created": now},
            {"id": "c", "campaign": OTHER, "timestamp_created": old},
        ])
        self.assertEqual(out["api_accepted"], 3)        # all accepted by the API
        self.assertEqual(out["enrolled"], 2)            # only two truly created
        self.assertEqual(out["net_new_delivered"], 2)
        self.assertEqual(out["not_delivered"], 1)
        self.assertEqual(out["pre_existing_in_instantly"], 1)
        self.assertEqual(out["membership"][IC.EXISTING_OTHER_CAMPAIGN], 1)
        self.assertEqual(out["membership"][IC.NEWLY_CREATED], 2)
        self.assertEqual(len(out["pre_existing_detail"]), 1)
        # THE regression: the undelivered row must never reach the Enrolled write.
        self.assertEqual(len(out["enrolled_record_ids"]), 2)
        self.assertNotIn("r2", out["enrolled_record_ids"])
        self.assertEqual(out["failed"], 1)

    def test_membership_histogram_lists_every_class(self):
        now = _now().isoformat().replace("+00:00", "Z")
        out = self._run([{"id": "a", "campaign": TARGET, "timestamp_created": now}])
        self.assertEqual(set(out["membership"]), set(IC.MEMBERSHIP_CLASSES))

    def test_unattempted_rows_do_not_pollute_the_histogram(self):
        def fake_enroll(record):
            return IC.EnrollmentResult(False, "failed", record["id"], "", "", "boom",
                                       membership="")
        with mock.patch.object(IC, "enroll_record", side_effect=fake_enroll), \
             mock.patch.object(config, "INSTANTLY_RATE_LIMIT_DELAY", 0):
            out = IC.enroll_approved_leads([{"id": "r1", "fields": {}}])
        self.assertEqual(out["net_new_delivered"], 0)
        self.assertEqual(sum(out["membership"].values()), 0)
        self.assertEqual(out["failed"], 1)


class EmailVerificationGateParityTests(unittest.TestCase):
    """Email-verification semantics must be identical across all three gates."""

    def setUp(self):
        p = mock.patch.object(config, "VALIDATION_SIGNING_KEY", "unit-test-key")
        p.start()
        self.addCleanup(p.stop)

    def _row(self, **over):
        fields = {
            "Final Decision": "FINAL_PASS", "Validation Version": str(config.VALIDATION_VERSION),
            "Email": "hm@acme.com", "Apollo Email Status": "verified",
            "Email Validation": "PASS", "Contact Alignment": "PASS",
            "Outbound Company": "Acme", "Outbound Company Confidence": "high",
            "Outbound Role": "Account Executive", "Outbound Role Confidence": "high",
            "Role Focus": "pipeline development", "Role Bucket": "gtm_revenue",
            "Campaign ID": TARGET, "Outbound Hold": False,
        }
        fields.update(over)
        fields["Validation Fingerprint"] = vi.validation_fingerprint(fields)
        return fields

    def test_extrapolated_email_is_blocked_by_send_safety(self):
        ok, reason = airtable_client.send_safe_facts(
            self._row(**{"Apollo Email Status": "extrapolated", "Email Validation": "NEEDS_CHECK"}))
        self.assertFalse(ok)
        self.assertEqual(reason, "apollo_email_not_verified")

    def test_needs_check_validation_is_blocked_even_if_apollo_says_verified(self):
        ok, reason = airtable_client.send_safe_facts(
            self._row(**{"Email Validation": "NEEDS_CHECK"}))
        self.assertFalse(ok)
        self.assertEqual(reason, "email_gate_not_pass")

    def test_eligibility_delegates_to_send_safety_so_they_cannot_diverge(self):
        for over in ({"Apollo Email Status": "extrapolated", "Email Validation": "NEEDS_CHECK"},
                     {"Email Validation": "NEEDS_CHECK"},
                     {"Apollo Email Status": "unavailable", "Email Validation": "NEEDS_CHECK"},
                     {}):
            with self.subTest(over=over):
                fields = self._row(**over)
                ok, reason = airtable_client.send_safe_facts(fields)
                category, elig_reason = airtable_client.approved_row_eligibility(fields)
                self.assertEqual(elig_reason, "eligible" if ok else reason)

    def test_delivery_is_never_stricter_than_send_safety_on_email(self):
        """A send-safe row must never die downstream on email semantics."""
        fields = self._row()
        self.assertTrue(airtable_client.send_safe_facts(fields)[0])
        lead = IC.airtable_record_to_lead({"id": "rec1", "fields": fields}, probe=False)
        self.assertEqual(lead["email"], fields["Email"])


if __name__ == "__main__":
    unittest.main()
