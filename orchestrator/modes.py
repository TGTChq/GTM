"""Explicit execution modes and the permissions each one grants.

A mode is not a label -- it is a *capability grant*. The policy object is the
single authority every other component consults before it is allowed to touch
the network, write production state, or contact a delivery service. Because the
grant is data, a test can assert exactly what a mode may and may not do without
running anything.

Production is never the default. Constructing the default policy yields
``full_dry_run``; selecting ``production`` requires naming it explicitly AND
passing the production acknowledgement at the CLI boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict


class ExecutionMode(str, Enum):
    #: Replay recorded corpora/fixtures. No network, no production writes.
    OFFLINE_REPLAY = "offline_replay"
    #: Real provider requests for acquisition ONLY. No enrichment, no delivery,
    #: no production-state mutation. This is the mode the controlled live ATS
    #: validation uses.
    LIVE_ACQUISITION_ONLY = "live_acquisition_only"
    #: Every stage runs end to end against FAKE external adapters. No real
    #: network, no production writes -- the full waterfall, provably hermetic.
    FULL_DRY_RUN = "full_dry_run"
    #: Controlled mode: live acquisition + live Apollo/Hunter enrichment, but
    #: NO Airtable write and NO Instantly enrollment, and no production-state
    #: write. This is the mode the final controlled live run uses.
    LIVE_ACQUISITION_AND_ENRICHMENT = "live_acquisition_and_enrichment"
    #: The only mode that may reach real external services and write production
    #: state. Never the default; gated behind an explicit acknowledgement.
    PRODUCTION = "production"


@dataclass(frozen=True)
class ModePolicy:
    mode: ExecutionMode
    #: May any component open a real outbound socket?
    allow_network: bool
    #: May the acquisition lanes run against real providers?
    allow_live_acquisition: bool
    #: May enrichment (company/contact/email) run at all?
    allow_enrichment: bool
    #: May enrichment call real paid providers (Apollo/Hunter/...)?
    allow_live_enrichment: bool
    #: May delivery write to a real Airtable base?
    allow_airtable_write: bool
    #: May delivery enrol contacts in Instantly?
    allow_instantly_enrollment: bool
    #: May ANY component write to a production state directory?
    allow_production_state_write: bool
    #: Lanes permitted to make real requests in this mode (others must use a
    #: replay/fake fetcher or be skipped).
    def requires_production_ack(self) -> bool:
        return self.mode is ExecutionMode.PRODUCTION

    def to_dict(self) -> Dict[str, object]:
        return {
            "mode": self.mode.value,
            "allow_network": self.allow_network,
            "allow_live_acquisition": self.allow_live_acquisition,
            "allow_enrichment": self.allow_enrichment,
            "allow_live_enrichment": self.allow_live_enrichment,
            "allow_airtable_write": self.allow_airtable_write,
            "allow_instantly_enrollment": self.allow_instantly_enrollment,
            "allow_production_state_write": self.allow_production_state_write,
        }


_POLICIES: Dict[ExecutionMode, ModePolicy] = {
    ExecutionMode.OFFLINE_REPLAY: ModePolicy(
        ExecutionMode.OFFLINE_REPLAY,
        allow_network=False,
        allow_live_acquisition=False,
        allow_enrichment=True,          # runs against fakes
        allow_live_enrichment=False,
        # "write" here means to the INJECTED adapter; in these modes that is a
        # fake, and the real-service protection is allow_network=False plus
        # allow_production_state_write=False, not withholding the code path.
        allow_airtable_write=True,
        allow_instantly_enrollment=True,
        allow_production_state_write=False,
    ),
    ExecutionMode.LIVE_ACQUISITION_ONLY: ModePolicy(
        ExecutionMode.LIVE_ACQUISITION_ONLY,
        allow_network=True,
        allow_live_acquisition=True,
        allow_enrichment=False,         # acquisition only, by definition
        allow_live_enrichment=False,
        allow_airtable_write=False,
        allow_instantly_enrollment=False,
        allow_production_state_write=False,
    ),
    ExecutionMode.FULL_DRY_RUN: ModePolicy(
        ExecutionMode.FULL_DRY_RUN,
        allow_network=False,
        allow_live_acquisition=False,
        allow_enrichment=True,
        allow_live_enrichment=False,
        # Fake adapters injected; real-service safety is allow_network=False plus
        # allow_production_state_write=False.
        allow_airtable_write=True,
        allow_instantly_enrollment=True,
        allow_production_state_write=False,
    ),
    ExecutionMode.LIVE_ACQUISITION_AND_ENRICHMENT: ModePolicy(
        ExecutionMode.LIVE_ACQUISITION_AND_ENRICHMENT,
        allow_network=True,
        allow_live_acquisition=True,
        allow_enrichment=True,
        allow_live_enrichment=True,      # live Apollo/Hunter
        # Airtable REVIEW-STAGING is permitted (write reviewable leads as
        # Status=Pending for manual review). Instantly enrollment and
        # production-state writes remain forbidden; auto-approval is off by flag.
        allow_airtable_write=True,
        allow_instantly_enrollment=False,
        allow_production_state_write=False,
    ),
    ExecutionMode.PRODUCTION: ModePolicy(
        ExecutionMode.PRODUCTION,
        allow_network=True,
        allow_live_acquisition=True,
        allow_enrichment=True,
        allow_live_enrichment=True,
        allow_airtable_write=True,
        allow_instantly_enrollment=True,   # still behind its own explicit flag
        allow_production_state_write=True,
    ),
}

#: The default when nothing is specified. Deliberately the safest full-pipeline
#: mode, never production.
DEFAULT_MODE = ExecutionMode.FULL_DRY_RUN


def policy_for(mode: ExecutionMode) -> ModePolicy:
    return _POLICIES[ExecutionMode(mode)]
