"""What a reply actually says, decided from its text.

Instantly's own labels were measured on 2026-09-24 and are not a source of truth: of 293
replies whose text says "out of office" it had marked 5, and of 75 that say the person
has left it had marked 18, while labelling 78 unrelated replies as out of office. So the
text decides, and Instantly's label is kept beside the verdict as evidence, never as the
verdict.

Five labels, because they need five different reactions:

* ``out_of_office`` -- the person is away and coming back. Nothing is suppressed and
  nobody else is contacted; the follow-up waits, and waits until the date the reply
  gives when it gives one.
* ``no_longer_here`` -- the person has left the employer. That address stops being used
  and the company x campaign unit needs a current decision maker.
* ``opt_out`` -- do not contact again. Permanent, and it must hold before future
  acquisition as well as before the next send.
* ``human_reply`` -- a person wrote back. It belongs to a human, never to an automatic
  send.
* ``unknown`` -- the text does not decide. It goes to review; it never triggers anything.

``covering_contact`` is only filled when the reply NAMES someone to contact for this
matter ("please reach out to X"). A reply that merely mentions a name is not a referral,
and a claim that the original contact referred us is a claim about a person, so it is
made only when they actually did.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

OUT_OF_OFFICE = "out_of_office"
NO_LONGER_HERE = "no_longer_here"
OPT_OUT = "opt_out"
HUMAN_REPLY = "human_reply"
UNKNOWN = "unknown"

#: The longest leave a reply is believed to state. Beyond it, the date is not a date.
MAX_LEAVE_DAYS = 120

#: Checked first: a departure that also reads like an auto-reply is a departure.
_GONE = re.compile(
    r"no longer (?:with|at|works?|working|employed|here|part of)"
    r"|is no longer (?:with|at|the)"
    r"|(?:has|have) left (?:the )?(?:company|business|firm|organi[sz]ation|us)?"
    r"|left the (?:company|business|firm|organi[sz]ation)"
    r"|no longer (?:an? )?(?:employee|member)"
    r"|has departed|is not with (?:us|the company)"
    r"|ya no (?:trabaja|forma parte|est[aá])", re.I)

_OPT_OUT = re.compile(
    r"\bunsubscribe\b|remove me from|take me off|stop (?:emailing|contacting|sending)"
    r"|do not (?:contact|email) (?:me|us)|don'?t (?:contact|email) (?:me|us)"
    r"|not interested(?:,| and)? (?:please )?(?:remove|stop)"
    r"|opt[- ]out|d[ée]sabonner|dar(?:me)? de baja|no (?:me )?vuelvan? a", re.I)

_OOO = re.compile(
    r"out of (?:the )?office|\booo\b|automatic(?:ally)? repl(?:y|ies)|auto[- ]?reply"
    r"|annual leave|on leave|on vacation|on holiday|away from (?:my |the )?(?:desk|office|email)"
    r"|currently away|limited access to (?:my )?(?:e-?mail|email)"
    r"|maternity leave|paternity leave|parental leave|sabbatical"
    r"|fuera de la oficina|de vacaciones|ausente", re.I)

#: A named person to contact FOR THIS MATTER. The direction has to be explicit.
_COVERING = re.compile(
    r"(?:please\s+)?(?:reach out|reply|write|contact|email|speak|talk|direct(?:\s+\w+)?|forward(?:\s+\w+)?)"
    r"(?:\s+\w+){0,3}?\s+to\s+(?P<name>[A-Z][\w'’-]+(?:\s+[A-Z][\w'’-]+){0,2})"
    r"|(?P<name2>[A-Z][\w'’-]+(?:\s+[A-Z][\w'’-]+){0,2})\s+(?:is|will be)\s+(?:now\s+)?"
    r"(?:covering|handling|taking over|replacing|the right person|your best contact)")

_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")

_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")
#: "back October 6", "returning on Monday, September 28th", "back in the office on
#: October 6", "hasta el 5 de octubre": the trigger and the date are allowed a few words
#: of prose between them, lazily, so the FIRST date after the trigger is the one read.
_RETURN = re.compile(
    r"(?:return(?:ing)?|back|until|till|til|through|hasta)\b"
    r"(?:[\s,.:;-]+(?:on|to|the|in|el|de|at|our|my|office|día)\b){0,4}[\s,.:;-]+"
    r"(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day\b[\s,]+)?"
    r"(?P<month>" + "|".join(_MONTHS) + r")\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:[\s,]+(?P<year>\d{4}))?", re.I)
_RETURN_ISO = re.compile(
    r"(?:return(?:ing)?|back|until|till|hasta)\b[^\n]{0,40}?(?P<iso>\d{4}-\d{2}-\d{2})", re.I)


@dataclass(frozen=True)
class ReplyVerdict:
    label: str
    confidence: str                       # 'text' when a rule matched, 'none' for unknown
    matched: List[str] = field(default_factory=list)
    returns_on: Optional[date] = None
    covering_contact: Optional[str] = None
    covering_email: Optional[str] = None
    provider_label: Optional[Any] = None

    @property
    def suppresses(self) -> bool:
        """Only these two stop us from ever using that address again."""
        return self.label in (OPT_OUT, NO_LONGER_HERE)

    @property
    def needs_review(self) -> bool:
        return self.label in (UNKNOWN, HUMAN_REPLY)

    def to_dict(self) -> Dict[str, Any]:
        return {"label": self.label, "confidence": self.confidence, "matched": self.matched,
                "returns_on": self.returns_on.isoformat() if self.returns_on else None,
                "covering_contact": self.covering_contact, "covering_email": self.covering_email,
                "provider_label": self.provider_label}


def _parse_return(text: str, *, received: Optional[datetime] = None) -> Optional[date]:
    iso = _RETURN_ISO.search(text)
    if iso:
        try:
            return date.fromisoformat(iso.group("iso"))
        except ValueError:
            return None
    match = _RETURN.search(text)
    if not match:
        return None
    month = _MONTHS.index(match.group("month").lower()) + 1
    day = int(match.group("day"))
    year = int(match.group("year")) if match.group("year") else (received or datetime.now(timezone.utc)).year
    try:
        parsed = date(year, month, day)
    except ValueError:
        return None
    if received and parsed < received.date():
        # "back March 3" on a December reply means next year, not nine months ago.
        try:
            parsed = date(year + 1, month, day)
        except ValueError:
            return None
    if received and (parsed - received.date()).days > MAX_LEAVE_DAYS:
        # A rollover that lands a year out is a date the text did not really give --
        # "until September 21" in a reply from September 22 is a leave that already
        # ended. Waiting a year is the same as never, so it is treated as no date and
        # the follow-up takes the ordinary short wait.
        return None
    return parsed


def _covering(text: str) -> tuple:
    match = _COVERING.search(text)
    if not match:
        return None, None
    name = (match.group("name") or match.group("name2") or "").strip()
    if not name or name.lower() in {"me", "us", "you", "them"}:
        return None, None
    tail = text[match.end(): match.end() + 200]
    email = _EMAIL.search(tail) or _EMAIL.search(text[max(0, match.start() - 120): match.start()])
    return name, (email.group(0).lower() if email else None)


def classify(subject: str = "", body: str = "", *, provider_label: Any = None,
             received_at: Optional[datetime] = None) -> ReplyVerdict:
    """Decide what one reply is. Pure: no database, no provider, no clock of its own."""
    text = f"{subject or ''}\n{body or ''}".strip()
    if not text:
        return ReplyVerdict(UNKNOWN, "none", provider_label=provider_label)
    matched: List[str] = []

    # Order matters. A departure auto-reply says both things; the departure is the fact
    # that changes what we do next.
    if _GONE.search(text):
        matched.append("no_longer_here")
        name, email = _covering(text)
        return ReplyVerdict(NO_LONGER_HERE, "text", matched, covering_contact=name, covering_email=email,
                            provider_label=provider_label)
    if _OPT_OUT.search(text):
        matched.append("opt_out")
        return ReplyVerdict(OPT_OUT, "text", matched, provider_label=provider_label)
    if _OOO.search(text):
        matched.append("out_of_office")
        name, email = _covering(text)
        return ReplyVerdict(OUT_OF_OFFICE, "text", matched, returns_on=_parse_return(text, received=received_at),
                            covering_contact=name, covering_email=email, provider_label=provider_label)
    # Something a person wrote, with no rule for it: a human decides, never a send.
    if len(text.split()) >= 4:
        return ReplyVerdict(HUMAN_REPLY, "text", ["human_reply"], provider_label=provider_label)
    return ReplyVerdict(UNKNOWN, "none", provider_label=provider_label)
