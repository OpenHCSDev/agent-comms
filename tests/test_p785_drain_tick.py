"""The drained session states all reasons another observation is owed."""

import pytest

from agent_comms.input_drain import DrainedSession
from agent_comms.routing import ScheduledTurn


@pytest.mark.parametrize("turn_live", [False, True])
def test_session_owes_tick_for_each_unconsumed_reason(turn_live, monkeypatch):
    state = DrainedSession()
    assert not state.owes_tick(False)
    assert not state.self_tick_owed(0, False, turn_live)

    state.repeat_observation = True
    assert state.owes_tick(False)
    assert state.self_tick_owed(0, False, turn_live) == (not turn_live)
    assert not state.self_tick_owed(1, False, turn_live)
    state.repeat_observation = False

    state.pending_turns.append(ScheduledTurn("queued"))
    assert state.owes_tick(False)
    assert not state.self_tick_owed(0, False, turn_live)
    state.pending_turns.clear()

    assert state.owes_tick(True)
    assert state.self_tick_owed(1, True, turn_live)

    monkeypatch.setattr(state, "wake_pending", lambda: True)
    assert state.owes_tick(False)
    assert not state.self_tick_owed(0, False, turn_live)
