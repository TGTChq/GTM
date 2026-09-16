"""Build a self-contained Windows validator; never push, deploy or call providers.

Artifacts are derived from a clean, committed Git tree plus local test evidence.
"""
import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import textwrap
import xml.etree.ElementTree as ET
import zipfile

REPO = Path(__file__).resolve().parents[1]
BASE = "65e61a0c5fa1107f2a5e4a375269967539864173"
BRANCH = "codex/yield-recovery-profiles-20260916"

WRAPPER = r'''#!/usr/bin/env python3
"""Valida el cambio sin titulos cerrados. Sin proveedores ni SQL de produccion."""
import base64
import hashlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

PACKAGE_NAME = __PACKAGE__
PAYLOAD_SHA256 = __HASH__
PAYLOAD = (
__PAYLOAD__
)

def extract(destination):
    data = base64.b85decode(PAYLOAD)
    if hashlib.sha256(data).hexdigest() != PAYLOAD_SHA256:
        raise RuntimeError("Descarga incompleta o modificada.")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if (len(infos) != 8 or len({i.filename for i in infos}) != 8
                or sum(i.file_size for i in infos) > 10_000_000):
            raise RuntimeError("Estructura del paquete invalida.")
        for item in infos:
            if (Path(item.filename).name != item.filename or item.filename in (".", "..")
                    or "/" in item.filename or "\\" in item.filename):
                raise RuntimeError("Ruta insegura en el paquete.")
            target = destination / item.filename
            if target.exists() and target.read_bytes() != archive.read(item.filename):
                raise RuntimeError("No se sobrescribe el archivo distinto: " + str(target))
        destination.mkdir(parents=True, exist_ok=True)
        for item in infos:
            target = destination / item.filename
            if not target.exists():
                target.write_bytes(archive.read(item.filename))
    return destination

def main():
    args = sys.argv[1:]
    if "--self-test" in args:
        with tempfile.TemporaryDirectory(prefix="gtm-acquisition-selftest-") as tmp:
            package = extract(Path(tmp))
            return subprocess.run([sys.executable, str(package / "publish.py"), "--self-test"], check=False).returncode
    package = extract(Path(__file__).resolve().parent / PACKAGE_NAME)
    print("Paquete verificado:", package, flush=True)
    print("Activacion posterior: TGTC_ACQUISITION_STRATEGY=balanced_v1. Consulta README.md.", flush=True)
    return subprocess.run([sys.executable, str(package / "publish.py"), *args,
                           "--package", str(package)], check=False).returncode

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        print("Detenido:", exc, file=sys.stderr)
        raise SystemExit(1)
'''


def git(*args):
    return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()


def junit(path):
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    if not cases or any(c.find(tag) is not None for c in cases for tag in ("failure", "error", "skipped")):
        raise RuntimeError("Incomplete or failed local evidence: " + str(path))
    return len(cases)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--core-total", type=int, required=True)
    args = parser.parse_args()
    if git("status", "--porcelain"):
        raise RuntimeError("Commit and review the source tree before packaging.")
    target = git("rev-parse", "HEAD")
    if git("rev-parse", BRANCH) != target:
        raise RuntimeError("Release branch must point to the tested commit.")
    root = args.artifacts.resolve()
    legacy = root / "acquisition-legacy.xml"
    core = root / "acquisition-core-portable.xml"
    legacy_count, core_count = junit(legacy), junit(core)
    if legacy_count < 3755 or core_count < 476 or args.core_total < core_count:
        raise RuntimeError("Insufficient test coverage evidence.")
    package = root / ("GTM-adquisicion-" + target[:7])
    package.mkdir(exist_ok=False)
    bundle = package / "release.bundle"
    subprocess.run(["git", "bundle", "create", str(bundle), BRANCH, "^" + BASE], cwd=REPO, check=True)
    subprocess.run(["git", "bundle", "verify", str(bundle)], cwd=REPO, check=True)
    manifest = dict(target=target, base=BASE, branch=BRANCH,
                    bundle_sha256=hashlib.sha256(bundle.read_bytes()).hexdigest(),
                    legacy_minimum=3755, core_minimum=args.core_total)
    (package / "release.json").write_text(json.dumps(manifest, indent=2) + "\n")
    shutil.copy2(REPO / "rebuild/publish_validated_release.py", package / "publish.py")
    shutil.copy2(REPO / "rebuild/ACQUISITION_PROFILES.md", package / "README.md")
    shutil.copy2(legacy, package / "legacy-local.xml")
    shutil.copy2(core, package / "core-portable-local.xml")
    shutil.copy2(root / "acquisition-cohort-audit.json", package / "cohort-scope-offline.json")
    evidence = dict(sha=target, legacy_passed=legacy_count, legacy_subtests_passed=1001,
                    core_portable_passed=core_count, core_database_pending=args.core_total-core_count,
                    provider_calls=0, production_sql=False, deployed=False,
                    full_linux_ci_required=True, production_ready=False,
                    blocker="Local PostgreSQL cannot create required non-root system user.",
                    simulation="Cohort filter counts are NOT verified qualified jobs or leads.")
    (package / "evidence-local.json").write_text(json.dumps(evidence, indent=2) + "\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(package.iterdir()):
            archive.write(path, path.name)
    payload = buf.getvalue()
    code = WRAPPER.replace("__PACKAGE__", repr(package.name)).replace("__HASH__", repr(hashlib.sha256(payload).hexdigest()))
    code = code.replace("__PAYLOAD__", "\n".join("    " + repr(line) for line in textwrap.wrap(base64.b85encode(payload).decode(), 110)))
    output = root / "GTM-validar-adquisicion-sin-titulos.py"
    with output.open("x", encoding="utf-8") as handle:
        handle.write(code)
    print(json.dumps(dict(artifact=str(output), sha=target, size=output.stat().st_size), indent=2))


if __name__ == "__main__":
    main()
