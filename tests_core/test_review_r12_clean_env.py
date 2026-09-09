"""R12 -- the core suite must run with only the declared core dependencies. The registry
test reads the legacy env map from source; nothing in tests_core imports the legacy
``config`` module (which needs python-dotenv), except the read-only legacy-consumer test,
which skips when the legacy tree is not importable."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module


def test_no_core_test_imports_the_legacy_config_module():
    offenders = []
    for path in (ROOT / "tests_core").glob("test_*.py"):
        if path.name == "test_legacy_consumer_exclusion.py":
            continue   # deliberately imports legacy airtable_client and skips if not importable
        for mod in _imports(path):
            if mod == "config" or mod.startswith("config."):
                offenders.append(path.name)
    assert offenders == []


def test_declared_core_dependencies_do_not_include_dotenv_and_do_include_the_db_driver():
    text = (ROOT / "requirements-core-dev.txt").read_text(encoding="utf-8") + (ROOT / "requirements-core.txt").read_text(encoding="utf-8")
    assert "python-dotenv" not in text
    assert "psycopg" in text and "pgserver" in text and "pytest" in text
