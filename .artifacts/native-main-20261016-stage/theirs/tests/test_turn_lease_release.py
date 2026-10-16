"""An ending attempt cannot release a replacement just because its ID is reused."""

import os
from dataclasses import replace

import pytest

from agent_comms.comms import wire
from agent_comms.child_process import ProcessIdentity
from agent_comms.field_codec import FieldCodec
from agent_comms.threads import Thread
from agent_comms.turn_lease import ActiveTurn


def test_activity_start_failure_cannot_clear_replacement_turn(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.registry.declare(Thread("owner", frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    replacement = []

    def fail_after_replacement(_activity):
        original = comms.registry.require("owner")
        assert comms.registry.release_turn(original.turn_lease)[0]
        successor, _ = comms.registry.lease_local_turn("owner", "same-id")
        replacement.append(successor.turn_lease)
        raise OSError("activity publication failed")

    monkeypatch.setattr(comms.agents.activity, "emit", fail_after_replacement)
    with pytest.raises(OSError, match="activity publication failed"):
        comms.agents.begin_turn("owner", "same-id")
    assert comms.registry.require("owner").turn_lease == replacement[0]


def test_old_lease_cannot_clear_same_id_after_delete_rebind(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread("owner", frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    original = comms.agents.begin_turn("owner", "same-id").turn_lease
    comms.registry.unregister("owner")
    comms.registry.remove("owner")
    comms.registry.declare(Thread("owner", frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    successor = comms.agents.begin_turn("owner", "same-id").turn_lease
    assert original.identity.generation == successor.identity.generation
    saved = comms.registry.store.path.read_bytes()
    assert comms.registry.release_turn(original) == (False, None)
    assert comms.registry.store.path.read_bytes() == saved
    assert comms.registry.require("owner").turn_lease == successor
    assert comms.agents.finish_turn(successor) is not None


def test_revoked_admission_cleanup_cannot_attest_goal_completion(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread("owner", frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    lease = comms.agents.begin_turn("owner", "turn").turn_lease
    comms.registry.archive("owner")
    assert comms.registry.release_turn(lease) == (True, None)
    assert comms.registry.require("owner").active_turn is None


def test_unattested_saved_turn_remains_data_without_a_lease(tmp_path):
    owner = Thread("owner", frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    retained = replace(owner, active_turn=ActiveTurn("retained", owner.pid))
    restored = FieldCodec.decode(Thread, FieldCodec.encode(retained))
    assert restored.active_turn == retained.active_turn
    assert restored.turn_lease is None
