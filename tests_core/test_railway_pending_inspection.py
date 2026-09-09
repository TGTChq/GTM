"""Synthetic Railway responses only; no Railway session or network is used."""

import json
from types import SimpleNamespace

import pytest

from rebuild import inspect_railway_pending as probe


def response():
    sid = next(iter(probe.SERVICES))
    current = {"services": {sid: {"variables": {
        "APOLLO_API_KEY": {"value": "private-current-value", "description": "unchanged metadata"},
        "SEALED": {"value": "********", "isSealed": True},
    }, "deploy": {"cronSchedule": "0 3 * * *"}}}}
    patch = {"services": {sid: {"variables": {
        "APOLLO_API_KEY": {"value": "private-proposed-value"},
        "SEALED": {"value": "********", "isSealed": True},
    }, "deploy": {"cronSchedule": "0 3 * * *"}}},
        "sharedVariables": {"EXTRA": {"value": "private-shared-value"}}}
    return {"data": {
        "environment": {"id": probe.ENVIRONMENT, "projectId": probe.PROJECT, "config": current},
        "environmentStagedChanges": {"id": probe.OBSERVED_PATCH, "environmentId": probe.ENVIRONMENT,
                                     "status": "STAGED", "patch": patch},
    }}


def test_comparison_reports_names_without_secret_values():
    result = probe.summarize(response())
    assert result["inspected_entry_count"] == 4
    assert result["by_scope"] == {"GTM": 3, "environment": 1}
    assert result["by_comparison"] == {
        "different_from_current": 1, "same_as_current": 1,
        "value_not_comparable": 1, "absent_in_current": 1,
    }
    encoded = json.dumps(result)
    assert "APOLLO_API_KEY" in encoded
    assert "private-" not in encoded and "********" not in encoded


def test_partial_variable_patch_does_not_turn_omitted_metadata_into_change():
    raw = response()
    sid = next(iter(probe.SERVICES))
    raw["data"]["environmentStagedChanges"]["patch"]["services"][sid]["variables"]["APOLLO_API_KEY"]["value"] = "private-current-value"
    result = probe.summarize(raw)
    assert result["by_comparison"]["same_as_current"] == 2


@pytest.mark.parametrize("value", [None, "********", "[REDACTED]"])
def test_hidden_values_are_never_reported_as_equal(value):
    assert probe._variable_comparison({"value": value}, {"value": value}) == "value_not_comparable"


def test_wrong_environment_is_rejected():
    raw = response()
    raw["data"]["environment"]["id"] = "some-other-environment"
    with pytest.raises(ValueError, match="unexpected_project_or_environment"):
        probe.summarize(raw)


def test_empty_patch_and_replaced_patch_are_reported_without_inventing_changes():
    raw = response()
    staged = raw["data"]["environmentStagedChanges"]
    staged["patch"] = {}
    staged["id"] = "00000000-0000-4000-8000-000000000000"
    result = probe.summarize(raw)
    assert result["inspected_entry_count"] == 0
    assert result["matches_connector_patch_id"] is False
    assert result["interpretation"] == "masked_view_not_requested"
    assert result["safe_to_apply"] is False


def test_empty_decrypted_patch_does_not_hide_masked_variable_entries():
    raw = response()
    staged = raw["data"]["environmentStagedChanges"]
    staged["maskedPatch"] = staged["patch"]
    staged["patch"] = {}
    result = probe.summarize(raw)
    assert result["inspected_entry_count"] == 0
    assert result["masked_view"]["inspected_entry_count"] == 4
    assert result["interpretation"] == "entries_only_in_masked_view"
    assert "by_comparison" not in result["masked_view"]
    assert "private-" not in json.dumps(result)
    assert result["safe_to_apply"] is False


def test_zero_entries_preserves_evidence_of_empty_nested_containers():
    raw = response()
    staged = raw["data"]["environmentStagedChanges"]
    staged["patch"] = {"services": {next(iter(probe.SERVICES)): {"variables": {}}}}
    staged["maskedPatch"] = {}
    result = probe.summarize(raw)
    assert result["interpretation"] == "no_entries_in_either_returned_view"
    assert result["patch_shape"]["objects"] == 4
    assert result["patch_shape"]["empty_objects"] == 1
    assert result["masked_view"]["shape"]["objects"] == 1
    assert result["safe_to_apply"] is False


def test_null_patch_is_not_silently_treated_as_no_changes():
    raw = response()
    raw["data"]["environmentStagedChanges"]["maskedPatch"] = None
    with pytest.raises(ValueError, match="unsupported_masked_patch_shape"):
        probe.summarize(raw)


def test_query_requests_both_representations_in_same_read():
    assert "patch(decryptVariables: true)" in probe.QUERY
    assert "maskedPatch: patch(decryptVariables: false)" in probe.QUERY


def test_cli_uses_only_fixed_read_query_and_persists_only_redacted_result(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(probe.shutil, "which", lambda name: "railway.exe")

    def fake_run(command, **kwargs):
        assert command == ["railway.exe", "api", "--file", "-", "--compact"]
        assert kwargs["input"] == probe.QUERY
        assert probe.QUERY.strip().startswith("query ")
        assert "mutation" not in probe.QUERY.lower()
        assert kwargs["capture_output"] is True
        assert kwargs["env"]["RAILWAY_NO_AUTO_UPDATE"] == "1"
        assert kwargs["timeout"] == 45
        return SimpleNamespace(returncode=0, stdout=json.dumps(response()), stderr="")

    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    assert probe.main() == 0
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    assert "private-" not in files[0].read_text()
    assert "private-" not in str(capsys.readouterr())


def test_failed_cli_does_not_echo_raw_response_or_save_it(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(probe.shutil, "which", lambda name: "railway.exe")
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=1, stdout=json.dumps(response()), stderr="private-error-secret"))
    assert probe.main() == 1
    assert "private-" not in str(capsys.readouterr())
    assert list(tmp_path.iterdir()) == []


def test_protected_cwd_saves_under_user_home_without_repeating_api(monkeypatch, tmp_path, capsys):
    protected = tmp_path / "System32"
    protected.mkdir()
    user_home = tmp_path / "user"
    user_home.mkdir()
    monkeypatch.chdir(protected)
    monkeypatch.setattr(probe.Path, "home", classmethod(lambda cls: user_home))
    real_open = probe.Path.open

    def protected_open(path, *args, **kwargs):
        if path.parent == protected:
            raise PermissionError("private-filesystem-message")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(probe.Path, "open", protected_open)
    reads = []

    def read_once():
        reads.append(True)
        return probe.summarize(response())

    monkeypatch.setattr(probe, "run_query", read_once)
    assert probe.main() == 0
    assert reads == [True]
    files = list((user_home / "TGTC-diagnostics").glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["read_only"] is True
    printed = str(capsys.readouterr())
    assert str(files[0]) in printed
    assert "private-" not in printed
