"""Artifact writing, bounded retention, and the human-readable report.

Retention here is deliberately timid. The production pipeline has no eviction
anywhere -- ``source_cache.py:38`` treats an expired entry as a miss and never
unlinks it, and the only ``unlink`` calls in the whole codebase belong to the
checkpoint and the lock -- which is how a 5 GB volume filled up. The fix is not
for a measurement harness to start deleting things. So retention **reports by
default** and deletes only when explicitly asked, only inside its own artifact
root, only for directories it created, and never through a symlink.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from retrieval_measurement.schema import CLAIM_BOUNDARY

ARTIFACT_DIRNAME = "retrieval_measurement"

#: Retention bounds. Generous for local work, and two orders of magnitude below
#: the volume that failed in production.
MAX_RUNS = 20
MAX_AGE_DAYS = 30
MAX_TOTAL_BYTES = 500 * 1024 * 1024

#: Path fragments that must never appear in a deletion candidate. Production
#: artifacts are not the harness's to manage, under any flag.
PROTECTED_FRAGMENTS = (
    ("data", "raw"),
    ("data", "filtered"),
    ("data", "enriched"),
    ("data", "state"),
    ("data", "evidence"),
    ("data", "replay"),
    ("logs",),
)


class RetentionRefused(RuntimeError):
    """Raised when a deletion candidate fails a safety check."""


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def run_dir(root: str | Path, run_id: str) -> Path:
    return Path(root) / ARTIFACT_DIRNAME / run_id


def atomic_write_text(path: str | Path, text: str) -> int:
    """Write via temp file + os.replace, so a crashed run never leaves a
    half-written artifact that later reads as truth."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", delete=False, dir=target.parent, suffix=".tmp"
    )
    try:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, target)
    return len(text.encode("utf-8"))


def atomic_write_json(path: str | Path, payload: Any) -> int:
    return atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=False, default=str))


def write_jsonl_gz(path: str | Path, rows: Iterable[Mapping[str, Any]]) -> int:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    with gzip.open(temp, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, default=str))
            handle.write("\n")
    os.replace(temp, target)
    return target.stat().st_size


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------


@dataclass
class RetentionCandidate:
    path: str
    run_id: str
    bytes: int
    age_days: float
    reasons: List[str] = field(default_factory=list)


@dataclass
class RetentionReport:
    root: str
    applied: bool
    total_runs: int = 0
    total_bytes: int = 0
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)
    refused: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "root": self.root,
            "applied": self.applied,
            "total_runs": self.total_runs,
            "total_bytes": self.total_bytes,
            "candidates": self.candidates,
            "deleted": self.deleted,
            "refused": self.refused,
            "bounds": {
                "max_runs": MAX_RUNS,
                "max_age_days": MAX_AGE_DAYS,
                "max_total_bytes": MAX_TOTAL_BYTES,
            },
        }


