"""Idle and active turns keep the registry's null-or-record format."""
from dataclasses import replace

import pytest

from agent_comms.child_process import NoProcess, ProcessIdentity
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.thread_identity import RecordedThreadIncarnation, TurnId, TurnIdentity
from agent_comms.turn_lease import ActiveTurn, NoTurn, StoredTurnRecord, TurnDeclaration, TurnState
from agent_comms.turn_phase import ToolRunningPhase


@pytest.mark.parametrize('turn', [NoTurn(), ActiveTurn('turn', 123, started_at=1.0)])
def test_turn_family_round_trip_keeps_its_original_wire_form(turn):
    encoded = FieldCodec.encode(turn, TurnDeclaration)
    assert FieldCodec.decode(TurnDeclaration, encoded) == turn
    assert TurnDeclaration.from_wire(encoded) == turn


def test_idle_turn_answers_without_an_active_record():
    idle = NoTurn()
    assert FieldCodec.encode(idle) is None
    assert not idle.executing
    assert idle.report_turn == ''
    assert not idle.matches(TurnId('turn'))
    assert not idle.started_by(10000)
    assert idle.observed_id('finished') == 'finished'
    assert idle.without_claim() is idle
    assert not tuple(idle.phase_effects(ToolRunningPhase()))
    idle.validate_owner(NoProcess(), 0)
    idle.require_idle()
    idle.require_current(1, 1)
    with pytest.raises(RelationViolationError):
        idle.require_active()
    with pytest.raises(RelationViolationError):
        idle.require_turn(TurnId('turn'))


def test_active_turn_validates_its_owner_and_preserves_fences(tmp_path):
    turn = ActiveTurn('turn', 123, started_at=1.0, claimed_generation=2, turn_generation=3)
    turn.validate_owner(ProcessIdentity(123, 456), 3)
    turn.require_current(2, 3)
    turn.require_turn(TurnId('turn'))
    assert turn.executing and turn.has_claim(2)
    assert turn.require_active() is turn
    assert turn.started_by(1000)
    assert not turn.started_by(900)
    assert turn.observed_id('finished') == 'turn'
    assert turn.without_claim() == replace(turn, claimed_generation=None)
    incarnation = RecordedThreadIncarnation('worker', 1.0)
    record = StoredTurnRecord.for_identity(tmp_path / 'registry.json', TurnIdentity(incarnation, 3))
    lease = turn.lease(incarnation, record)
    assert lease.turn_id == 'turn' and lease.claimed_generation == 2
    with pytest.raises(RelationViolationError):
        turn.validate_owner(NoProcess(), 3)
    with pytest.raises(RelationViolationError):
        turn.require_idle()
    with pytest.raises(RelationViolationError):
        turn.require_current(9, 3)
    with pytest.raises(RelationViolationError):
        turn.require_turn(TurnId('other'))
    phase = ToolRunningPhase()
    state, = TurnState(turn).phase_effects(phase)
    assert state.active == replace(turn, phase=phase)
    assert state.matches('turn') and not state.matches('other')
