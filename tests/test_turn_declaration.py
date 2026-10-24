"""Idle and active turns keep the registry's null-or-record format."""

import pytest

from agent_comms.child_process import NoProcess, ProcessIdentity
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec, TurnId
from agent_comms.thread_identity import (
    NoObservedTurn,
    RecordedThreadIncarnation,
    TurnFence,
    TurnIdentity,
)
from agent_comms.turn_lease import (
    ActiveTurn, FinishedTurn, NoTurn, NoTurnRecord, TurnDeclaration,
)
from agent_comms.reported_turn import NoReportedTurn
from agent_comms.turn_phase import ToolRunningPhase


@pytest.mark.parametrize('turn', [
    NoTurn(),
    ActiveTurn(TurnId('9b473845f684444f80a26586ae298d2b'),
               ProcessIdentity(123, 1), started_at=1.0),
])
def test_turn_family_round_trip_keeps_its_original_wire_form(turn):
    encoded = FieldCodec.encode(turn, TurnDeclaration)
    assert FieldCodec.decode(TurnDeclaration, encoded) == turn
    assert TurnDeclaration.from_wire(encoded) == turn


def test_idle_turn_answers_without_an_active_record():
    idle = NoTurn()
    assert FieldCodec.encode(idle) is None
    idle.require_idle()
    assert idle.reported_turn == NoReportedTurn()
    assert not idle.matches(TurnId('9b473845f684444f80a26586ae298d2b'))
    assert not idle.started_by(10000)
    identity = TurnIdentity(RecordedThreadIncarnation("owner", 1.0), 1)
    turn_id = TurnId.mint()
    missing = NoObservedTurn()
    fence = TurnFence(identity, turn_id, 1)
    assert idle.observed_turn(NoTurnRecord(), identity, 1) == missing
    assert idle.observed_turn(FinishedTurn(turn_id), identity, 1) == fence
    assert missing == NoObservedTurn() and not missing.can_attest(1)
    assert missing != fence and fence != missing
    assert idle.without_claim() is idle
    assert not tuple(idle.phase_effects(ToolRunningPhase()))
    idle.validate_owner(NoProcess(), 0)
    idle.require_idle()
    idle.require_current(1, 1)
    with pytest.raises(RelationViolationError):
        idle.require_active()
    with pytest.raises(RelationViolationError):
        idle.require_turn(TurnId('9b473845f684444f80a26586ae298d2b'))


