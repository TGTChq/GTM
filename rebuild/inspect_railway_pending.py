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
    id environmentId status createdAt updatedAt patch(decryptVariables: true)
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
    entries = []
    for path, comparison, kind in _entries(current, patch):
        scope = SERVICES.get(path[1], path[1]) if len(path) > 1 and path[0] == "services" else "environment"
        entries.append({"scope": scope, "path": list(path), "kind": kind, "comparison": comparison})
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
        "inspected_entry_count": len(entries),
        "count_definition": "one entry per variable, one per other supplied scalar/list/null field; not Railway's undocumented changeCount",
        "by_scope": dict(Counter(x["scope"] for x in entries)),
        "by_comparison": dict(Counter(x["comparison"] for x in entries)),
        "entries": entries,
        "limits": [
            "No values, passwords, hashes or raw API errors included.",
            "Equality describes the current returned representation, not historical pre-incident values or resolved reference semantics.",
            "Sealed, masked and omitted values remain unknown; no deployment or acceptance is authorized by this result.",
            "Current config and patch reads are not a locked snapshot. Recheck before any separately authorized mutation.",
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
        print("Comparte el archivo JSON generado. No contiene los valores de las variables.")
        return 0
    except Exception as exc:
        known = {"railway_cli_not_found", "installed_cli_has_no_supported_api_command",
                 "cli_query_failed_check_login_and_project_access", "api_response_error",
                 "unexpected_project_or_environment", "unsupported_config_shape", "unexpected_patch_id"}
        code = str(exc) if type(exc) in (RuntimeError, ValueError) and str(exc) in known else type(exc).__name__
        print("No se pudo completar la lectura: " + code, file=sys.stderr)
        print("No se aplicaron cambios. No compartas tokens ni salidas crudas de variables.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
