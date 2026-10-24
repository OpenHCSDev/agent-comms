"""An ending attempt cannot release a replacement just because its ID is reused."""

import os


from agent_comms.comms import wire
from agent_comms.child_process import ProcessIdentity
from agent_comms.field_codec import FieldCodec, TurnId
from agent_comms.threads import Thread
from agent_comms.turn_lease import FinishedTurnFence, NoFinishedTurn, NoTurnLease


def test_old_lease_cannot_clear_same_id_after_delete_rebind(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    original = comms.agents.begin_turn(
        "owner", TurnId('058be655fae2a4c37a21d2c091ba7661')
    ).turn_lease
    comms.registry.unregister("owner")
    comms.registry.remove("owner")
    comms.registry.declare(Thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    successor = comms.agents.begin_turn(
        "owner", TurnId('058be655fae2a4c37a21d2c091ba7661')
    ).turn_lease
    assert original.identity.generation == successor.identity.generation
    saved = comms.registry.store.path.read_bytes()
    assert comms.registry.release_turn(original) == (False, NoFinishedTurn())
    assert comms.registry.store.path.read_bytes() == saved
    assert comms.registry.require("owner").turn_lease == successor
    assert isinstance(comms.agents.finish_turn(successor), FinishedTurnFence)


def test_revoked_claim_cleanup_cannot_attest_goal_completion(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    lease = comms.agents.begin_turn("owner", TurnId('9b473845f684444f80a26586ae298d2b')).turn_lease
    comms.registry.archive("owner")
    assert comms.registry.release_turn(lease) == (True, NoFinishedTurn())
    assert not comms.registry.require('owner').executing


def test_unattested_saved_turn_remains_data_without_a_lease(tmp_path, *, thread_registration):
    owner = Thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    thread_registration.register(owner)
    retained, _ = thread_registration.lease_local_turn(
        'owner', TurnId('ca67bd59cc4116ac80db5f64cbf4747f')
    )
    expected = retained.turn_record.read().without_claim()
    thread_registration.register(retained, new_owner=True)
    # P482 stores the turn separately; the registry binds a decoded reference.
    restored = thread_registration.store._bind_thread(
        FieldCodec.decode(Thread, FieldCodec.encode(retained))
    )
    assert restored.turn_record.read() == expected
    assert restored.turn_lease == NoTurnLease()
