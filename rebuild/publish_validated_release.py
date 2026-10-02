"""Validate an isolated release checkout, then publish only after Linux CI passes.

Uses the user's normal Git access. No Railway, SQL, provider or delivery commands.
The release manifest/bundle accompany this script in the generated package.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


def run(args, *, cwd=None, capture=False):
    result = subprocess.run([str(a) for a in args], cwd=cwd, check=False,
                            stdout=subprocess.PIPE if capture else None,
                            encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"El comando {Path(str(args[0])).name} termino con codigo {result.returncode}.")
    return (result.stdout or "").strip()


def validate_junit(path: Path, *, minimum: int, legacy: bool, windows: bool):
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    count = sum(int(s.get("tests", 0)) for s in suites)
    cases = list(root.iter("testcase"))
    # pytest subtests can inflate the tests attribute without adding testcases.
    # Require the full base suite too; never let subtests hide missing test files.
    if (count < minimum or len(cases) < minimum
            or any(int(s.get(k, 0)) for s in suites for k in ("failures", "errors"))
            or any(c.find("failure") is not None or c.find("error") is not None for c in cases)):
        raise RuntimeError(f"Pruebas insuficientes o con fallos: {path.name}; resultados={count}.")
    omissions = {"posix": 0, "symlink": 0}
    for case in cases:
        skip = case.find("skipped")
        if skip is None:
            continue
        reason = (skip.get("message", "") + " " + (skip.text or "")).lower()
        name = case.get("classname", "")
        if legacy and windows and "test_gtm_start_command_contract" in name and "needs a posix sh" in reason:
            omissions["posix"] += 1
        elif legacy and windows and "test_retrieval_measurement_artifacts_isolation" in name and "symlink creation requires privileges" in reason:
            omissions["symlink"] += 1
        else:
            raise RuntimeError(f"Omision no admitida en {name}.")
    if omissions["posix"] > 9 or omissions["symlink"] > 1:
        raise RuntimeError("Mas omisiones de plataforma que las revisadas.")
    if sum(int(s.get("skipped", 0)) for s in suites) != sum(omissions.values()):
        raise RuntimeError("El resumen de omisiones no coincide con los casos revisados.")
    return {"results": count, "test_cases": len(cases), "platform_skips": sum(omissions.values())}


def api(path):
    req = Request("https://api.github.com/repos/TGTChq/GTM/" + path,
                  headers={"Accept": "application/vnd.github+json", "User-Agent": "TGTC-release-validator"})
    with urlopen(req, timeout=30) as response:
        return json.load(response)


def select_run(data, sha):
    matches = [r for r in data.get("workflow_runs", [])
               if r.get("head_sha") == sha and r.get("path") == ".github/workflows/ci.yml"
               and r.get("event") == "push"]
    return max(matches, key=lambda r: r["id"]) if matches else None


def validate_jobs(jobs):
    by_name = {j["name"]: j for j in jobs}
    if any(by_name.get(name, {}).get("conclusion") != "success" for name in ("test", "core")):
        raise RuntimeError("CI no acredita ambos jobs completos: test y core.")
    required = {
        "test": {"Offline test suite", "Integrity manifest", "Confirm no credential reached the job"},
        "core": {"Core integrated suite (embedded PostgreSQL, simulated providers)",
                 "Build isolated core image and check zero-network startup",
                 "Legacy consumer compatibility (required)", "Confirm no credential reached the job"},
    }
    for name, steps in required.items():
        actual = {s["name"]: s.get("conclusion") for s in by_name[name].get("steps", [])}
        if any(actual.get(step) != "success" for step in steps):
            raise RuntimeError(f"Falta una puerta obligatoria de CI en {name}.")


def wait_ci(sha, timeout_minutes=25):
    deadline = time.monotonic() + timeout_minutes * 60
    last = None
    while time.monotonic() < deadline:
        selected = select_run(api(f"actions/runs?head_sha={sha}&event=push&per_page=100"), sha)
        status = (selected or {}).get("status", "esperando inicio")
        if status != last:
            print("CI Linux:", status, flush=True)
            last = status
        if selected and status == "completed":
            url = selected["html_url"]
            if selected.get("conclusion") != "success":
                raise RuntimeError(f"CI fallo: {url}. feat/rebuild-core no se publica.")
            validate_jobs(api(f"actions/runs/{selected['id']}/jobs?per_page=100").get("jobs", []))
            return url
        time.sleep(45)
    raise RuntimeError("CI no termino en 25 minutos. feat/rebuild-core no se publica; se puede repetir este script.")


def self_test():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp, "results.xml")
        p.write_text('<testsuites><testsuite tests="2" failures="0" errors="0"><testcase/><testcase/></testsuite></testsuites>')
        assert validate_junit(p, minimum=2, legacy=False, windows=False)["results"] == 2
        for xml in ('<testsuite tests="1"/>', '<testsuite tests="2" errors="1"/>',
                    '<testsuite tests="2000"><testcase/></testsuite>',
                    '<testsuite tests="2"><testcase><failure/></testcase><testcase/></testsuite>',
                    '<testsuite tests="2"><testcase><skipped message="database unavailable"/></testcase></testsuite>'):
            p.write_text(xml)
            try:
                validate_junit(p, minimum=2, legacy=False, windows=True)
            except RuntimeError:
                pass
            else:
                raise AssertionError("Gate accepted invalid tests")
    assert select_run({"workflow_runs": [{"head_sha": "other", "path": ".github/workflows/ci.yml", "event": "push", "id": 1}]}, "target") is None
    try:
        validate_jobs([{"name": "core", "conclusion": "success", "steps": []}])
    except RuntimeError:
        pass
    else:
        raise AssertionError("Gate accepted partial CI")
    print("Autoprueba OK: no Git remoto, proveedores, Railway ni SQL.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo")
    parser.add_argument("--package", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--python", dest="python_path", type=Path)
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    if not args.repo:
        parser.error("falta --repo")
    repo = Path(args.repo).resolve()
    if run(["git", "-C", repo, "rev-parse", "--is-inside-work-tree"], capture=True) != "true":
        raise RuntimeError("La ruta no es un checkout Git.")
    package = args.package.resolve()
    manifest = json.loads((package / "release.json").read_text(encoding="utf-8"))
    target, base, branch = (manifest[k] for k in ("target", "base", "branch"))
    bundle = package / "release.bundle"
    if hashlib.sha256(bundle.read_bytes()).hexdigest() != manifest["bundle_sha256"]:
        raise RuntimeError("El paquete no coincide con su hash.")
    origin = run(["git", "-C", repo, "remote", "get-url", "origin"], capture=True)
    normalized = origin.removesuffix(".git").rstrip("/")
    if normalized != "git@github.com:TGTChq/GTM":
        parsed = urlsplit(normalized)
        if parsed.hostname != "github.com" or parsed.path.lower() != "/tgtchq/gtm":
            raise RuntimeError("origin no corresponde a TGTChq/GTM.")
    print("1. Verificando el bundle y creando checkout de validacion aislado...", flush=True)
    run(["git", "-C", repo, "bundle", "verify", bundle])
    run(["git", "-C", repo, "fetch", bundle, branch])
    if run(["git", "-C", repo, "rev-parse", "FETCH_HEAD"], capture=True) != target:
        raise RuntimeError("SHA inesperado en el bundle.")
    checkout = package / "validation"
    if not checkout.exists():
        run(["git", "-C", repo, "worktree", "add", "--detach", checkout, target])
    if run(["git", "-C", checkout, "rev-parse", "HEAD"], capture=True) != target:
        raise RuntimeError("El checkout de validacion pertenece a otra version.")
    if run(["git", "-C", checkout, "status", "--porcelain", "--untracked-files=no"], capture=True):
        raise RuntimeError("Hay cambios en el checkout de validacion; no se sobrescriben.")
    py = args.python_path or Path(r"C:\TGTC\tgtc-core-budget-ef4e33b-venv\Scripts\python.exe")
    if not py.is_file():
        venv = package / "venv"
        py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not py.is_file():
            run([sys.executable, "-m", "venv", venv])
    print("2. Preparando dependencias y comprobando integridad...", flush=True)
    run([py, "-m", "pip", "install", "--disable-pip-version-check", "-r", checkout / "requirements-core-dev.txt",
         "-r", checkout / "requirements-dev.txt"])
    run([py, "ci_check_integrity.py"], cwd=checkout)
    results = {"sha": target, "published": False, "railway_changed": False}
    for suite, minimum in (("tests", manifest["legacy_minimum"]), ("tests_core", manifest["core_minimum"])):
        xml = package / (suite + "-results.xml")
        print(f"3. Suite completa {suite}, sin credenciales y con red externa bloqueada...", flush=True)
        run([py, "rebuild/run_offline_tests.py", suite, "-q", "-ra", "--tb=short", f"--junitxml={xml}"], cwd=checkout)
        results[suite] = validate_junit(xml, minimum=minimum, legacy=suite == "tests", windows=os.name == "nt")
        print(suite, results[suite], flush=True)
    if run(["git", "-C", checkout, "status", "--porcelain", "--untracked-files=no"], capture=True):
        raise RuntimeError("Las pruebas modificaron archivos versionados; no se publica.")
    if args.push:
        print("4. Publicando rama de validacion; esperando CI Linux del SHA exacto...", flush=True)
        run(["git", "-C", repo, "push", "origin", f"{target}:refs/heads/{branch}"])
        results["linux_ci"] = wait_ci(target)
        remote = run(["git", "-C", repo, "ls-remote", "origin", "refs/heads/feat/rebuild-core"], capture=True)
        remote_sha = remote.split()[0] if remote else ""
        if remote_sha not in (base, target):
            raise RuntimeError("feat/rebuild-core avanzo con otros cambios. No se sobrescribe.")
        print("5. CI completo verde. Publicando feat/rebuild-core sin force...", flush=True)
        run(["git", "-C", repo, "push", "origin", f"{target}:refs/heads/feat/rebuild-core"])
        verified = run(["git", "-C", repo, "ls-remote", "origin", "refs/heads/feat/rebuild-core"], capture=True)
        if not verified.startswith(target + "\t"):
            raise RuntimeError("No se pudo confirmar el SHA remoto.")
        results["published"] = True
        # Update only the expected, clean local feature branch. Never reset edits.
        local_branch = run(["git", "-C", repo, "branch", "--show-current"], capture=True)
        clean = not run(["git", "-C", repo, "status", "--porcelain"], capture=True)
        local_head = run(["git", "-C", repo, "rev-parse", "HEAD"], capture=True)
        if local_branch == "feat/rebuild-core" and clean and local_head == base:
            run(["git", "-C", repo, "merge", "--ff-only", target])
    destination = package / "resultado_validacion.json"
    destination.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print("Validacion terminada:", destination)
    print("Sin despliegue, SQL de produccion, llamadas a proveedores ni envios.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, ET.ParseError) as exc:
        print("Detenido:", exc, file=sys.stderr)
        raise SystemExit(1)
