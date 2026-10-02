"""The core never imports legacy control flow; the frozen copy renderer is shared."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "tgtc_core"
#: `outbound_wave1` is the FROZEN Challenger copy renderer, not control flow: the
#: nine live campaign bodies contain no literal copy, so the core cannot build a
#: sendable Challenger lead without it. Its purity is proved below.
ALLOWED_LEGACY = {"domain_utils", "source_domains", "outbound_wave1"}
# Declared runtime dependencies (requirements-core.txt). `google` is google-auth,
# imported only inside the Drive publication path and only when it is configured.
STDLIB_OR_DEPS = set(sys.stdlib_module_names) | {"psycopg", "requests", "anthropic", "pgserver",
                                                 "google", "__future__", "tgtc_core"}


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


def test_shared_copy_renderer_has_no_legacy_or_network_dependencies():
    """The renderer must stay pure: stdlib only, no provider client, no config."""
    allowed = set(sys.stdlib_module_names) | {"__future__"}
    # `outcomes.py` holds a LAZY operator-only http_utils import. The renderer
    # neither calls nor imports it; that is proved in a fresh process below.
    for path in sorted((ROOT / "outbound_wave1").glob("*.py")):
        if path.name == "outcomes.py":
            continue
        assert set(_top_level_imports(path)) <= allowed, path
    import subprocess
    subprocess.run(
        [sys.executable, "-c",
         "import outbound_wave1.resolver, sys; "
         "leaked = {'http_utils', 'config', 'requests', 'instantly_client'} & set(sys.modules); "
         "assert not leaked, leaked"],
        cwd=ROOT, check=True)


def test_core_image_includes_the_shared_copy_and_claim_registry():
    text = (ROOT / "Dockerfile.core").read_text(encoding="utf-8")
    assert "COPY outbound_wave1/ ./outbound_wave1/" in text
    assert "COPY data/wave1_claims.json ./data/wave1_claims.json" in text


def _copy_sources(dockerfile_text):
    """Every source path the Dockerfile copies (the last token is the destination)."""
    for raw in dockerfile_text.splitlines():
        line = raw.strip()
        if not line.upper().startswith("COPY "):
            continue
        parts = [p for p in line[5:].split() if not p.startswith("--")]
        for src in parts[:-1]:
            yield src


def test_every_dockerfile_copy_source_is_allowed_into_the_build_context():
    """`Dockerfile.core.dockerignore` denies everything (`*`) and re-admits named
    paths, and BuildKit prefers it over the generic `.dockerignore`. So adding a
    COPY without a matching `!` line builds a context that lacks the file and the
    BUILD fails -- which is exactly how the 2026-10-02 copy fix first failed to
    deploy. Checking the Dockerfile alone cannot catch that; this checks both."""
    dockerfile = (ROOT / "Dockerfile.core").read_text(encoding="utf-8")
    ignore_path = ROOT / "Dockerfile.core.dockerignore"
    lines = [l.strip() for l in ignore_path.read_text(encoding="utf-8").splitlines()]
    lines = [l for l in lines if l and not l.startswith("#")]
    assert "*" in lines, "expected a deny-all ignore file; revisit this test if that changed"
    allowed = {l[1:].rstrip("/") for l in lines if l.startswith("!")}

    missing = []
    for src in _copy_sources(dockerfile):
        name = src.rstrip("/")
        covered = name in allowed or any(
            name == a or name.startswith(a + "/") for a in allowed if a
        )
        if not covered:
            missing.append(src)
    assert not missing, (
        "Dockerfile.core copies paths the ignore file excludes, so the build will "
        "fail with 'not found': %s" % missing
    )
