"""Regression: the definitive production ATS lane can be *constructed* without a
NameError.

Production hotfix. A live "Run Now" crashed immediately after strict preflight
with ``NameError: name 'logger' is not defined`` while ``main()`` built the ATS
lane at ``lane_runners["ats"] = _live_ats_runner(a)`` -- specifically the
``logger.info("ATS scheduler: ...")`` diagnostic on the definitive registry path
(reached only when no static ``--boards`` file is supplied, i.e. the real
production command). ``run_orchestrator`` is a print-based entry script and never
defined ``logger``; the two stray ``logger`` calls were the only offenders.

The pre-existing ATS tests only exercised ``retrieval_measurement.ats_schedule``
primitives, never ``run_orchestrator._live_ats_runner`` itself, so the crashing
statement was never executed under test. These tests construct the runner on the
exact production path (``a.boards`` unset) far enough to run both converted
diagnostics -- which would raise NameError before the fix -- while making zero
external calls (the registry is faked and the returned runner is never invoked).
"""

from __future__ import annotations

import argparse

import config
import run_orchestrator
from retrieval_measurement import ats_schedule


class _FakeRegistry:
    """Stand-in for AtsBoardRegistry: local only, no network, no real registry
    file. Construction of ``_live_ats_runner`` reaches the scheduler diagnostics
    before ever touching the registry, but faking it keeps the test hermetic."""

    entries: dict = {}

    def seed_from_history(self) -> None:  # best-effort seed in the real path
        pass

    def due_entries(self, limit, force):  # matches the definitive-path call
        return [{"provider": "greenhouse", "identifier": "acme"}]


def _production_namespace():
    # boards=None => the DEFINITIVE registry path that hits the crashing
    # ``logger.info`` line, not the static ``runner_static`` early-return.
    return argparse.Namespace(boards=None, max_boards=5, artifact_root=".")


def _patch_registry(monkeypatch):
    import ats_board_registry

    monkeypatch.setattr(ats_board_registry, "AtsBoardRegistry", _FakeRegistry)


def test_live_ats_runner_definitive_path_constructs_without_nameerror(monkeypatch, capsys):
    """The exact production failure: constructing the ATS runner on the registry
    path must not raise (previously NameError at the scheduler ``logger.info``)."""
    _patch_registry(monkeypatch)

    runner = run_orchestrator._live_ats_runner(_production_namespace())

    assert callable(runner)  # returns the lane runner; no NameError building it
    out = capsys.readouterr().out
    # The precise statement that crashed in production now emits to stdout.
    assert "ATS scheduler: mode=" in out


def test_live_ats_runner_unreadable_state_warns_without_nameerror(monkeypatch, capsys):
    """The other converted line: the best-effort scheduler-state ``except`` branch
    (``logger.warning`` -> print) must also construct cleanly when state load
    fails and a state path is configured."""
    _patch_registry(monkeypatch)
    monkeypatch.setattr(config, "ATS_SCHEDULER_STATE_PATH", "unreadable-state.json")

    def _boom(*_a, **_k):
        raise ValueError("corrupt scheduler state")

    monkeypatch.setattr(ats_schedule.SchedulerState, "load", _boom)

    runner = run_orchestrator._live_ats_runner(_production_namespace())

    assert callable(runner)
    out = capsys.readouterr().out
    assert "ATS scheduler state unreadable" in out  # warning branch executed
    assert "ATS scheduler: mode=" in out            # info line still reached
