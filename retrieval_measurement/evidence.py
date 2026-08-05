"""Run lineage, the end-to-end waterfall, and fresh-inventory measurement.

Three capabilities that together answer "where did this number come from?":

* ``RunLineage`` -- run_id, commit, full redacted effective config, fingerprint,
  and the explicit stop metric. Reuses ``identity.py`` rather than duplicating
  its secret handling.
* ``Waterfall`` -- every stage boundary, in ONE declared unit, reconciled.
  Unit changes are recorded as transitions, never as yields: the 2026-07-30
  04:56 run went from 159 postings to 255 companies, and treating that as a
  "160% pass rate" or silently multiplying it into a conversion chain is how a
  funnel stops meaning anything.
* ``FreshInventory`` accounting -- new versus previously-processed companies,
  which is the measurement that decides whether a daily target is reachable at
  all. The pipeline has never produced it.

Nothing here changes business eligibility, outreach policy, or delivery. It
observes and reconciles.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from retrieval_measurement.identity import (
    ReadOnlySeenSnapshot,
    assert_no_secret_values,
    config_fingerprint,
    effective_config_snapshot,
    git_identity,
    new_run_id,
    python_identity,
    utc_stamp,
)
from retrieval_measurement.schema import (
    UNITS,
    FreshInventory,
    StateCounts,
    WaterfallBoundary,
)


class WaterfallUnreconciled(RuntimeError):
    """A boundary did not account for every record that entered it."""


# --------------------------------------------------------------------------
# Lineage
# --------------------------------------------------------------------------


class RunLineage:
    """Identity every artifact of a run must carry."""

    def __init__(
        self,
        *,
        run_id: Optional[str] = None,
        run_arguments: Optional[Mapping[str, Any]] = None,
        repo_root: Optional[Path] = None,
    ) -> None:
        entries = effective_config_snapshot(dict(run_arguments or {}))
        # Fail loudly rather than write a secret into a run artifact.
        assert_no_secret_values(entries)
        git = git_identity(repo_root)
        python = python_identity()
        self.run_id = run_id or new_run_id()
        self.started_at = utc_stamp()
        self.git_commit = git["commit"]
        self.git_branch = git["branch"]
        self.git_dirty = git["dirty"]
        self.python_version = python["python_version"]
        self.platform = python["platform"]
        self.effective_config = [entry.to_dict() for entry in entries]
        self.config_fingerprint = config_fingerprint(entries)
        self.stop_metric = ""
        self.stop_reason = ""

    def declare_stop(self, metric: str, reason: str) -> None:
        """Record WHICH metric ended the run, not only that something did.

        ``stop_reason='final_pass_target_reached'`` while FINAL_PASS was 15 of
        30 is unfalsifiable without this: the reason named a metric that had
        not in fact been reached.
        """
        self.stop_metric = str(metric)
        self.stop_reason = str(reason)

    def stamp(self) -> Dict[str, Any]:
        """The block every artifact embeds."""
        return {
            "run_id": self.run_id,
            "git_commit": self.git_commit,
            "git_branch": self.git_branch,
            "git_dirty": self.git_dirty,
            "config_fingerprint": self.config_fingerprint,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.stamp(),
            "started_at": self.started_at,
            "python_version": self.python_version,
            "platform": self.platform,
            "stop_metric": self.stop_metric,
            "stop_reason": self.stop_reason,
            "effective_config": self.effective_config,
        }


# --------------------------------------------------------------------------
# Waterfall
# --------------------------------------------------------------------------


class Waterfall:
    """Ordered stage boundaries with enforced reconciliation."""

    def __init__(self, run_id: str = "") -> None:
        self.run_id = run_id
        self.boundaries: List[WaterfallBoundary] = []
        self.states = StateCounts()

    def add(
        self,
        name: str,
        *,
        unit: str,
        entered: int,
        passed: int,
        rejected: int = 0,
        deferred: int = 0,
        errored: int = 0,
        primary_reasons: Optional[Mapping[str, int]] = None,
        secondary_reasons: Optional[Mapping[str, int]] = None,
        note: str = "",
    ) -> WaterfallBoundary:
        transition = ""
        previous = self.boundaries[-1] if self.boundaries else None
        if previous is not None and previous.unit != unit:
            transition = f"{previous.unit}->{unit}"
        boundary = WaterfallBoundary(
            name=name,
            unit=unit,
            entered=int(entered),
            passed=int(passed),
            rejected=int(rejected),
            deferred=int(deferred),
            errored=int(errored),
            primary_reasons=dict(primary_reasons or {}),
            secondary_reasons=dict(secondary_reasons or {}),
            unit_transition=transition,
            note=note,
        )
        # Cumulative survival is only meaningful WITHIN one unit. Across a unit
        # change it is arithmetic on two different populations, so it is left
        # unset rather than computed and quietly believed.
        same_unit = [b for b in self.boundaries if b.unit == unit]
        if same_unit:
            first = same_unit[0].entered
            boundary.cumulative_survival = (
                round(boundary.passed / first, 6) if first else None
            )
        else:
            boundary.cumulative_survival = (
                round(boundary.passed / boundary.entered, 6) if boundary.entered else None
            )
        self.boundaries.append(boundary)
        return boundary

    def record_states(self, **counts: int) -> StateCounts:
        for key, value in counts.items():
            if not hasattr(self.states, key):
                raise ValueError(f"unknown terminal state {key!r}")
            setattr(self.states, key, int(value))
        return self.states

    def unreconciled(self) -> List[WaterfallBoundary]:
        return [b for b in self.boundaries if not b.reconciles]

    def reason_mismatches(self) -> List[Tuple[str, int, int]]:
        """Boundaries whose primary reason codes do not sum to their rejections."""
        out = []
        for b in self.boundaries:
            if b.primary_reasons and b.reason_total() != b.rejected:
                out.append((b.name, b.reason_total(), b.rejected))
        return out

    def assert_reconciled(self) -> None:
        bad = self.unreconciled()
        if bad:
            raise WaterfallUnreconciled(
                "boundaries do not account for every record: "
                + ", ".join(
                    f"{b.name}(entered={b.entered}, accounted={b.accounted})" for b in bad
                )
            )
        mismatched = self.reason_mismatches()
        if mismatched:
            raise WaterfallUnreconciled(
                "primary reason codes do not sum to rejections: "
                + ", ".join(f"{n}({got} != {want})" for n, got, want in mismatched)
            )

    def unit_transitions(self) -> List[str]:
        return [b.unit_transition for b in self.boundaries if b.unit_transition]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "units": list(UNITS),
            "boundaries": [b.to_dict() for b in self.boundaries],
            "unit_transitions": self.unit_transitions(),
            "reconciled": not self.unreconciled() and not self.reason_mismatches(),
            "terminal_states": self.states.to_dict(),
            # Reported separately, on purpose. An Airtable row is not a
            # FINAL_PASS lead and an enrolled contact is not either.
            "reviewable_rows": self.states.reviewable,
            "final_pass_leads": self.states.final_pass,
            "airtable_created": self.states.airtable_created,
            "outbound_enrolled": self.states.outbound_enrolled,
        }


# --------------------------------------------------------------------------
# Fresh inventory
# --------------------------------------------------------------------------


def company_identity(job: Mapping[str, Any]) -> str:
    """Stable company key across runs.

    Domain first because it survives naming variation ("Acme, Inc." vs "Acme");
    normalised employer name only when no domain is known.
    """
    from domain_utils import normalize_company_domain

    domain = normalize_company_domain(
        str(job.get("employer_website") or job.get("employer_domain") or "")
    )
    if domain:
        return f"domain:{domain}"
    name = " ".join(str(job.get("employer_name") or "").lower().split())
    for suffix in (" inc", " inc.", " llc", " ltd", " corp", " corporation", " limited"):
        if name.endswith(suffix):
            name = name[: -len(suffix)].strip()
    return f"name:{name}" if name else ""


class CompanyLedger:
    """Bounded, read-only history of companies already processed.

    Deliberately not ``SeenJobsRegistry``: that class creates directories and
    can move a corrupt file aside on load. This one reads a JSON snapshot and
    never writes anything back.
    """

    def __init__(self, seen: Optional[Iterable[str]] = None, *, path: str = "") -> None:
        self.seen = {str(key) for key in (seen or []) if key}
        self.path = path

    @classmethod
    def empty(cls) -> "CompanyLedger":
        return cls()

    @classmethod
    def load(cls, path: str | Path, *, max_entries: int = 500_000) -> "CompanyLedger":
        target = Path(path)
        if not target.is_file():
            raise FileNotFoundError(f"company ledger not found: {target}")
        with open(target, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            keys = list(data.get("companies") or data.keys())
        elif isinstance(data, list):
            keys = list(data)
        else:
            raise ValueError(f"unrecognised company ledger shape: {target}")
        return cls(keys[:max_entries], path=str(target))

    def has(self, key: str) -> bool:
        return bool(key) and key in self.seen


def measure_fresh_inventory(
    jobs: Sequence[Mapping[str, Any]],
    *,
    run_id: str = "",
    ledger: Optional[CompanyLedger] = None,
    eligible_companies: Optional[Iterable[str]] = None,
    suppressed_companies: Optional[Iterable[str]] = None,
    posting_identities: Optional[Iterable[str]] = None,
    opportunity_keys: Optional[Iterable[str]] = None,
) -> FreshInventory:
    """Split observed companies into new versus previously processed.

    Without a ledger every company is reported as new AND
    ``snapshot_available`` is False, so a first run cannot be mistaken for
    evidence of infinite fresh supply.
    """
    ledger = ledger or CompanyLedger.empty()
    eligible = {str(k) for k in (eligible_companies or [])}
    suppressed = {str(k) for k in (suppressed_companies or [])}

    observed: set[str] = set()
    for job in jobs:
        key = company_identity(job)
        if key:
            observed.add(key)

    new_companies = {key for key in observed if not ledger.has(key)}
    previously = observed - new_companies

    return FreshInventory(
        run_id=run_id,
        new_posting_identities=len({str(k) for k in (posting_identities or []) if k}),
        new_opportunities=len({str(k) for k in (opportunity_keys or []) if k}),
        new_companies=len(new_companies),
        new_icp_eligible_companies=len(new_companies & eligible) if eligible else 0,
        previously_processed_companies=len(previously),
        suppressed_companies=len(observed & suppressed) if suppressed else 0,
        total_companies_observed=len(observed),
        snapshot_available=bool(ledger.seen),
    )


def depletion(series: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Cross-run trend in new eligible companies.

    ``companies_considered`` fell 255 -> 131 -> 78 across three runs on
    2026-07-30. A target that depends on fresh companies is not reachable if
    this curve keeps falling, so the curve is reported rather than a single run.
    """
    runs = [dict(item) for item in series]
    if not runs:
        return {"runs": 0, "trend": "unknown"}
    values = [int(item.get("new_companies", 0)) for item in runs]
    first, last = values[0], values[-1]
    trend = "flat"
    if last < first * 0.9:
        trend = "declining"
    elif last > first * 1.1:
        trend = "growing"
    return {
        "runs": len(values),
        "series": values,
        "first": first,
        "last": last,
        "delta": last - first,
        "trend": trend,
        "sustainable": trend != "declining",
    }
