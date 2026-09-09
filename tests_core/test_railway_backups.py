import copy
import pytest

from rebuild import backup_railway_volumes as backup


def inventory():
    return {"environment": {"id": backup.ENVIRONMENT, "projectId": backup.PROJECT,
        "volumeInstances": {"pageInfo": {"hasNextPage": False}, "edges": [
            {"node": {"id": "10000000-0000-4000-8000-00000000000" + str(i),
                      "volumeId": volume, "environmentId": backup.ENVIRONMENT,
                      "serviceId": service, "mountPath": mount,
                      "deletedAt": None, "isPendingDeletion": False}}
            for i, (volume, (service, mount, _)) in enumerate(backup.VOLUMES.items())]}}}


@pytest.mark.parametrize("field,value", [("environmentId", "wrong"), ("serviceId", "wrong"),
    ("mountPath", "/wrong"), ("deletedAt", "yesterday"), ("isPendingDeletion", True)])
def test_wrong_bindings_block_before_mutation(field, value):
    data = inventory()
    data["environment"]["volumeInstances"]["edges"][0]["node"][field] = value
    with pytest.raises(RuntimeError):
        backup.targets(data)


def test_incomplete_or_ambiguous_inventory():
    data = inventory()
    data["environment"]["volumeInstances"]["pageInfo"]["hasNextPage"] = True
    with pytest.raises(RuntimeError):
        backup.targets(data)
    data = inventory()
    data["environment"]["volumeInstances"]["edges"].append(copy.deepcopy(data["environment"]["volumeInstances"]["edges"][0]))
    with pytest.raises(RuntimeError):
        backup.targets(data)


@pytest.mark.parametrize("timeout", [False, True])
def test_one_request_even_on_timeout_and_repeat(tmp_path, timeout):
    volume, instance = next(iter(backup.targets(inventory()).items()))
    calls, rows = [], []
    def call(query):
        calls.append(query)
        if query == backup.QUERY:
            return inventory()
        if "mutation" in query:
            assert (tmp_path / (volume + ".json")).is_file()
            rows.append({"id": "backup1", "name": "TGTC-cierre-20260909-" + volume})
            if timeout:
                raise TimeoutError("SECRET_RAW_API_ERROR")
            return {"volumeInstanceBackupCreate": {"workflowId": "20000000-0000-4000-8000-000000000000"}}
        return {"volumeInstanceBackupList": rows.copy()}
    assert backup.request_one(volume, instance, tmp_path, call)["state"] == "backup_listed"
    assert backup.request_one(volume, instance, tmp_path, call)["state"] == "backup_listed"
    assert sum("mutation" in q for q in calls) == 1
    assert all("SECRET" not in p.read_text() for p in tmp_path.iterdir())


def test_uncertain_request_never_blindly_retried(tmp_path):
    volume, instance = next(iter(backup.targets(inventory()).items()))
    calls = []
    def call(query):
        calls.append(query)
        if query == backup.QUERY:
            return inventory()
        if "mutation" in query:
            raise TimeoutError()
        return {"volumeInstanceBackupList": []}
    for _ in range(2):
        assert backup.request_one(volume, instance, tmp_path, call)["state"] == "pending_or_ambiguous"
    assert sum("mutation" in q for q in calls) == 1


def test_binding_change_blocks_mutation_and_keeps_intent(tmp_path):
    volume, instance = next(iter(backup.targets(inventory()).items()))
    def call(query):
        assert "mutation" not in query
        if query == backup.QUERY:
            data = inventory()
            data["environment"]["volumeInstances"]["edges"][0]["node"]["id"] = "30000000-0000-4000-8000-000000000000"
            return data
        return {"volumeInstanceBackupList": []}
    with pytest.raises(RuntimeError, match="volume_binding_changed"):
        backup.request_one(volume, instance, tmp_path, call)
    assert (tmp_path / (volume + ".json")).is_file()


def test_existing_named_backup_does_not_create_another(tmp_path):
    volume, instance = next(iter(backup.targets(inventory()).items()))
    def call(query):
        assert "mutation" not in query
        return {"volumeInstanceBackupList": [{"id": "existing", "name": "TGTC-cierre-20260909-" + volume}]}
    result = backup.request_one(volume, instance, tmp_path, call)
    assert result["state"] == "backup_listed"
    assert result["restore_tested"] is False
