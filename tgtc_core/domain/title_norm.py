"""The ONE title normaliser: canonical spelling, content tokens, and the
predicates the contact gates and the candidate ranking share.

Phase 4 audit (2026-09-20). The measurement this exists to fix is
`funnel_audit_20260919/phase5_apollo_2a/STAGE2A_REPORT.md` §3 with its raw
per-person record `state/dropscan.jsonl`: over the frozen 100-unit frame the
deployed selector returned 615 people and refused 70 of them with
``contact:function_or_authority_mismatch``. Reading the 70 titles, they are
ordinary spellings of titles the buyer hierarchies in ``policy/campaigns.py``
already list.

Phase 2 task 8 put the first half of the answer into ``gates.py``: collapse the
seniority wording (Vice President/SVP/EVP -> VP, Human Resources -> HR) and
bridge a leading ``"<Seniority>, <Function>"`` comma shape. That half is a
CONTIGUOUS-PHRASE search, and a phrase search cannot bridge word order or an
inserted word:

    "Director of Human Resources"  (a buyer title)
    "Director Human Resources"     (what the person actually wrote)

Neither string contains the other. This module adds the second half -- a
token-set rule -- and keeps both halves in one place so there is exactly one
answer to "is this person's title one of the titles we are buying".

Three properties hold, and the tests pin all three:

1. **Strictly additive**, with two named and counted exceptions. Every other
   title that matched before still matches, so the normaliser essentially
   cannot lose a candidate.
   (a) The operations over-match guard now also fires when the unrelated-domain
       qualifier sits AFTER the phrase ("Operations Manager, Warehouse") rather
       than only before it -- the token rule has no notion of "the word
       immediately before the match", so the guard is restated over the token
       set, and restating it symmetrically is the honest form of it.
   (b) ``NOT_AN_EMPLOYEE_IN_ROLE_PHRASES`` applies on both paths. Over the
       frozen frame that removes 2 of 513 accepted candidates, both outside
       contractors ("Founder, Fractional CFO"; a consulting accounting
       manager) -- the "current versus former employer" audit item, applied to
       the only evidence a free search carries.
2. **The new path is guarded.** Ignoring word order is exactly what lets
   "Assistant Manager Human Resources" reach "HR Manager", so the token rule
   refuses a title carrying a non-decision-maker qualifier, refuses a bare
   department name, and carries Q21's operations guard.
3. **It does not reopen the founder/president collision** phase 2 task 7 closed.
   ``campaigns.is_founder_tier`` is the single owner of that question and it is
   re-asserted here against the CANONICAL form too, because this module
   rewrites the very phrase ("vice president") the collision turned on.

The separator class and the base normalisation are ``campaigns.py``'s and are
imported, not restated: ``domain/contact_mapping.py`` delegates to
``base_normalize`` below for exactly the same reason. ``contact_mapping`` does
NOT use ``canonical_title``, and that is deliberate, not an oversight: its
``seniority()`` has to keep SVP and EVP distinct from VP to rank a buyer against
the opening, and canonicalisation deliberately collapses them. Two different
questions, one shared base, no duplicated regex.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Sequence, Set, Tuple

from ..policy.campaigns import split_title_separators

#: Words that carry no meaning for title identity. Dropped to form content
#: tokens, so "Director of Human Resources" and "Director Human Resources" are
#: the same title, which is the whole point.
CONNECTIVES = frozenset({"of", "the", "and", "for", "a", "an", "to", "in", "on", "at", "with"})

#: Equivalent spellings, applied in order (longest form first). Every entry is
#: either an abbreviation of the other side or the same word in another part of
#: speech; none of them widens a title into a DIFFERENT job.
_EQUIVALENCES: Tuple[Tuple[re.Pattern, str], ...] = tuple((re.compile(p), r) for p, r in (
    # --- C-suite spelled out -> the abbreviation the buyer lists are written in.
    (r"\bchief executive officer\b", "ceo"),
    (r"\bchief (?:technology|technical) officer\b", "cto"),
    (r"\bchief information officer\b", "cio"),
    (r"\bchief financial officer\b", "cfo"),
    (r"\bchief marketing officer\b", "cmo"),
    (r"\bchief revenue officer\b", "cro"),
    (r"\bchief operat(?:ing|ions) officer\b", "coo"),
    (r"\bchief product officer\b", "cpo"),
    (r"\bchief (?:people|human resources) officer\b", "chro"),
    (r"\bchief customer officer\b", "cco"),
    # --- Vice-president family. Assistant/Associate VP is DIRECTOR-equivalent,
    # the same reading ``contact_mapping.seniority`` already gives AVP. The
    # rewrite consumes the word "assistant", which is why an AVP does not then
    # trip the non-decision-maker guard.
    (r"\b(?:assistant|associate) vice president\b", "director"),
    (r"\bavp\b", "director"),
    (r"\b(?:senior|executive|group|global|corporate|regional|sector|divisional) vice president\b", "vp"),
    (r"\b(?:svp|evp|gvp)\b", "vp"),
    (r"\bvice president\b", "vp"),
    # --- Function words.
    (r"\bhuman resources\b", "hr"),
    (r"\bfinancial\b", "finance"),
    (r"\b(?:revops|rev ops)\b", "revenue operations"),
    (r"\b(?:salesops|sales ops)\b", "sales operations"),
    (r"\bpeople ops\b", "people operations"),
    (r"\bbiz ops\b", "business operations"),
    (r"\bmarketing ops\b", "marketing operations"),
    (r"\be commerce\b", "ecommerce"),
    # --- Ordinary abbreviations observed in the corpus.
    (r"\bmgr\b", "manager"),
    (r"\bcofounder\b", "co founder"),
))

#: A title saying, in the title itself, that this person does not hold this role
#: at this employer NOW: they left it, they advise it, or they sell it as a
#: service. This is the "current versus former employer" check applied to the
#: one piece of evidence a free search actually carries.
#:
#: It is the only part of the phase-2 matcher's behaviour this change NARROWS,
#: and the narrowing is counted, not asserted: over the frozen 100-unit frame
#: (`state/dropscan.jsonl`) it removes **2** of 513 candidates production
#: currently accepts -- "…consultant, consulting accounting manager" and
#: "Founder, Fractional CFO". Both are outside contractors.
NOT_AN_EMPLOYEE_IN_ROLE_PHRASES: Tuple[str, ...] = (
    "former", "retired", "consultant", "contractor", "fractional", "intern", "student",
    "volunteer", "advisor", "adviser", "board member", "board of", "investor",
)

#: Shared with ``contact_mapping.EXCLUDED_TOKENS`` -- ONE list, imported there,
#: not a second copy. A title carrying one of these is not the person who owns
#: the opening, however well the rest of the words line up.
#:
#: The two extra entries beyond the list above ("assistant", "recruiter") are
#: seniority/function qualifiers rather than employment facts, so they guard the
#: NEW token rule only. The token rule ignores word order, which is precisely
#: what would let "Assistant Manager Human Resources" reach "HR Manager"; the
#: phrase rule never could, and "Assistant Director of Marketing" matching
#: "Director of Marketing" through it is pre-existing behaviour this change is
#: not chartered to revisit (3 candidates in the frame).
NON_DECISION_MAKER_PHRASES: Tuple[str, ...] = (
    "assistant", "intern", "advisor", "adviser", "board member", "board of", "investor", "former",
    "retired", "consultant", "contractor", "fractional", "student", "volunteer", "recruiter",
)

#: The department names Apollo puts in the ``title`` field. A title is the NAME
#: OF A DEPARTMENT only when it is built entirely out of these and carries no
#: role token -- "Marketing", "Human Resources", "Customer Success".
#:
#: The narrower rule matters: "no role token" ALONE is not the test, or every
#: individual-contributor title ("Warehouse Associate", "Staff Accountant")
#: would be called a department, which is both wrong and a different loss.
DEPARTMENT_TOKENS = frozenset({
    "marketing", "sales", "finance", "accounting", "engineering", "technology", "tech", "product",
    "design", "operations", "hr", "people", "talent", "acquisition", "recruiting", "support",
    "success", "customer", "client", "service", "services", "experience", "it", "legal",
    "compliance", "procurement", "ecommerce", "revenue", "growth", "data", "analytics", "security",
    "research", "development", "communications", "creative", "brand", "content", "business",
    "corporate", "administration", "admin", "information", "systems", "digital",
})

#: A title needs at least one of these to be a JOB title rather than the name of
#: a department. Apollo returns both in the same field.
ROLE_TOKENS = frozenset({
    "manager", "director", "head", "vp", "chief", "officer", "president", "founder", "owner",
    "lead", "leader", "principal", "partner", "controller", "supervisor", "executive", "administrator",
    "ceo", "cto", "cfo", "cmo", "cro", "coo", "cpo", "chro", "cco", "cio", "cdo", "caio", "cpto",
})

#: Q21 (phase 2 task 8, fix round 1 C1): these three phrases are generic English
#: words for "running things", which unrelated domains use for their OWN work.
#: Every other buyer-title phrase already names its own function, so the guard
#: stays scoped to exactly what was measured.
OPERATIONS_AMBIGUOUS_PHRASES = frozenset({"operations manager", "operations director", "director of operations"})
UNRELATED_TITLE_QUALIFIERS: Tuple[str, ...] = ("warehouse", "clinical", "it")


def base_normalize(title) -> str:
    """Lower-cased, punctuation-free, with role separators already turned into
    spaces by ``campaigns.split_title_separators`` and ``sr`` spelled out.

    This is the normalisation ``contact_mapping.normalize_title`` has always
    applied; it lives here so both callers share it.
    """
    t = split_title_separators(title)
    t = re.sub(r"[^a-z0-9 ]+", "", t)
    t = re.sub(r"\bsr\b", "senior", t)
    return " ".join(t.split())


def canonical_title(title) -> str:
    """``base_normalize`` plus the equivalent-spelling rewrites."""
    t = base_normalize(title)
    for pattern, replacement in _EQUIVALENCES:
        t = pattern.sub(replacement, t)
    return " ".join(t.split())


def content_tokens(title) -> Tuple[str, ...]:
    """The canonical title's tokens with connectives removed, in order."""
    return tuple(t for t in canonical_title(title).split() if t not in CONNECTIVES)


