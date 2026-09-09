"""The core never imports the legacy control flow. Allowed legacy modules are exactly two."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "tgtc_core"
ALLOWED_LEGACY = {"domain_utils", "source_domains"}
STDLIB_OR_DEPS = set(sys.stdlib_module_names) | {"psycopg", "requests", "anthropic", "pgserver", "__future__", "tgtc_core"}


def _top_level_imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                yield node.module.split(".")[0]


def test_only_two_legacy_modules_are_imported():
    offenders = {}
    for path in PKG.rglob("*.py"):
        for name in _top_level_imports(path):
            if name in STDLIB_OR_DEPS or name in ALLOWED_LEGACY:
                continue
            offenders.setdefault(str(path.relative_to(ROOT)), set()).add(name)
    assert not offenders, f"unexpected imports: {offenders}"


def test_old_orchestrator_is_never_referenced():
    banned = ("run_orchestrator", "orchestrator.", "hiring_manager", "run_approved", "airtable_client", "instantly_client",
              "apollo_client", "role_gate", "email_gate", "job_gate", "contact_gate", "account_gate", "import config")
    hits = []
    for path in PKG.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = getattr(node, "module", None) or ",".join(a.name for a in node.names)
                for b in banned:
                    if b.rstrip(".") == (mod or "").split(".")[0] or (mod or "").startswith(b):
                        hits.append((str(path.relative_to(ROOT)), mod))
    assert not hits, hits
