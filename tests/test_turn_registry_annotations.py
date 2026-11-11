"""Turn storage and terminal-wait callbacks expose their runtime contracts."""

from typing import get_type_hints

import pytest

from agent_comms.execution.turn_lease import FinishedTurnFence
from agent_comms.execution.turn_lease import NoFinishedTurn
from agent_comms.execution.turn_lease import StoredTurnRecord
from agent_comms.execution.turn_lease import TurnCompletion


@pytest.mark.parametrize("callback", (
    StoredTurnRecord.registry.fget,
    TurnCompletion.idle_sources,
    NoFinishedTurn.idle_sources,
    FinishedTurnFence.idle_sources,
))
def test_turn_registry_callback_annotations(callback):
    assert get_type_hints(callback)
