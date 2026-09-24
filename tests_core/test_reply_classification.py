"""The labelled sample. Every case here is a shape seen in the real replies on
2026-09-24, anonymised, plus the traps that matter.

The rule the tests exist to hold: only an opt-out or a departure may stop us using an
address, and nothing a person wrote may ever trigger an automatic send.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from tgtc_core.domain.reply_classification import (HUMAN_REPLY, NO_LONGER_HERE, OPT_OUT, OUT_OF_OFFICE,
                                                   UNKNOWN, classify)

# (subject, body, expected label) -- the sample the classifier is judged on.
SAMPLE = [
    # --- out of office, the shapes production actually receives -------------------
    ("Automatic reply:", "Thank you for contacting me. I am out of the office and have limited access to "
                         "email and telephone.", OUT_OF_OFFICE),
    ("Out of Office:", "I apologize for this auto reply - I am currently away from email until Monday, "
                       "September 28th.", OUT_OF_OFFICE),
    ("Automatic reply: [EXT]", "Hello! I am currently out of the office. If you have an urgent need, please "
                               "reach out to Dana Whitfield and she will assist. Thanks!", OUT_OF_OFFICE),
    ("", "I am on annual leave and will be back in the office on October 6.", OUT_OF_OFFICE),
    ("Respuesta automática", "Estoy fuera de la oficina hasta el 2026-10-05.", OUT_OF_OFFICE),
    ("Automatic reply", "On parental leave with limited access to my email.", OUT_OF_OFFICE),
    # --- no longer here ----------------------------------------------------------
    ("No longer with the organization", "Casey Doyle is no longer with the organization. Her email is being "
                                        "monitored, but please expect a 48 hour delay in response.", NO_LONGER_HERE),
    ("", "Thanks for reaching out - Robin has left the company. Please contact Alex Moreau for anything "
         "related to support hiring.", NO_LONGER_HERE),
    ("Re: hiring", "I am no longer at Northwind as of last month.", NO_LONGER_HERE),
    ("", "This person is no longer an employee.", NO_LONGER_HERE),
    ("Automatic reply", "I have left the firm. For urgent matters email the team inbox.", NO_LONGER_HERE),
    # --- opt out -----------------------------------------------------------------
    ("", "Please remove me from your list.", OPT_OUT),
    ("Re:", "Unsubscribe.", OPT_OUT),
    ("", "Stop emailing me.", OPT_OUT),
    ("", "Do not contact me again about this.", OPT_OUT),
    # --- a human wrote back ------------------------------------------------------
    ("Re: Customer Support hiring", "Thanks for the note - we are still figuring out the timeline, can you "
                                    "send pricing?", HUMAN_REPLY),
    ("Re:", "Who are you and how did you get my email?", HUMAN_REPLY),
    ("Re: intro", "Happy to chat next week.", HUMAN_REPLY),
    # --- undecidable -------------------------------------------------------------
    ("", "", UNKNOWN),
    ("Re:", "ok", UNKNOWN),
    ("", "+1", UNKNOWN),
]


@pytest.mark.parametrize("subject,body,expected", SAMPLE)
def test_the_labelled_sample(subject, body, expected):
    assert classify(subject, body).label == expected


def test_the_sample_covers_every_label():
    assert {label for _, _, label in SAMPLE} == {OUT_OF_OFFICE, NO_LONGER_HERE, OPT_OUT, HUMAN_REPLY, UNKNOWN}


def test_a_departure_dressed_as_an_auto_reply_is_a_departure():
    """Both rules match this text. Which one we act on decides whether we keep emailing
    an address nobody reads."""
    verdict = classify("Automatic reply", "I am out of the office permanently - I no longer work at Acme. "
                                          "Please reach out to Jordan Blake.")
    assert verdict.label == NO_LONGER_HERE
    assert verdict.covering_contact == "Jordan Blake"


def test_only_an_opt_out_or_a_departure_may_stop_an_address():
    assert classify("", "Please remove me from your list.").suppresses is True
    assert classify("", "Robin has left the company.").suppresses is True
    assert classify("Automatic reply", "I am out of the office until Monday.").suppresses is False
    assert classify("Re:", "Can you send pricing?").suppresses is False
    assert classify("", "").suppresses is False


def test_a_human_reply_and_an_unknown_go_to_review_and_never_to_a_send():
    assert classify("Re:", "Happy to chat next week.").needs_review is True
    assert classify("Re:", "ok").needs_review is True
    assert classify("Automatic reply", "Out of the office until Friday.").needs_review is False


def test_a_return_date_is_read_when_the_reply_gives_one():
    received = datetime(2026, 9, 24, 12, 0)
    assert classify("Out of Office", "away from email until Monday, September 28th.",
                    received_at=received).returns_on == date(2026, 9, 28)
    assert classify("", "I am on annual leave and will be back in the office on October 6.",
                    received_at=received).returns_on == date(2026, 10, 6)
    assert classify("Respuesta automática", "Estoy fuera de la oficina hasta el 2026-10-05.",
                    received_at=received).returns_on == date(2026, 10, 5)
    # December -> March means next year, not nine months ago.
    assert classify("Out of office", "back January 5", received_at=datetime(2026, 12, 20)).returns_on == \
        date(2027, 1, 5)
    # No date given is not a guessed date.
    assert classify("Automatic reply", "I am out of the office.").returns_on is None


def test_a_covering_contact_is_only_taken_when_the_reply_directs_us_to_one():
    named = classify("Automatic reply", "I am out of the office. If urgent, please reach out to Dana Whitfield "
                                        "at dana.whitfield@example.com and she will assist.")
    assert named.covering_contact == "Dana Whitfield" and named.covering_email == "dana.whitfield@example.com"

    mentioned = classify("Automatic reply", "I am out of the office visiting our Boston office with Chris Lane.")
    assert mentioned.covering_contact is None, "a name in passing is not a referral"

    plain = classify("Automatic reply", "I am out of the office until Friday.")
    assert plain.covering_contact is None and plain.covering_email is None


def test_the_provider_label_is_evidence_beside_the_verdict_never_the_verdict():
    """Measured 2026-09-24: Instantly marked 5 of 293 real OOO and 78 unrelated replies
    as OOO. Its label is recorded, and it does not decide."""
    verdict = classify("Re:", "Can you send pricing?", provider_label=-1)
    assert verdict.label == HUMAN_REPLY and verdict.provider_label == -1
    verdict = classify("Automatic reply", "I am out of the office until Monday.", provider_label=0)
    assert verdict.label == OUT_OF_OFFICE and verdict.provider_label == 0


def test_the_verdict_serialises_for_the_event_row():
    verdict = classify("Automatic reply", "Out of the office, back October 6. Please reach out to Dana Whitfield.",
                       received_at=datetime(2026, 9, 24), provider_label=0)
    assert verdict.to_dict() == {"label": OUT_OF_OFFICE, "confidence": "text", "matched": ["out_of_office"],
                                 "returns_on": "2026-10-06", "covering_contact": "Dana Whitfield",
                                 "covering_email": None, "provider_label": 0}
