"""Turn storage and terminal-wait callbacks expose their runtime contracts."""

from typing import get_type_hints

import pytest

from agent_comms.turn_lease import (
    FinishedTurnFence,
    NoFinishedTurn,
    StoredTurnRecord,
    TurnCompletion,
)


@pytest.mark.parametrize("callback", (
    StoredTurnRecord.registry.fget,
    TurnCompletion.idle_sources,
    NoFinishedTurn.idle_sources,
    FinishedTurnFence.idle_sources,
))
def test_turn_registry_callback_annotations(callback):
    assert get_type_hints(callback)
