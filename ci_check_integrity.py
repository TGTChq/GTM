"""Verify orchestrator.MANIFEST.sha256 against the working tree. Exit 1 on drift.

    python ci_check_integrity.py

This is the same check ``run_orchestrator.py``'s strict preflight performs, with
the same CRLF normalization, extracted so CI can run it without a config, a
credential or an artifact root. It exists because a manifested file edited without
refreshing the manifest fails the production preflight **before any external
request** -- the run stops, and the only signal is a log line nobody is watching
at 03:00 UTC.

To refresh after an intentional edit, rewrite each line as
``<sha256 of LF-normalized bytes>  <length of those bytes>  <path>``.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

MANIFEST = Path("orchestrator.MANIFEST.sha256")


def main() -> int:
    if not MANIFEST.is_file():
        print(f"FAILED: {MANIFEST} is absent", file=sys.stderr)
        return 1

    checked = mismatch = absent = 0
    problems = []
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        expected, expected_size, name = parts[0], parts[1], parts[2]
        path = Path(name)
        if not path.is_file():
            absent += 1
            problems.append(f"  absent   {name}")
            continue
        checked += 1
        data = path.read_bytes().replace(b"\r\n", b"\n")
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            mismatch += 1
            problems.append(
                f"  mismatch {name}\n"
                f"           expected {expected}  {expected_size} bytes\n"
                f"           actual   {actual}  {len(data)} bytes")

    ok = mismatch == 0 and absent == 0
    print(f"package_integrity    checked={checked} mismatch={mismatch} "
          f"absent={absent} ({'OK' if ok else 'FAILED'})")
    if problems:
        print("\n".join(problems), file=sys.stderr)
        print(
            "\nA manifested file changed without the manifest being refreshed. The "
            "production strict preflight makes the same comparison and refuses to "
            "run, so this must be resolved rather than bypassed.",
            file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
