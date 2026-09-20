"""`run-target` must drain the Instantly channel, not only Airtable.

Found in the production canary, not by reading code: 252 Instantly outbox rows
sat at `attempts = 0` with no error -- never claimed -- while Airtable
delivered normally in the same run. `run_target` passed
`delivery_channels=("airtable",)`, hardcoded.

This is an independent cause of production sending zero contacts, separate from
the missing INSTANTLY_* configuration, the absent cron, and the approvals that
pointed at retired Control campaigns. The target metric counts Airtable leads;
that is a reason to MEASURE Airtable, never a reason to leave the Instantly
outbox undrained.
"""
from __future__ import annotations

from tgtc_core.runner import TARGET_DELIVERY_CHANNELS


def test_run_target_delivers_both_channels():
    assert set(TARGET_DELIVERY_CHANNELS) == {"airtable", "instantly"}


def test_instantly_is_present_and_not_an_accident_of_ordering():
    assert "instantly" in TARGET_DELIVERY_CHANNELS
    assert "airtable" in TARGET_DELIVERY_CHANNELS
    assert len(TARGET_DELIVERY_CHANNELS) == 2


def test_run_target_uses_that_constant():
    """Guards against the hardcoded tuple coming back."""
    import inspect

    from tgtc_core import runner

    source = inspect.getsource(runner.Runner.run_to_target)
    assert "TARGET_DELIVERY_CHANNELS" in source
    assert 'delivery_channels=("airtable",)' not in source
