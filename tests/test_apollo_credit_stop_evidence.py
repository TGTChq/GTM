"""A credit stop has to be readable from the log, and must not invent a remedy.

On 2026-09-06 the production run stopped with exactly one usable line:

    Apollo organization enrichment stopped for jerry.ai/Jerry: credit_exhausted (HTTP 422).

That is our category and our status. Apollo's own error code and message went only
into an artifact under LOG_DIR on the run volume -- which a cron service exposes
only while the container is alive -- so the claim could not be checked against the
account from the log at all. Both fields were already sanitized by the classifier;
withholding them bought nothing.

The second problem was the remedy. The exception read "Ask an Apollo admin to add
credits", which is a fix we invented. Apollo's body reports the team's allotment is
used up FOR THIS BILLING CYCLE and links a plan upgrade; on an account with
pay-as-you-go or additional usage the same code can equally mean a spend cap was
reached, that overage does not cover this credit type, or that a payment failed.
Those want different actions and none of them is visible over the API, so the
exception now reports what Apollo said and stops.

The body below is the real one, captured read-only from production's key on
2026-09-06T07:26Z (request id a972a3be-9a9a-44fb-808a-8616b386a1f0).
"""

from __future__ import annotations

import json
import logging
import unittest
import unittest.mock

import requests

import apollo_client
from apollo_errors import ApolloErrorCategory, classify_apollo_error

#: Verbatim, as Apollo returned it.
REAL_CREDIT_BODY = json.dumps({
    "error": ("You have insufficient credits! <a href='https://app.apollo.io/#/"
              "settings/plans/upgrade?source=api_credit_limit'>Upgrade your plan"
              "</a> to increase your number of lead credits."),
    "error_details": {
        "code": "BILLING.LIMIT.CREDITS_EXHAUSTED",
        "message": "Your team has used all of its credits for this billing cycle.",
        "suggestions": [{"label": "Upgrade your plan to get more API credits",
                         "url": "https://app.apollo.io/#/settings/plans/upgrade"}],
        "context": {
            "credit_type": {"value": "lead credits",
                            "description": "The type of credit that was checked"},
            "credit_balance": {"value": 0,
                               "description": "Credits currently available"},
            "next_billing_date": {"value": "2026-09-18",
                                  "description": "When the next cycle begins"},
        },
    },
})


class ApollosOwnDiagnosisSurvives(unittest.TestCase):
    """Apollo answers the account questions in the error body. We were dropping it.

    ``context`` says which credit pool ran out, what Apollo reads the balance as,
    and when the cycle turns over. That last one is a remedy in its own right --
    the account recovers on its own -- and a message that only said "add credits"
    concealed it.
    """

    def test_the_context_block_is_extracted(self):
        classification = classify_apollo_error(_http_error(422, REAL_CREDIT_BODY))
        self.assertEqual(classification.context, {
            "credit_type": "lead credits",
            "credit_balance": 0,
            "next_billing_date": "2026-09-18",
        })

    def test_it_reaches_the_persisted_evidence_record(self):
        from apollo_errors import build_error_record

        record = build_error_record(
            classify_apollo_error(_http_error(422, REAL_CREDIT_BODY)),
            domain="jerry.ai")
        self.assertEqual(record["apollo_context"]["credit_balance"], 0)
        self.assertEqual(record["apollo_context"]["next_billing_date"], "2026-09-18")
        self.assertEqual(record["apollo_error_code"], "BILLING.LIMIT.CREDITS_EXHAUSTED")

    def test_a_non_scalar_context_value_is_not_copied_through(self):
        """Only scalars. A nested shape is not evidence we know how to read."""
        body = json.dumps({"error": "insufficient credits",
                           "error_details": {"code": "X", "context": {
                               "credit_balance": {"value": 0},
                               "weird": {"value": {"nested": [1, 2]}}}}})
        classification = classify_apollo_error(_http_error(422, body))
        self.assertEqual(classification.context, {"credit_balance": 0})


def _http_error(status: int, body: str, url: str = "https://api.apollo.io/api/v1/organizations/enrich"):
    response = requests.Response()
    response.status_code = status
    response._content = body.encode("utf-8")
    response.url = url
    exc = requests.HTTPError(response=response)
    exc.response = response
    return exc


class TheRealBodyStillClassifiesAsCredits(unittest.TestCase):
    def test_apollos_own_words_are_what_makes_it_a_credit_stop(self):
        classification = classify_apollo_error(_http_error(422, REAL_CREDIT_BODY))
        self.assertIs(classification.category, ApolloErrorCategory.CREDIT_EXHAUSTED)
        self.assertTrue(classification.global_fatal)
        self.assertEqual(classification.error_code, "BILLING.LIMIT.CREDITS_EXHAUSTED")

    def test_a_422_without_credit_words_is_still_only_a_record_problem(self):
        """The guard that keeps one bad company from aborting a run."""
        classification = classify_apollo_error(
            _http_error(422, json.dumps({"error": "domain is invalid"})))
        self.assertIs(classification.category, ApolloErrorCategory.VALIDATION)
        self.assertFalse(classification.global_fatal)


class TheStopIsReadableAndPrescribesNothing(unittest.TestCase):
    def _raise(self):
        exc = _http_error(422, REAL_CREDIT_BODY)
        classification = classify_apollo_error(exc)
        with self.assertRaises(apollo_client.ApolloCreditsExhaustedError) as caught:
            apollo_client._raise_global_fatal(classification, exc)
        return str(caught.exception)

    def test_it_carries_apollos_code_and_message(self):
        text = self._raise()
        self.assertIn("BILLING.LIMIT.CREDITS_EXHAUSTED", text)
        self.assertIn("billing cycle", text)
        self.assertIn("422", text)

    def test_it_does_not_tell_anyone_to_buy_credits(self):
        """The whole point: our label was right and our remedy was a guess."""
        text = self._raise().lower()
        self.assertNotIn("add credits", text)
        self.assertNotIn("ask an apollo admin", text)
        # It should instead name the possibilities it cannot distinguish.
        self.assertIn("billing settings", text)

    def test_the_log_line_carries_the_evidence_too(self):
        """Without this the only copy is an artifact on an unreachable volume."""
        exc = _http_error(422, REAL_CREDIT_BODY)

        def _boom(*_a, **_k):
            raise exc

        with unittest.mock.patch.object(
                apollo_client, "_organization_enrichment_request", _boom), \
                unittest.mock.patch.object(apollo_client, "_record_apollo_error"), \
                self.assertLogs(apollo_client.logger, level=logging.ERROR) as logged:
            with self.assertRaises(apollo_client.ApolloCreditsExhaustedError):
                apollo_client.enrich_organization(domain="jerry.ai", name="Jerry")

        line = "\n".join(logged.output)
        self.assertIn("BILLING.LIMIT.CREDITS_EXHAUSTED", line)
        self.assertIn("credit_exhausted", line)
        self.assertIn("422", line)


if __name__ == "__main__":
    import unittest.mock  # noqa: F401
    unittest.main()
