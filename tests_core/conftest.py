"""Integrated tests run against a REAL PostgreSQL (embedded via pgserver, or the
server named by TGTC_TEST_DATABASE_URL). Providers are SIMULATED (tgtc_core.testing).

Every test gets a fresh schema. Nothing here touches a live service.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tgtc_core.db import connect, reset_schema  # noqa: E402
from tgtc_core.testing.embedded_pg import database_url  # noqa: E402

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def pg_url():
    tmp = tempfile.mkdtemp(prefix="tgtc_pgtest_")
    url, server = database_url(tmp)
    yield url
    if server is not None:
        try:
            server.cleanup()
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass


@pytest.fixture
def conn(pg_url):
    c = connect(pg_url)
    reset_schema(c)
    yield c
    c.close()


@pytest.fixture
def conn2(pg_url):
    """A second, independent connection (a second worker)."""
    c = connect(pg_url)
    yield c
    c.close()


@pytest.fixture
def now():
    return NOW


@pytest.fixture
def clock(now):
    class Clock:
        def __init__(self, t):
            self.t = t

        def __call__(self):
            return self.t

        def advance(self, **kw):
            from datetime import timedelta

            self.t = self.t + timedelta(**kw)
            return self.t

    return Clock(now)
