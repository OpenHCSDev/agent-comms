"""Settlement uses actual isolated stores, never a retained user input."""

import sqlite3
from dataclasses import replace

import pytest

from agent_comms.attempt_states import SucceededAttempt
from agent_comms.bus import Bus
from agent_comms.child_process import NoProcess
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.coordination_session import CoordinationSession
from agent_comms.coordinator import StandaloneCoordination
from agent_comms.envelope_claim_transitions import ExistingFileClaim
from agent_comms.selected_claim import publish_selected_resource_claim
from test_claim_bus_read import selected_turn, settle_failed


def test_recovery_does_not_release_a_claim_from_a_dead_owner(selected_turn):
    (comms, store, selected, resource) = selected_turn
    claim = publish_selected_resource_claim(
        comms, store, selected, "Alice", ExistingFileClaim(resource)
    )
    owner = comms.registry.require("Alice")
    comms.registry.register(replace(owner, process_identity=NoProcess()), StoppedThreadStatus())
    before = comms.bus.bus.revision()
    settle_failed(store)
    assert comms.bus.bus.revision() == before
    assert comms.bus.bus.claim_projection()[str(resource)] == claim
    assert not comms.bus.bus.claim_projection().live_in(comms.registry.snapshot())


def test_standalone_session_never_guesses_native_resources_from_bus_file(tmp_path):
    Bus.path_in(tmp_path).write_text("not a native bus\n")
    with StandaloneCoordination(tmp_path / "coordination.sqlite3") as store:
        assert type(store.session) is CoordinationSession
        with store.session.settlement() as boundary:
            assert boundary is None
        assert Bus.path_in(tmp_path).read_text() == "not a native bus\n"


def test_wire_success_cannot_silently_complete_original_response(tmp_path):
    from test_coordination_store import ready, started, final_evidence

    with ready(tmp_path / "coordination.sqlite3") as store:
        _, fence = started(store)
        fence = final_evidence(store, fence)
        before = store.get("exec")
        with pytest.raises(sqlite3.IntegrityError):
            store.settle_nonpublication(fence, expected_pointer_revision=1,
                                                outcome=SucceededAttempt())
        assert store.get("exec") == before
        assert not store.session._connection.in_transaction
