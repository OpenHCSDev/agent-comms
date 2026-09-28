"""An ending attempt cannot release a replacement just because its ID is reused."""

import json
import os
from dataclasses import replace

import pytest

from agent_comms import wire
from agent_comms.threads import Thread
from agent_comms.turn_lease import ActiveTurn


def test_activity_start_failure_cannot_clear_replacement_turn(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.register(Thread("owner", frozenset(), str(tmp_path), pid=os.getpid()))
    replacement = []

    def fail_after_replacement(_activity):
        original = comms.registry.require("owner")
        assert comms.registry.release_turn(original.turn_lease)[0]
        successor, _ = comms.registry.claim_local_turn("owner", "same-id")
        replacement.append(successor.turn_lease)
        raise OSError("activity publication failed")

    monkeypatch.setattr(comms.activity, "emit", fail_after_replacement)
    with pytest.raises(OSError, match="activity publication failed"):
        comms.begin_turn("owner", "same-id")
    assert comms.registry.require("owner").turn_lease == replacement[0]


def test_old_lease_cannot_clear_same_id_after_delete_rebind(tmp_path):
    comms = wire(tmp_path)
    comms.register(Thread("owner", frozenset(), str(tmp_path), pid=os.getpid()))
    original = comms.begin_turn("owner", "same-id")
    comms.registry.unregister("owner")
    comms.registry.remove("owner")
    comms.register(Thread("owner", frozenset(), str(tmp_path), pid=os.getpid()))
    successor = comms.begin_turn("owner", "same-id")
    assert original.identity.generation == successor.identity.generation
    saved = comms.registry.store.path.read_bytes()
    assert comms.registry.release_turn(original) == (False, None)
    assert comms.registry.store.path.read_bytes() == saved
    assert comms.registry.require("owner").turn_lease == successor
    assert comms.finish_turn(successor) is not None


def test_revoked_admission_cleanup_cannot_attest_goal_completion(tmp_path):
    comms = wire(tmp_path)
    comms.register(Thread("owner", frozenset(), str(tmp_path), pid=os.getpid()))
    lease = comms.begin_turn("owner", "turn")
    comms.registry.archive("owner")
    assert comms.registry.release_turn(lease) == (True, None)
    assert comms.registry.require("owner").active_turn is None


def test_unattested_saved_turn_remains_data_without_a_lease(tmp_path):
    owner = Thread("owner", frozenset(), str(tmp_path), pid=os.getpid())
    retained = replace(owner, active_turn=ActiveTurn("retained", owner.pid))
    restored = Thread.from_registry(
        owner.name, json.loads(json.dumps(retained.to_wire())), tmp_path
    )
    assert restored.active_turn == retained.active_turn
    assert restored.turn_lease is None
