"""Read Railway's pending patch through the user's CLI; output no config values.

Uses only a fixed GraphQL QUERY, via the installed CLI and its existing login.
No token files are read, no environment is linked, and no remote state is changed.
Values are requested for an in-memory comparison; raw responses are never saved
or printed. Sealed/masked values remain unknown. This is not a pre-incident audit.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

PROJECT = "898f2e3a-1c1e-4b00-b9a6-686cf0432282"
ENVIRONMENT = "bae427bd-64a6-4f4e-8f56-fbd406985434"
OBSERVED_PATCH = "2f6879e3-ccdb-40f1-87c8-719d79cd8dab"
SERVICES = {
    "3a41d0d7-cd66-4f53-baa6-886266ddbbed": "GTM",
    "d6b2e1c1-c931-4803-8555-91a8216124bc": "GTM Approved Sync",
    "74ffcf60-dfbf-47cd-a623-b9aee69ad16d": "GTM Core Acceptance",
    "f3e843e3-16f9-495e-a63b-574a653f3016": "Postgres Core",
}
QUERY = '''query InspectTGTCStagedReadOnly {
  environment(id: "%s", projectId: "%s") {
    id projectId name config(decryptVariables: true)
  }
  environmentStagedChanges(environmentId: "%s") {
    id environmentId status createdAt updatedAt
    patch(decryptVariables: true)
    maskedPatch: patch(decryptVariables: false)
  }
}''' % (ENVIRONMENT, PROJECT, ENVIRONMENT)
MISSING = object()


def _masked(value):
    if not isinstance(value, str):
        return False
    return bool(re.fullmatch(r"\*+|<redacted>|\[redacted\]|<sealed>|\[sealed\]",
                             value.strip(), re.IGNORECASE))


def _comparison(current, proposed):
    """Compare only supplied patch keys, not defaults absent from a partial patch."""
    if current is MISSING:
        return "absent_in_current"
    if proposed is None:
        return "proposed_null"
    if isinstance(proposed, dict):
        if not isinstance(current, dict):
            return "different_from_current"
        statuses = [_comparison(current.get(k, MISSING), v) for k, v in proposed.items()]
        return "same_as_current" if all(s == "same_as_current" for s in statuses) else "different_from_current"
    return "same_as_current" if type(current) is type(proposed) and current == proposed else "different_from_current"


def _variable_comparison(current, proposed):
    if proposed is None:
        return "proposed_null"
    # A visible name, an omitted value, or matching masks cannot prove equality.
    for item in (current, proposed):
        if item is MISSING:
            continue
        if not isinstance(item, dict) or item.get("isSealed"):
            return "value_not_comparable"
        if "value" not in item or item["value"] is None or _masked(item["value"]):
            return "value_not_comparable"
    return _comparison(current, proposed)


def _entries(current, patch, path=()):
    if isinstance(patch, dict):
        for key, proposed in sorted(patch.items()):
            previous = current.get(key, MISSING) if isinstance(current, dict) else MISSING
            child_path = path + (key,)
            if path and path[-1] in ("variables", "sharedVariables"):
                yield child_path, _variable_comparison(previous, proposed), "variable"
            elif isinstance(proposed, dict):
                yield from _entries(previous, proposed, child_path)
            else:
                yield child_path, _comparison(previous, proposed), "configuration_field"


def _shape(value):
    """Count containers as well as leaves; never serialize a config value."""
    counts = Counter(objects=0, empty_objects=0, arrays=0, scalars=0, nulls=0)

    def visit(node):
        if isinstance(node, dict):
            counts["objects"] += 1
            counts["empty_objects"] += int(not node)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            counts["arrays"] += 1
            for child in node:
                visit(child)
        else:
            counts["nulls" if node is None else "scalars"] += 1

    visit(value)
    return dict(counts)


def _view(current, patch):
    entries = []
    for path, comparison, kind in _entries(current, patch):
        scope = SERVICES.get(path[1], "unknown_service") if len(path) > 1 and path[0] == "services" else "environment"
        entries.append({"scope": scope, "path": list(path), "kind": kind, "comparison": comparison})
    return {"shape": _shape(patch), "inspected_entry_count": len(entries),
            "by_scope": dict(Counter(x["scope"] for x in entries)),
            "by_comparison": dict(Counter(x["comparison"] for x in entries)),
            "entries": entries}


def summarize(response):
    if not isinstance(response, dict) or response.get("errors"):
        raise ValueError("api_response_error")
    data = response.get("data", {})
    env = data.get("environment", {})
    staged = data.get("environmentStagedChanges", {})
    if (env.get("id") != ENVIRONMENT or env.get("projectId") != PROJECT
            or staged.get("environmentId") != ENVIRONMENT):
        raise ValueError("unexpected_project_or_environment")
    current, patch = env.get("config"), staged.get("patch")
    if not isinstance(current, dict) or not isinstance(patch, dict):
        raise ValueError("unsupported_config_shape")
    decrypted = _view(current, patch)
    # The old report did not request this alias. Preserve its readable format,
    # but label that missing evidence instead of treating it as an empty patch.
    masked = staged.get("maskedPatch", MISSING)
    if masked is not MISSING and not isinstance(masked, dict):
        raise ValueError("unsupported_masked_patch_shape")
    masked_view = None if masked is MISSING else _view(MISSING, masked)
    # A masked value cannot establish equality or deletion semantics. Report
    # structural counts only for this representation, never comparisons.
    if masked_view is not None:
        masked_view.pop("by_comparison")
        masked_view.pop("entries")
    if masked_view is None:
        interpretation = "masked_view_not_requested"
    elif not decrypted["inspected_entry_count"] and not masked_view["inspected_entry_count"]:
        interpretation = "no_entries_in_either_returned_view"
    elif not decrypted["inspected_entry_count"]:
        interpretation = "entries_only_in_masked_view"
    else:
        interpretation = "decrypted_patch_has_entries"
    patch_id = str(staged.get("id", ""))
    if patch_id and not re.fullmatch(r"[0-9a-fA-F-]{36}", patch_id):
        raise ValueError("unexpected_patch_id")
    status = staged.get("status")
    if status not in ("STAGED", "APPLYING", "APPLIED", "FAILED", "DISCARDED"):
        status = "OTHER"
    return {
        "read_only": True, "project_id": PROJECT, "environment_id": ENVIRONMENT,
        "patch_id": patch_id, "patch_status": status,
        "matches_connector_patch_id": patch_id == OBSERVED_PATCH,
        "inspected_entry_count": decrypted["inspected_entry_count"],
        "count_definition": "one entry per variable, one per other supplied scalar/list/null field; not Railway's undocumented changeCount",
        "by_scope": decrypted["by_scope"],
        "by_comparison": decrypted["by_comparison"],
        "entries": decrypted["entries"],
        "patch_shape": decrypted["shape"],
        "masked_view": masked_view,
        "interpretation": interpretation,
        "safe_to_apply": False,
        "current_services_returned": sorted(SERVICES[sid] for sid in SERVICES
                                            if sid in current.get("services", {})),
        "limits": [
            "No values, passwords, hashes or raw API errors included.",
            "Equality describes the current returned representation, not historical pre-incident values or resolved reference semantics.",
            "Sealed, masked and omitted values remain unknown; no deployment or acceptance is authorized by this result.",
            "Current config and patch reads are not a locked snapshot. Recheck before any separately authorized mutation.",
            "STAGED alone can describe an empty placeholder. Zero enumerated entries do not authorize applying or discarding it.",
        ],
    }


def run_query():
    railway = shutil.which("railway")
    if not railway:
        raise RuntimeError("railway_cli_not_found")
    env = dict(os.environ)
    env["RAILWAY_NO_AUTO_UPDATE"] = "1"
    result = subprocess.run([railway, "api", "--file", "-", "--compact"],
                            input=QUERY, text=True, encoding="utf-8", errors="replace",
                            capture_output=True, timeout=45, env=env, check=False)
    if result.returncode:
        # Never forward raw CLI stdout/stderr: GraphQL diagnostics may quote values.
        if "unrecognized subcommand" in result.stderr.lower() or "unexpected argument" in result.stderr.lower():
            raise RuntimeError("installed_cli_has_no_supported_api_command")
        raise RuntimeError("cli_query_failed_check_login_and_project_access")
    return summarize(json.loads(result.stdout))


def main():
    try:
        report = run_query()
        report["observed_at_utc"] = datetime.now(timezone.utc).isoformat()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = Path.cwd() / f"railway-pending-inspection-{stamp}.json"
        with destination.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print(f"Diagnostico de solo lectura guardado: {destination.name}")
        print(f"Parche: {report['patch_id']} ({report['patch_status']})")
        print("Entradas por servicio: " + json.dumps(report["by_scope"], ensure_ascii=False))
        print("Comparacion: " + json.dumps(report["by_comparison"], ensure_ascii=False))
        print("Resultado de ambas lecturas: " + report["interpretation"])
        masked = report["masked_view"]
        print("Entradas descifradas / ocultas: " + str(report["inspected_entry_count"]) + " / "
              + (str(masked["inspected_entry_count"]) if masked is not None else "no consultada"))
        print("Servicios visibles en configuracion actual: " + ", ".join(report["current_services_returned"]))
        print("Forma del parche descifrado: " + json.dumps(report["patch_shape"]))
        print("Comparte el archivo JSON generado. No contiene los valores de las variables.")
        return 0
    except Exception as exc:
        known = {"railway_cli_not_found", "installed_cli_has_no_supported_api_command",
                 "cli_query_failed_check_login_and_project_access", "api_response_error",
                 "unexpected_project_or_environment", "unsupported_config_shape", "unsupported_masked_patch_shape", "unexpected_patch_id"}
        code = str(exc) if type(exc) in (RuntimeError, ValueError) and str(exc) in known else type(exc).__name__
        print("No se pudo completar la lectura: " + code, file=sys.stderr)
        print("No se aplicaron cambios. No compartas tokens ni salidas crudas de variables.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
