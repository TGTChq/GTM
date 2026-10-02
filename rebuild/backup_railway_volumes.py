"""Request one named snapshot per pinned production volume using the owner CLI.

No config/variables, deploys, restores, deletes or global patch commits. Receipts
are written before mutations. An uncertain request is reconciled, never retried.
Backup visibility is recorded separately from restore verification (not done).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

PROJECT = "898f2e3a-1c1e-4b00-b9a6-686cf0432282"
ENVIRONMENT = "bae427bd-64a6-4f4e-8f56-fbd406985434"
VOLUMES = {
    "ff83ecc1-750d-4781-bf14-5e29a3678de4":
        ("3a41d0d7-cd66-4f53-baa6-886266ddbbed", "/app/data/state", "GTM"),
    "d01e67f1-28a3-41ea-a7e6-dcda1b0dc480":
        ("f3e843e3-16f9-495e-a63b-574a653f3016", "/var/lib/postgresql/data", "Postgres Core"),
}
QUERY = '''query TGTCBackupTargets {
  environment(id: "%s", projectId: "%s") {
    id projectId
    volumeInstances(first: 100) {
      pageInfo { hasNextPage }
      edges { node {
        id volumeId environmentId serviceId mountPath deletedAt isPendingDeletion
      } }
    }
  }
}''' % (ENVIRONMENT, PROJECT)


def api(query):
    cli = shutil.which("railway")
    if not cli:
        raise RuntimeError("railway_cli_not_found")
    result = subprocess.run(
        [cli, "api", "--file", "-", "--compact"], input=query,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=45, env={**os.environ, "RAILWAY_NO_AUTO_UPDATE": "1"},
    )
    if result.returncode:
        raise RuntimeError("railway_api_failed_no_automatic_retry")
    body = json.loads(result.stdout)
    if not isinstance(body, dict) or body.get("errors"):
        raise RuntimeError("railway_api_response_error")
    data = body.get("data", body)
    if not isinstance(data, dict):
        raise RuntimeError("railway_api_shape_error")
    return data


def targets(data):
    env = data["environment"]
    if env["id"] != ENVIRONMENT or env["projectId"] != PROJECT:
        raise RuntimeError("unexpected_project_or_environment")
    connection = env["volumeInstances"]
    if connection["pageInfo"]["hasNextPage"] is not False:
        raise RuntimeError("incomplete_volume_inventory")
    selected = {}
    for edge in connection["edges"]:
        node = edge["node"]
        volume_id = node["volumeId"]
        if volume_id not in VOLUMES:
            continue
        service, mount, _ = VOLUMES[volume_id]
        if (node["environmentId"] != ENVIRONMENT or node["serviceId"] != service
                or node["mountPath"] != mount or node["deletedAt"] is not None
                or node["isPendingDeletion"] is not False or volume_id in selected):
            raise RuntimeError("unexpected_volume_binding")
        selected[volume_id] = str(uuid.UUID(node["id"]))
    if set(selected) != set(VOLUMES) or len(set(selected.values())) != len(VOLUMES):
        raise RuntimeError("missing_or_ambiguous_volume_instance")
    return selected


def listed(instance, call):
    query = '''query TGTCBackupList {
      volumeInstanceBackupList(volumeInstanceId: "%s") {
        id name createdAt expiresAt usedMB referencedMB volumeInstanceSizeMB
      }
    }''' % instance
    rows = call(query)["volumeInstanceBackupList"]
    if not isinstance(rows, list):
        raise RuntimeError("unexpected_backup_list")
    return rows


def write_new(path, data):
    # Exclusive creation is also the cross-process duplicate-request guard.
    with path.open("x", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())


def request_one(volume, instance, folder, call=api):
    receipt = folder / (volume + ".json")
    scope = {"project_id": PROJECT, "environment_id": ENVIRONMENT,
             "volume_id": volume, "volume_instance_id": instance}
    label = "TGTC-cierre-20260909-" + volume
    before = listed(instance, call)
    record = {**scope, "name": label, "state": "request_may_be_in_flight",
              "requested_at_utc": datetime.now(timezone.utc).isoformat(),
              "restore_tested": False}
    matching = [row for row in before if row.get("name") == label]
    owned = False
    if not receipt.exists():
        if matching:
            record["state"] = "existing_named_backup"
        try:
            write_new(receipt, record)
            owned = not matching
        except FileExistsError:
            pass  # Another process owns the request. Only read from now on.
    saved = json.loads(receipt.read_text(encoding="utf-8"))
    if any(saved.get(k) != v for k, v in scope.items()) or saved.get("name") != label:
        raise RuntimeError("receipt_scope_mismatch")
    if owned:
        # Revalidate BOTH bindings immediately before each authorized mutation.
        if targets(call(QUERY)).get(volume) != instance:
            raise RuntimeError("volume_binding_changed")
        try:
            response = call('''mutation TGTCBackupCreate {
              volumeInstanceBackupCreate(volumeInstanceId: "%s", name: "%s") {
                workflowId
              }
            }''' % (instance, label))
            workflow = response["volumeInstanceBackupCreate"]["workflowId"]
            # Validate the only response identifier before writing it.
            if workflow is not None:
                workflow = str(uuid.UUID(workflow))
            write_new(folder / (volume + ".request.json"), {**scope, "workflow_id": workflow})
        except Exception:
            # Timeout/error may have happened AFTER the remote request succeeded.
            # Keep the durable intent receipt; never auto-resubmit the mutation.
            print(VOLUMES[volume][2] + ": solicitud incierta; solo se consultara su estado.", flush=True)
    after = listed(instance, call)
    matching = [row for row in after if row.get("name") == label]
    state = "backup_listed" if len(matching) == 1 else "pending_or_ambiguous"
    allowed = ("id", "createdAt", "expiresAt", "usedMB", "referencedMB", "volumeInstanceSizeMB")
    result = {**scope, "name": label, "state": state, "restore_tested": False,
              "observed_at_utc": datetime.now(timezone.utc).isoformat(),
              "backups": [{k: row.get(k) for k in allowed} for row in matching]}
    path = folder / (volume + ".observed-" + uuid.uuid4().hex + ".json")
    write_new(path, result)
    print(VOLUMES[volume][2] + ": " + state + "; comprobante " + str(path), flush=True)
    return result


def main():
    folder = Path.home() / "TGTC-backups" / "cierre-20260909"
    folder.mkdir(parents=True, exist_ok=True)
    selected = targets(api(QUERY))  # Validate all targets before any write.
    results = []
    for volume, instance in selected.items():
        try:
            results.append(request_one(volume, instance, folder))
        except Exception as exc:
            print(VOLUMES[volume][2] + ": pendiente (" + type(exc).__name__ + ").", flush=True)
            results.append({"state": "pending_or_ambiguous"})
    print("No se probaron restauraciones. Conserva los comprobantes; repetir solo consulta solicitudes previas.")
    return 0 if all(r["state"] == "backup_listed" for r in results) else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # CLI errors may contain sensitive material. Never echo their payload.
        print("Respaldo pendiente: " + type(exc).__name__ + "; conserva TGTC-backups y comparte los comprobantes.")
        raise SystemExit(1)
