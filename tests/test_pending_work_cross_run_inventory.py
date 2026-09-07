import json

from orchestrator import pending_work as pw


def test_summary_counts_distinct_postings_and_preserves_physical_copies(tmp_path):
    a, b, c = [{"job_id": x} for x in "abc"]
    pw.record(tmp_path, "original", [a, b])
    pw.record(tmp_path, "recovery", [b, c])
    before = {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    summary = pw.summary(tmp_path)
    assert summary["pending_postings"] == 3
    assert summary["stored_posting_records"] == 4
    assert summary["duplicate_custody_records"] == 1
    loaded, info = pw.load(tmp_path)
    assert info["adopted"] == len(loaded) == summary["pending_postings"]
    assert {p.name: p.read_bytes() for p in tmp_path.glob("*.json")} == before


def test_artifact_adoption_skips_work_held_by_another_run_before_budget_slice(tmp_path):
    store = tmp_path / "pending_work"
    a, b, c = [{"job_id": x} for x in "abc"]
    pw.record(store, "original", [a, b])
    source = tmp_path / "run_artifacts/recovery/enrichment/postings.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"jobs": [a, b, c]}))
    result = pw.adopt_from_artifacts(tmp_path, store, limit=1)
    assert result["postings_imported"] == 1
    assert json.loads((store / "recovery.json").read_text())["jobs"] == [c]
    assert pw.summary(store)["pending_postings"] == 3
    assert pw.adopt_from_artifacts(tmp_path, store, limit=1)["postings_imported"] == 0
