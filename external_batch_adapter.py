"""Ingest an externally acquired Fantastic.jobs batch (Apify CSV) into the pipeline.

The Apify LinkedIn actor emits the SAME Fantastic.jobs record schema the live
Direct-API lane consumes, only flattened: every array/object element becomes its
own ``prefix/index`` column. This module reverses that flattening and then hands
each record to the canonical ``fantastic_jobs_adapter.map_record`` -- the exact
function the paid lane uses -- so no filtering, identity or normalization logic is
forked or re-implemented here.

The batch is already acquired and paid for, so this path issues ZERO provider
requests: no active-jb, no active-ats, no count endpoint.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

from fantastic_jobs_adapter import map_record

#: Source label for this lane. Distinct from the paid Fantastic lanes so the
#: yield ledger, artifacts and Airtable provenance can tell them apart, while
#: ``_fantastic_source``/``_fantastic_source_type`` still preserve the original
#: provider values ("linkedin"/"jobboard") from the row itself.
EXTERNAL_SOURCE = "external_apify_fantastic"

#: Apify writes a scalar column AND indexed columns for the same key (e.g.
#: ``ai_benefits`` plus ``ai_benefits/0``). When indexed columns carry data the
#: scalar is a redundant flattened rendering and must not shadow the real list.
_INDEX_RE = re.compile(r"^(?P<base>.+)/(?P<idx>\d+)$")

#: Columns whose flattened scalar is a JSON blob rather than a plain value.
_TRUE = {"true", "1", "yes", "t"}
_FALSE = {"false", "0", "no", "f", ""}


def _coerce(value: str) -> Any:
    """CSV is untyped text. Recover JSON scalars/containers, else keep the string.

    Only unambiguous forms are converted: a bare ``true``/``false`` and text that
    parses as a JSON object or array. Numbers are deliberately left as strings --
    the canonical mapper already coerces the numeric fields it uses, and blanket
    numeric coercion would corrupt zero-padded identifiers.
    """
    raw = value.strip()
    if not raw:
        return ""
    low = raw.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if raw[0] in "[{":
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return raw
    return raw


def unflatten_row(row: Dict[str, str]) -> Dict[str, Any]:
    """Rebuild provider-native arrays/objects from Apify's ``prefix/index`` columns.

    ``locations/0/city`` style nesting is rebuilt as a list of dicts. An indexed
    group always wins over a same-named scalar column, and index order is
    preserved (numeric, not lexicographic, so ``/10`` follows ``/9``).
    """
    scalars: Dict[str, Any] = {}
    indexed: Dict[str, Dict[int, Any]] = {}
    nested: Dict[str, Dict[int, Dict[str, Any]]] = {}

    for key, raw in row.items():
        if key is None:
            continue
        name = key.strip().lstrip("﻿")
        if not name:
            continue
        value = _coerce("" if raw is None else str(raw))
        match = _INDEX_RE.match(name)
        if match:
            base, idx = match.group("base"), int(match.group("idx"))
            if value != "":
                indexed.setdefault(base, {})[idx] = value
            continue
        # prefix/index/field  ->  list of objects
        parts = name.split("/")
        if len(parts) == 3 and parts[1].isdigit():
            if value != "":
                nested.setdefault(parts[0], {}).setdefault(int(parts[1]), {})[parts[2]] = value
            continue
        scalars[name] = value

    record: Dict[str, Any] = dict(scalars)
    for base, items in indexed.items():
        record[base] = [items[i] for i in sorted(items)]
    for base, items in nested.items():
        rebuilt = [items[i] for i in sorted(items)]
        # A nested group and an indexed group never coexist for the same base in
        # the actor's output; if they somehow do, the richer object form wins.
        record[base] = rebuilt
    return record


def iter_records(path: str) -> Iterator[Tuple[int, Dict[str, Any]]]:
    """Yield ``(row_number, provider_native_record)`` for every CSV row.

    ``utf-8-sig`` strips the actor's BOM. The field-size limit is raised because
    ``description_text`` routinely exceeds Python's default 128 KiB cell cap.
    """
    try:
        csv.field_size_limit(sys.maxsize)
    except OverflowError:  # 32-bit interpreters
        csv.field_size_limit(2 ** 31 - 1)
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for number, row in enumerate(csv.DictReader(handle), start=1):
            yield number, unflatten_row(row)


def normalize_batch(path: str, *, source_label: str = EXTERNAL_SOURCE,
                    classify: bool = True) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Normalize an external CSV batch into canonical jobs via the REAL mapper.

    Returns ``(jobs, stats)``. A malformed row fails closed (counted, skipped);
    it never aborts the batch. When ``classify`` is set the same
    ``multi_source_acquisition._classify`` step the paid Fantastic lane applies is
    run, so the RoleGate has a ``_matched_role`` target to verify.
    """
    from multi_source_acquisition import _classify

    jobs: List[Dict[str, Any]] = []
    rejected: Dict[str, int] = {}
    stats: Dict[str, Any] = {
        "raw_rows": 0, "mapped": 0, "row_errors": 0, "classified": 0,
        "classify_errors": 0, "rejected_by_reason": rejected,
    }
    for _number, record in iter_records(path):
        stats["raw_rows"] += 1
        try:
            job, reason = map_record(record, source_label)
        except Exception as exc:  # noqa: BLE001 - one bad row never stops the batch
            stats["row_errors"] += 1
            rejected[f"map_error:{type(exc).__name__}"] = rejected.get(
                f"map_error:{type(exc).__name__}", 0) + 1
            continue
        if job is None:
            rejected[reason or "unknown"] = rejected.get(reason or "unknown", 0) + 1
            continue
        job["_external_batch"] = Path(path).name
        stats["mapped"] += 1
        if classify:
            try:
                _classify(job)
                stats["classified"] += 1
            except Exception:  # noqa: BLE001 - classification never drops a job
                stats["classify_errors"] += 1
        jobs.append(job)
    return jobs, stats
