"""Replacement acquisition/delivery orchestrator.

This package REPLACES the orchestration boundary that the Final Controlled Live
ATS Validation proved defective: the validated acquisition components
(``ats_board_registry.fetch_board_jobs`` and the ``retrieval_measurement``
budget/checkpoint/trace/scheduler stack) were never assembled into a
production-reachable, ATS-only, checkpointed, budgeted and reconcilable
execution path. They are here.

Design rules
------------
* Composition over extension. Every collaborator (fetcher, budget, session,
  trace, board source, enrichment, delivery) is INJECTED. Nothing is imported
  for its side effects.
* Production is never the default execution mode.
* Offline modes make zero external network calls and never mutate production
  state -- enforced structurally, not by convention.
* Every artifact of one run shares one ``run_id``. Every boundary reconciles.

The legacy ``run_daily.run_pipeline`` function is NOT extended. This package is
the seam a future top-level scheduler drives instead.
"""

from __future__ import annotations

#: Bumped when the artifact/reconciliation contract changes in a breaking way.
ORCHESTRATOR_VERSION = "2.0.0"
SCHEMA_VERSION = "orchestrator/2"

from orchestrator.modes import ExecutionMode, ModePolicy, policy_for
from orchestrator.reasons import Disposition, ReasonCode

__all__ = [
    "ORCHESTRATOR_VERSION",
    "SCHEMA_VERSION",
    "ExecutionMode",
    "ModePolicy",
    "policy_for",
    "Disposition",
    "ReasonCode",
]
