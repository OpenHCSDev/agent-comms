"""Different observations of one turn share its three-field identity."""
from agent_comms.execution.turn_lease import FinishedTurnFence, StoredTurnRecord, TurnLeaseFence
from agent_comms.nominal.field_codec import TurnId
from agent_comms.registry.registry_store import RegistryStore
from agent_comms.registry.thread_identity import (
    RecordedThreadIncarnation,
    TurnFence,
    TurnIdentity,
)
from agent_comms.storage.locked_store import RegistryFile


def test_turn_identity_equality_across_observation_types(tmp_path):
    identity = TurnIdentity(RecordedThreadIncarnation("worker", 1.0), 1)
    observed = TurnFence(identity, TurnId('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'), 1)
    finished = FinishedTurnFence(identity, TurnId('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'), 1)
    lease = TurnLeaseFence(
        identity, TurnId('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'), 1,
        record=StoredTurnRecord.for_identity(RegistryFile.path_in(tmp_path), identity, registry_type=RegistryStore),
    )
    assert observed == finished == lease
    assert lease == finished == observed
    assert hash(observed) == hash(finished) == hash(lease)
    assert observed != TurnFence(identity, TurnId('bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'), 1)
    assert observed != TurnFence(identity, TurnId('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'), 2)
    assert observed.__eq__(None) is NotImplemented
