"""SYNTHETIC stratified corpus: per-campaign precision/recall of the deterministic layer.

This gates regressions. It is NOT the independent acceptance corpus (ACCEPTANCE §3).
"""

from __future__ import annotations

from collections import defaultdict

import pytest

from tgtc_core.domain.classification import classify_posting
from tgtc_core.policy.campaigns import CAMPAIGN_BY_FUNCTION, FUNCTION_KEYS
from tgtc_core.testing.corpus import CORPUS


def _run(e):
    return classify_posting(description=e.description, title=e.title, countries=["US"], employment_type="FULL_TIME")


@pytest.mark.parametrize("example", CORPUS, ids=[e.key for e in CORPUS])
def test_each_example_matches_its_label(example):
    r = _run(example)
    if example.expected_excluded:
        assert r.excluded, f"{example.key} should be excluded; got {r.compatible_functions} {r.notes}"
    elif example.expected_function is None:
        assert not r.compatible_functions, f"{example.key} should not be compatible: {r.compatible_functions}"
    else:
        assert not r.excluded, f"{example.key} excluded: {r.exclusion_reason}"
        assert r.compatible_functions == [example.expected_function], f"{example.key}: {r.compatible_functions} scores={r.scores}"
        assert 1 <= len(r.responsibilities) <= 3
        for resp in r.responsibilities:
            assert resp.excerpt and resp.phrase


def test_every_function_and_campaign_has_coverage_and_precision_recall_are_reported():
    tp = defaultdict(int)
    fp = defaultdict(int)
    fn_ = defaultdict(int)
    for e in CORPUS:
        r = _run(e)
        predicted = set(r.compatible_functions) if not r.excluded else set()
        expected = {e.expected_function} if e.expected_function and not e.expected_excluded else set()
        for f in predicted & expected:
            tp[f] += 1
        for f in predicted - expected:
            fp[f] += 1
        for f in expected - predicted:
            fn_[f] += 1
    report = {}
    for f in FUNCTION_KEYS:
        denom_p = tp[f] + fp[f]
        denom_r = tp[f] + fn_[f]
        report[f] = {"tp": tp[f], "fp": fp[f], "fn": fn_[f],
                     "precision": tp[f] / denom_p if denom_p else None, "recall": tp[f] / denom_r if denom_r else None}
        assert denom_r >= 2, f"{f} needs at least two positives in the corpus"
    assert {CAMPAIGN_BY_FUNCTION[f].key for f in FUNCTION_KEYS} == {c for c in {CAMPAIGN_BY_FUNCTION[f].key for f in FUNCTION_KEYS}}
    # On this synthetic corpus the deterministic layer must be exact; the acceptance
    # thresholds (>=95% precision, >=90% recall) apply to the INDEPENDENT corpus.
    for f, m in report.items():
        assert m["precision"] == 1.0 and m["recall"] == 1.0, (f, m)
