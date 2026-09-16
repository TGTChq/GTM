"""Run tests in a credential-free child; never load a deployed database URL.

Use: python rebuild/run_offline_tests.py tests_core -q
PostgreSQL is the local embedded test instance, never production.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv) or ["tests_core", "-q"]
    portable = "--portable-core" in args
    if portable:
        args.remove("--portable-core")
    with tempfile.TemporaryDirectory(prefix="tgtc-offline-") as tmp:
        # Deliberate allow-list: do not inherit provider keys, production URLs,
        # autorun flags, user pytest options, or user-site packages.
        env = {k: os.environ[k] for k in (
            "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LD_LIBRARY_PATH",
            "LANG", "LC_ALL", "TZ", "TMP", "TEMP", "TMPDIR",
            "TGTC_REQUIRE_LEGACY_CONSUMER_TEST",
        ) if k in os.environ}
        env.update(HOME=tmp, USERPROFILE=tmp, PYTHONNOUSERSITE="1",
                   PYTHON_DOTENV_DISABLED="1", PYTHONHASHSEED="0",
                   PIPELINE_AUTORUN_ENABLED="0", TGTC_CORE_AUTORUN_ENABLED="0")
        # Descendant Python processes also install the guard before importing
        # application modules. A shell-only fixture cannot carry a provider key.
        Path(tmp, "sitecustomize.py").write_text(
            "from types import SimpleNamespace\n"
            "import ci_no_network\n"
            "ci_no_network.pytest_configure(SimpleNamespace())\n", encoding="utf-8")
        if portable:
            Path(tmp, "ci_portable_selection.py").write_text(
                "def pytest_collection_modifyitems(config, items):\n"
                "    removed = [i for i in items if {'conn', 'conn2', 'pg_url'} & set(i.fixturenames)]\n"
                "    items[:] = [i for i in items if i not in removed]\n"
                "    config.hook.pytest_deselected(items=removed)\n", encoding="utf-8")
            args += ["-p", "ci_portable_selection"]
        env["PYTHONPATH"] = os.pathsep.join((tmp, str(ROOT)))
        return subprocess.run([sys.executable, "-m", "pytest", "-p", "ci_no_network", *args],
                              cwd=ROOT, env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
