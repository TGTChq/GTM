"""The per-service Railway config for the core batch.

Activated by RAILWAY_CONFIG_FILE on ONE service; a root railway.json would
apply to every service deploying from this repo.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "railway.core.json").read_text(encoding="utf-8"))
DEPLOY = CONFIG["deploy"]
CMD = DEPLOY["startCommand"]


def test_daily_schedule_at_three_utc():
    assert DEPLOY["cronSchedule"] == "0 3 * * *"


def test_a_short_run_never_restarts_and_spends_again():
    assert DEPLOY["restartPolicyType"] == "NEVER"


def test_migrations_run_before_the_batch():
    assert CMD.index("tgtc_core migrate") < CMD.index("tgtc_core budget") < CMD.index("tgtc_core run-target")


def test_the_budget_id_changes_every_day():
    """A fixed id is a daily-cron defect: day two's grant lands on a budget that
    day one already spent, and the run buys nothing."""
    assert "$(date -u +%Y%m%d)" in CMD


def test_budget_and_run_share_the_same_daily_id():
    ids = re.findall(r"--budget-id (\$[A-Z]+)", CMD)
    assert len(ids) == 2 and ids[0] == ids[1] == "$BID"


def test_the_batch_is_the_production_target_run():
    assert "run-target" in CMD and "--i-understand-spend" in CMD and "--target 1000" in CMD


def test_the_fantastic_grant_is_sustainable_on_the_monthly_plan():
    """100,000 records/month is about 3,333/day; the grant must not exceed it."""
    credits = int(re.search(r"--fantastic-credits (\d+)", CMD).group(1))
    assert credits <= 3333


def test_builds_the_core_image():
    assert CONFIG["build"]["dockerfilePath"] == "Dockerfile.core"