def _phrase_forms(title) -> Tuple[str, ...]:
    """Contiguous-phrase forms of one title: the canonical string, the same
    string with connectives dropped, and -- for a leading
    ``"<Seniority>, <Function>"`` shape -- the two word orders the buyer lists
    are themselves written in.

    Dropping connectives is what keeps this strictly additive across the change
    of base normalisation: ``gates._norm`` used to delete "&" outright, so
    "Sales & Operations Director" read as "sales operations director"; here "&"
    becomes "and" and the connective-free form restores that reading.
    """
    canonical = canonical_title(title)
    forms = [canonical, " ".join(t for t in canonical.split() if t not in CONNECTIVES)]
    m = re.match(r"^([A-Za-z .&/-]+?),\s*(.+)$", str(title or "").strip())
    if m:
        lead, rest = canonical_title(m.group(1)), canonical_title(m.group(2))
        if lead and rest:
            forms.append(f"{rest} {lead}")
            forms.append(f"{lead} of {rest}")
    seen: List[str] = []
    for f in forms:
        if f and f not in seen:
            seen.append(f)
    return tuple(seen)


def _has_phrase(canonical: str, phrases: Iterable[str]) -> bool:
    return any(re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", canonical) for p in phrases)


def not_an_employee_in_role(title) -> bool:
    """The title itself says the person does not hold this role here now."""
    return _has_phrase(canonical_title(title), NOT_AN_EMPLOYEE_IN_ROLE_PHRASES)


def non_decision_maker_title(title) -> bool:
    """A qualifier that says this person does not own the opening."""
    return _has_phrase(canonical_title(title), NON_DECISION_MAKER_PHRASES)


def is_department_label(title) -> bool:
    """The title names a department ("Marketing", "Human Resources") rather than
    a job. It carries no authority evidence, so it is not a buyer -- but it is a
    different loss from "the wrong function", and the gates now say which."""
    tokens = content_tokens(title)
    if not tokens or any(t in ROLE_TOKENS for t in tokens):
        return False
    return all(t in DEPARTMENT_TOKENS for t in tokens)


def _operations_guard_trips(target_forms: Iterable[str], title_tokens: Set[str]) -> bool:
    if not any(f in OPERATIONS_AMBIGUOUS_PHRASES for f in target_forms):
        return False
    return any(q in title_tokens for q in UNRELATED_TITLE_QUALIFIERS)


def title_matches(title: str, targets: Iterable[str]) -> bool:
    """Is ``title`` one of ``targets``? The single buyer-title predicate.

    Two rules, in order. The phrase rule is phase 2 task 8's, unchanged in
    outcome. The token rule is new: every content token of the target is present
    in the title, in any order, with inserted words allowed -- guarded against
    the three ways that widening would otherwise admit the wrong person.
    """
    if not_an_employee_in_role(title):
        return False
    title_forms = _phrase_forms(title)
    title_tokens = set(content_tokens(title))
    fallback: List[Tuple[Tuple[str, ...], Set[str]]] = []
    for target in targets:
        target_forms = _phrase_forms(target)
        target_tokens = set(content_tokens(target))
        if not target_tokens:
            continue
        for c in target_forms:
            for v in title_forms:
                if v == c or re.search(r"\b" + re.escape(c) + r"\b", v):
                    if _operations_guard_trips(target_forms, title_tokens):
                        continue
                    return True
        fallback.append((target_forms, target_tokens))
    if non_decision_maker_title(title) or is_department_label(title):
        return False
    for target_forms, target_tokens in fallback:
        if target_tokens <= title_tokens and not _operations_guard_trips(target_forms, title_tokens):
            return True
    return False


#: Phase 4, measured and reported rather than coded around: Apollo's free
#: people-search returns NEITHER ``departments`` NOR ``seniority``. Both are
#: empty on all 512 candidates Stage 2a saved
#: (`phase5_apollo_2a/state/search.jsonl`, 0 credits). So the "department versus
#: job title" question has exactly one reachable half before a paid call --
#: ``is_department_label`` above, on the title Apollo does return -- and a
#: helper that read ``person["departments"]`` would be dead code dressed as a
#: signal. It is deliberately absent.


def matched_target_index(title: str, targets: Sequence[str]) -> int:
    """Index of the first target this title matches, or ``len(targets)``.

    One place, so the gate that accepts a candidate and the ranking that orders
    it can never disagree about WHICH buyer title was matched.
    """
    for i, target in enumerate(targets):
        if title_matches(title, [target]):
            return i
    return len(targets)
