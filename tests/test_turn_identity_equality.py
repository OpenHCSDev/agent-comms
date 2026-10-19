"""Different observations of one turn share its three-field identity."""
from agent_comms.thread_identity import RecordedThreadIncarnation, TurnFence, TurnIdentity
from agent_comms.turn_lease import FinishedTurnFence, StoredTurnRecord, TurnLeaseFence


def test_turn_identity_equality_across_observation_types(tmp_path):
    identity = TurnIdentity(RecordedThreadIncarnation("worker", 1.0), 1)
    observed = TurnFence(identity, "a" * 32, 1)
    finished = FinishedTurnFence(identity, "a" * 32, 1)
    lease = TurnLeaseFence(
        identity, "a" * 32, 1,
        record=StoredTurnRecord.for_identity(tmp_path / "registry.json", identity),
    )
    assert observed == finished == lease
    assert lease == finished == observed
    assert hash(observed) == hash(finished) == hash(lease)
    assert observed != TurnFence(identity, "b" * 32, 1)
    assert observed != TurnFence(identity, "a" * 32, 2)
    assert observed.__eq__(None) is NotImplemented
    assert not hasattr(observed, "matches")