def _dir_bytes(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        if item.is_file() and not item.is_symlink():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def _is_protected(path: Path) -> bool:
    parts = [part.lower() for part in path.resolve().parts]
    for fragment in PROTECTED_FRAGMENTS:
        lowered = [part.lower() for part in fragment]
        for index in range(len(parts) - len(lowered) + 1):
            if parts[index:index + len(lowered)] == lowered:
                return True
    return False


def assert_deletable(candidate: Path, artifact_root: Path) -> None:
    """Four independent checks, all of which must pass.

    Any one of them alone would probably be enough. Given what unbounded growth
    already cost this system, "probably" is not the standard.
    """
    resolved = candidate.resolve()
    root = artifact_root.resolve()
    if candidate.is_symlink():
        raise RetentionRefused(f"refusing to delete a symlink: {candidate}")
    if not resolved.is_dir():
        raise RetentionRefused(f"not a directory: {candidate}")
    if root not in resolved.parents:
        raise RetentionRefused(f"outside the harness artifact root: {resolved}")
    if _is_protected(resolved):
        raise RetentionRefused(f"resolves into a protected production directory: {resolved}")


def evaluate_retention(
    artifact_root: str | Path,
    *,
    apply: bool = False,
    now: Optional[datetime] = None,
    max_runs: int = MAX_RUNS,
    max_age_days: int = MAX_AGE_DAYS,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> RetentionReport:
    """Report what retention *would* remove. Delete only if ``apply=True``."""
    root = Path(artifact_root) / ARTIFACT_DIRNAME
    report = RetentionReport(root=str(root), applied=bool(apply))
    if not root.is_dir():
        return report

    moment = now or datetime.now(timezone.utc)
    runs: List[RetentionCandidate] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        try:
            age = (moment - datetime.fromtimestamp(child.stat().st_mtime, timezone.utc)).total_seconds() / 86400.0
        except OSError:
            continue
        runs.append(RetentionCandidate(
            path=str(child), run_id=child.name, bytes=_dir_bytes(child), age_days=round(age, 3)
        ))

    runs.sort(key=lambda item: item.run_id)
    report.total_runs = len(runs)
    report.total_bytes = sum(item.bytes for item in runs)

    doomed: Dict[str, RetentionCandidate] = {}

    for item in runs:
        if item.age_days > max_age_days:
            doomed.setdefault(item.path, item).reasons.append(
                f"older than {max_age_days} days ({item.age_days:.1f})"
            )

    excess = len(runs) - max_runs
    for item in runs[:max(0, excess)]:
        doomed.setdefault(item.path, item).reasons.append(f"beyond the {max_runs} most recent runs")

    if report.total_bytes > max_total_bytes:
        running = report.total_bytes
        for item in runs:
            if running <= max_total_bytes:
                break
            doomed.setdefault(item.path, item).reasons.append(
                f"total artifact bytes {report.total_bytes} exceed {max_total_bytes}"
            )
            running -= item.bytes

    for path in sorted(doomed):
        item = doomed[path]
        report.candidates.append({
            "path": item.path,
            "run_id": item.run_id,
            "bytes": item.bytes,
            "age_days": item.age_days,
            "reasons": item.reasons,
        })

    if not apply:
        return report

    for path in sorted(doomed):
        candidate = Path(path)
        try:
            assert_deletable(candidate, Path(artifact_root))
        except RetentionRefused as exc:
            report.refused.append(str(exc))
            continue
        shutil.rmtree(candidate)
        report.deleted.append(path)
    return report


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

TYPICAL_WEEK_PROTOCOL = """\
### Measuring a typical week (not yet done)

This run cannot answer the weekly-inventory question. To get there, the
following is required, and each element exists because omitting it produces a
number that looks defensible and is not:

1. **Repetition.** At least 14 consecutive daily runs at a fixed configuration.
   Report the median and interquartile range across days. Never multiply one
   day by seven: posting volume is strongly day-of-week dependent.
2. **Cross-day deduplication.** A harness-owned posting-identity ledger,
   separate from the production seen-jobs registry. A posting visible on days
   1-5 counts once toward weekly inventory; its reappearances are persistence,
   not new supply.
3. **Posting-date bucketing.** Bucket by the provider's `posted_at`, not by
   fetch date, and state the right-censoring explicitly: postings published
   near either window edge are under-observed. Each run already records its
   first and last observed `posted_at` per source for this purpose.
4. **Cross-source overlap.** Pairwise overlap matrices over the posting
   identity ladder, so unioned inventory is measured rather than assumed.
   Denominators from different providers are never summed.
5. **Stated uncertainty.** Every weekly figure carries a range, names which
   sources contributed a provider denominator and which did not, and names the
   unmeasured fraction rather than estimating it.
"""


def _fmt(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, float):
        return f"{value:.1%}" if 0 <= value <= 1 else f"{value:.3f}"
    return f"{value:,}" if isinstance(value, int) else str(value)


def render_report(summary: Mapping[str, Any], manifest: Mapping[str, Any]) -> str:
    """Human-readable summary. Leads with the claim boundary, on purpose."""
    lines: List[str] = []
    lines.append(f"# Retrieval measurement - run {summary.get('run_id', '')}")
    lines.append("")
    lines.append("**Milestone 1 candidate. Not production-ready; not yet reviewed, and no "
                 "approved live acquisition validation has been run.**")
    lines.append("")
    lines.append("## Claim boundary")
    lines.append("")
    lines.append(CLAIM_BOUNDARY)
    lines.append("")
    lines.append(f"- mode: `{summary.get('mode', '')}`")
    lines.append(f"- commit: `{manifest.get('git_commit', '') or 'unknown'}`"
                 f" (branch `{manifest.get('git_branch', '') or 'unknown'}`,"
                 f" dirty={manifest.get('git_dirty')})")
    lines.append(f"- python: `{manifest.get('python_version', '')}`")
    lines.append(f"- config fingerprint: `{manifest.get('config_fingerprint', '')}`")
    snapshot = manifest.get("seen_snapshot") or {}
    lines.append(
        f"- seen-jobs snapshot: {'present' if snapshot.get('available') else 'ABSENT'}"
        f" ({snapshot.get('job_ids', 0)} job ids, read-only)"
    )
    selected = manifest.get("lanes_selected") or summary.get("lanes_selected") or []
    lines.append(
        "- lanes selected: "
        + (", ".join(f"`{lane}`" for lane in selected) or "none")
        + (f" (via {manifest.get('lane_selection_source')})"
           if manifest.get("lane_selection_source") else "")
    )
    lines.append("")

    budget = summary.get("request_budget") or manifest.get("request_budget") or {}
    if budget.get("enforced"):
        lines.append(
            f"- outbound requests: {_fmt(budget.get('requests_completed'))} of a "
            f"{_fmt(budget.get('limit'))} global ceiling"
            + (f" — **STOPPED: {budget.get('stop_reason')}**" if budget.get("stop_reason") else "")
        )
        lines.append("")
    if budget.get("stop_reason"):
        lines.append("## Request ceiling reached")
        lines.append("")
        lines.append(
            "This run stopped because it hit **our own** global request ceiling. That is "
            "not provider exhaustion, not an empty page, not a provider quota and not a "
            "network error: nothing may be inferred from it about how much inventory any "
            "provider still had. Everything retrieved before the stop is reported and "
            "reconciled; the run is marked incomplete."
        )
        lines.append("")
        blocked = budget.get("blocked_next_request") or {}
        lines.append(f"- requests completed: {_fmt(budget.get('requests_completed'))} "
                     f"of {_fmt(budget.get('limit'))}")
        lines.append(f"- blocked request: #{blocked.get('sequence')} "
                     f"lane `{blocked.get('lane')}` source `{blocked.get('source')}` "
                     f"host `{blocked.get('hostname')}`")
        lines.append("")

    lane_failures = summary.get("lane_failures") or manifest.get("lane_failures") or []
    if lane_failures:
        lines.append("## Lane failures")
        lines.append("")
        lines.append(
            f"**{len(lane_failures)} selected lane(s) failed. This run is PARTIAL.** A failed "
            "lane says nothing about whether that provider had more inventory, so it is "
            "recorded here and never as truncation or provider exhaustion."
        )
        lines.append("")
        lines.append("| lane | exception | requests before failure | error (redacted) |")
        lines.append("|---|---|---:|---|")
        for failure in lane_failures:
            lines.append(
                f"| `{failure.get('lane')}` | `{failure.get('exception_type')}` "
                f"| {_fmt(failure.get('requests_attempted_before_failure'))} "
                f"| {failure.get('error', '')} |"
            )
        lines.append("")

    lines.append("## Run totals")
    lines.append("")
    posting = summary.get("run_baseline_posting_identity") or {}
    production = summary.get("run_baseline_production_equivalent") or {}
    lines.append("| baseline | gross returned | unique in run | previously seen | incremental new |")
    lines.append("|---|---:|---:|---:|---:|")
    for label, block in (("posting identity", posting), ("production equivalent", production)):
        lines.append(
            f"| {label} | {_fmt(block.get('gross_returned'))} | {_fmt(block.get('unique_in_run'))} "
            f"| {_fmt(block.get('previously_seen'))} | {_fmt(block.get('incremental_new'))} |"
        )
    lines.append("")
    lines.append(
        "*Posting identity* counts distinct job postings and is the inventory number. "
        "*Production equivalent* applies the live pipeline's `(company, title)` dedupe "
        "and is the funnel number. The gap between them is real duplication of "
        "openings, not error."
    )
    lines.append("")

    lines.append("## Per source")
    lines.append("")
    lines.append("| source | returned | kept | unique (posting) | unique (production) | provider total | capture rate | truncated |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---|")
    for metric in summary.get("per_source", []):
        uniq = metric.get("uniqueness") or {}
        denominator = metric.get("denominator") or {}
        truncations = [
            record for record in metric.get("truncation", [])
            if record.get("detected")
        ]
        kinds = ", ".join(sorted({record.get("kind", "") for record in truncations})) or "no"
        lines.append(
            f"| `{metric.get('source')}` | {_fmt(metric.get('canonical_records'))} "
            f"| {_fmt(metric.get('kept_after_removals'))} "
            f"| {_fmt(uniq.get('unique_posting_identity'))} "
            f"| {_fmt(uniq.get('unique_production_equivalent'))} "
            f"| {_fmt(denominator.get('value'))} "
            f"| {_fmt(metric.get('capture_rate'))} | {kinds} |"
        )
    lines.append("")

    lines.append("## Capture rate scope")
    lines.append("")
    with_denominator = summary.get("sources_with_denominator") or []
    without = summary.get("sources_without_denominator") or []
    lines.append(
        f"- Sources publishing their own total ({len(with_denominator)}): "
        + (", ".join(f"`{name}`" for name in with_denominator) or "none")
    )
    lines.append(
        f"- Sources with NO published total ({len(without)}): "
        + (", ".join(f"`{name}`" for name in without) or "none")
    )
    lines.append("")
    lines.append(
        "**No total-US-market capture rate is reported.** "
        + str(summary.get("total_market_estimate_reason", ""))
    )
    lines.append("")

    reconciliation = summary.get("reconciliation") or {}
    status = "PASSED" if reconciliation.get("passed") else "FAILED"
    lines.append(f"## Reconciliation: {status}")
    lines.append("")
    failures = [check for check in reconciliation.get("checks", []) if not check.get("passed")]
    if failures:
        lines.append("| check | scope | stage | left | right | delta |")
        lines.append("|---|---|---|---:|---:|---:|")
        for check in failures:
            lines.append(
                f"| {check.get('name')} | {check.get('scope')} | {check.get('stage')} "
                f"| {check.get('left')} | {check.get('right')} | {check.get('delta')} |"
            )
    else:
        lines.append(
            f"All {len(reconciliation.get('checks', []))} identities hold: every record "
            "the providers returned is accounted for at every stage boundary."
        )
    lines.append("")

    parity = summary.get("parity") or []
    if parity:
        lines.append("## Parity")
        lines.append("")
        for check in parity:
            mark = "ok" if check.get("passed") else "FAILED"
            lines.append(f"- [{mark}] {check.get('name')} - {check.get('detail', '')}")
        lines.append("")

    coverage = summary.get("title_coverage") or []
    if coverage:
        empty = [entry for entry in coverage if not entry.get("matched_records")]
        lines.append("## Title coverage")
        lines.append("")
        lines.append(f"- targeted titles: {len(coverage)}")
        lines.append(f"- titles with zero retrieved postings: {len(empty)}")
        if empty:
            preview = ", ".join(f"`{entry['title']}`" for entry in empty[:15])
            lines.append(f"- examples: {preview}{' ...' if len(empty) > 15 else ''}")
        lines.append("")

    notes = summary.get("notes") or []
    if notes:
        lines.append("## Notes")
        lines.append("")
        lines.extend(f"- {note}" for note in notes)
        lines.append("")

    lines.append(TYPICAL_WEEK_PROTOCOL)
    return "\n".join(lines) + "\n"


#: Assertive weekly claims. The protocol section legitimately discusses the
#: typical week, so a bare mention is fine; asserting a weekly figure is not.
_TYPICAL_WEEK_CLAIMS = (
    re.compile(r"\bin\s+a\s+typical\s+week\s*,?\s+(?:we|the|there)\b", re.I),
    re.compile(r"\bper\s+(?:a\s+)?typical\s+week\b", re.I),
    re.compile(r"\btypical\s+week\b[^.\n]{0,60}\bwe\s+(?:retrieve|capture|see|get|find)\b", re.I),
    re.compile(r"\d[\d,]*\s*(?:postings?|jobs?|results?)[^.\n]{0,30}\bper\s+week\b", re.I),
)


def assert_no_typical_week_claim(text: str) -> None:
    """Guard against the report ever *asserting* a weekly figure.

    A single run is a snapshot. The report may explain what measuring a typical
    week would require; it may not state one.
    """
    for pattern in _TYPICAL_WEEK_CLAIMS:
        match = pattern.search(text)
        if match:
            raise RuntimeError(
                f"report makes an unsupported typical-week claim: {match.group(0)!r}"
            )


def write_run_artifacts(
    artifact_root: str | Path,
    run_id: str,
    *,
    manifest: Mapping[str, Any],
    summary: Mapping[str, Any],
    source_metrics: Sequence[Mapping[str, Any]],
    query_metrics: Sequence[Mapping[str, Any]] = (),
    board_metrics: Sequence[Mapping[str, Any]] = (),
    discard_metrics: Sequence[Mapping[str, Any]] = (),
    parity: Sequence[Mapping[str, Any]] = (),
    request_ledger: Sequence[Mapping[str, Any]] = (),
    posting_lineage: Sequence[Mapping[str, Any]] = (),
    lane_failures: Sequence[Mapping[str, Any]] = (),
    preflight: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, int]:
    """Write every artifact for one run; return per-file byte counts.

    Each file is written independently so that a failure in one does not cost
    the others. ``run_status.json`` is written last and names exactly which
    artifacts reached disk -- the only honest way to report "artifacts
    successfully persisted" is to report it after persisting them.
    """
    directory = run_dir(artifact_root, run_id)
    directory.mkdir(parents=True, exist_ok=True)

    report = render_report(summary, manifest)
    assert_no_typical_week_claim(report)

    plan = [
        ("run_manifest.json", lambda: atomic_write_json(directory / "run_manifest.json", manifest)),
        ("coverage_summary.json", lambda: atomic_write_json(directory / "coverage_summary.json", summary)),
        ("source_metrics.json", lambda: atomic_write_json(directory / "source_metrics.json", list(source_metrics))),
        ("query_metrics.json", lambda: atomic_write_json(directory / "query_metrics.json", list(query_metrics))),
        ("board_metrics.json", lambda: atomic_write_json(directory / "board_metrics.json", list(board_metrics))),
        ("discard_metrics.json", lambda: atomic_write_json(directory / "discard_metrics.json", list(discard_metrics))),
        ("parity_report.json", lambda: atomic_write_json(directory / "parity_report.json", list(parity))),
        ("lane_failures.json", lambda: atomic_write_json(directory / "lane_failures.json", list(lane_failures))),
        ("preflight.json", lambda: atomic_write_json(directory / "preflight.json", list(preflight))),
        ("request_ledger.jsonl.gz", lambda: write_jsonl_gz(directory / "request_ledger.jsonl.gz", request_ledger)),
        ("posting_lineage.jsonl.gz", lambda: write_jsonl_gz(directory / "posting_lineage.jsonl.gz", posting_lineage)),
        ("coverage_summary.md", lambda: atomic_write_text(directory / "coverage_summary.md", report)),
    ]

    written: Dict[str, int] = {}
    unwritable: Dict[str, str] = {}
    for name, write in plan:
        try:
            written[name] = write()
        except OSError as exc:  # one bad file must not cost the whole run
            unwritable[name] = type(exc).__name__

    status_payload = {
        "run_id": run_id,
        "status": manifest.get("status", "incomplete"),
        "exit_reason": manifest.get("exit_reason", ""),
        "lanes_selected": list(manifest.get("lanes_selected") or []),
        "request_budget": dict(manifest.get("request_budget") or {}),
        "lane_failures": list(lane_failures),
        "artifacts_persisted": sorted(written),
        "artifact_bytes": dict(written),
        "artifacts_unwritable": unwritable,
    }
    try:
        written["run_status.json"] = atomic_write_json(
            directory / "run_status.json", status_payload
        )
    except OSError as exc:
        unwritable["run_status.json"] = type(exc).__name__

    written["_total_bytes"] = sum(value for key, value in written.items() if not key.startswith("_"))
    written["_unwritable"] = len(unwritable)
    return written
