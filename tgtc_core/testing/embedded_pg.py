"""Embedded PostgreSQL for local tests and the offline demo.

Uses ``pgserver`` (pip-installed PostgreSQL 16 binaries) when ``TGTC_TEST_DATABASE_URL``
is not set. This is a LOCAL test convenience, never a production service.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional, Tuple


def database_url(base_dir: Optional[str] = None) -> Tuple[str, Optional[object]]:
    """Return (url, server_handle). Prefer an explicit test URL; else start pgserver."""
    explicit = os.environ.get("TGTC_TEST_DATABASE_URL", "").strip()
    if explicit:
        return explicit, None
    import pgserver  # type: ignore

    root = Path(base_dir or tempfile.mkdtemp(prefix="tgtc_pg_")).absolute()
    server = pgserver.get_server(str(root))
    return server.get_uri(), server
