"""The SEND-SAFE backfill write path must use the established request helper
contract (request_with_retry(..., json_body=...)) and touch ONLY Status. This
pins the exact PATCH construction so the `json`/`json_body` kwarg regression
cannot recur."""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import config
import http_utils
import run_fantastic_sendsafe_reeval as backfill


def test_request_with_retry_takes_json_body_not_json():
    params = inspect.signature(http_utils.request_with_retry).parameters
    assert "json_body" in params
    assert "json" not in params


def test_backfill_builds_batched_status_only_patch():
    calls = []

    def fake_rwr(method, url, *, headers=None, json_body=None, params=None, **kw):
        calls.append((method, url, json_body))
        return MagicMock()

    ids = [f"rec{i}" for i in range(23)]  # -> batches of 10, 10, 3
    with patch.object(backfill, "request_with_retry", fake_rwr), \
         patch.object(backfill, "safe_json", lambda r: {}):
        n = backfill.backfill_flip_to_approved(ids)

    assert n == 23
    assert len(calls) == 3                                   # 10 + 10 + 3
    assert [len(b["records"]) for _, _, b in calls] == [10, 10, 3]
    seen_ids = []
    for method, _url, body in calls:
        assert method == "PATCH"
        assert body is not None                              # json_body was passed
        for rec in body["records"]:
            # ONLY Status is written; disposition/evidence/canonical untouched.
            assert rec["fields"] == {"Status": config.AIRTABLE_STATUS_APPROVED}
            assert rec["id"]
            seen_ids.append(rec["id"])
    assert seen_ids == ids                                   # every id, no dupes/loss


def test_empty_backfill_writes_nothing():
    with patch.object(backfill, "request_with_retry",
                      MagicMock(side_effect=AssertionError("must not call"))):
        assert backfill.backfill_flip_to_approved([]) == 0


if __name__ == "__main__":
    import unittest
    unittest.main()
