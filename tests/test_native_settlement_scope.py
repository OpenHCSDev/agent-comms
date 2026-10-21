"""Historical release on real isolated stores, never on a retained user input."""

import os
import sqlite3
from dataclasses import replace

import pytest

from agent_comms.attempt_states import SucceededAttempt
from agent_comms.child_process import NoProcess, ProcessIdentity
from agent_comms.thread_status import RunningThreadStatus, StoppedThreadStatus

from agent_comms.coordination_session import (
    CoordinationSession,
    NativeCoordinationSession,
)
from agent_comms.coordinator import StandaloneCoordination
from agent_comms.envelope_claim_transitions import ExistingFileClaim
from agent_comms.selected_claim import publish_selected_resource_claim
from test_claim_bus_read import (
    acquire_tool_claim, claim_originals, release_tool_claim, selected_turn, settle_failed,
)


def strand_terminal(monkeypatch, store):
    # Recreate the old installed settlement's omitted release, not an input retry.
    with monkeypatch.context() as patch:
        patch.setattr(NativeCoordinationSession, "release_resources", lambda *_: None)
        settle_failed(store)
    terminal = store.snapshots.get("execution")
    assert terminal.attempt.lifecycle.terminal and not terminal.is_current
    return terminal


def test_recovery_does_not_release_a_claim_from_a_dead_owner():
    with selected_turn() as (comms, store, selected, resource):
        claim = publish_selected_resource_claim(
            comms, store, selected, "Alice", ExistingFileClaim(resource)
        )
        owner = comms.registry.require("Alice")
        comms.registry.register(replace(owner, process_identity=NoProcess()), StoppedThreadStatus())
        before = (comms.root / "bus.jsonl").read_bytes()
        settle_failed(store)
        assert (comms.root / "bus.jsonl").read_bytes() == before
        assert comms.bus.bus.claim_projection()[str(resource)] == claim
        assert not comms.bus.bus.claim_projection().live_in(comms.registry.snapshot())
        assert store.snapshots.get("execution").attempt.lifecycle.terminal


def test_historical_terminal_release_skips_dead_incarnation_and_keeps_new_owner(monkeypatch, *, thread_registration):
    with selected_turn() as (comms, store, selected, resource):
        claimed = publish_selected_resource_claim(comms, store, selected, "Alice", ExistingFileClaim(resource))
        terminal = strand_terminal(monkeypatch, store)
        original = comms.registry.require("Alice")
        other_project = resource.parent.parent / "new-project"
        other_project.mkdir()
        later_file = other_project / "later.py"
        later_file.write_text("later\n")
        comms.registry.remove("Alice")
        successor = thread_registration.new_thread(
            'Alice', original.tags, str(other_project), created_at=original.created_at + 100,
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
        comms.registry.declare(successor, RunningThreadStatus())
        later = acquire_tool_claim(comms, "Alice", later_file)
        assert later.incarnation != claimed.incarnation
        before = (comms.root / "bus.jsonl").read_bytes()
        with store.session.settlement() as boundary:
            store.session.release_resources(terminal, terminal.attempt.lifecycle, boundary)
        assert (comms.root / "bus.jsonl").read_bytes() == before
        assert dict(comms.bus.bus.claim_projection()) == {str(later_file): later}
        assert not [m for m in claim_originals(comms) if m.claim_transition.releases]


def test_old_terminal_cleanup_preserves_later_generation_of_same_file(monkeypatch):
    with selected_turn() as (comms, store, selected, resource):
        old = publish_selected_resource_claim(comms, store, selected, "Alice", ExistingFileClaim(resource))
        terminal = strand_terminal(monkeypatch, store)
        release_tool_claim(comms, old)
        later = acquire_tool_claim(comms, "Alice", resource)
        before = (comms.root / "bus.jsonl").read_bytes()
        with store.session.settlement() as boundary:
            store.session.release_resources(terminal, terminal.attempt.lifecycle, boundary)
        assert (comms.root / "bus.jsonl").read_bytes() == before
        assert dict(comms.bus.bus.claim_projection()) == {str(resource): later}




def test_standalone_session_never_guesses_native_resources_from_bus_file(tmp_path):
    (tmp_path / "bus.jsonl").write_text("not a native bus\n")
    with StandaloneCoordination(tmp_path / "coordination.sqlite3") as store:
        assert type(store.session) is CoordinationSession
        with store.session.settlement() as boundary:
            assert boundary is None
        assert (tmp_path / "bus.jsonl").read_text() == "not a native bus\n"


def test_wire_success_cannot_silently_complete_original_response(tmp_path):
    from test_coordination_store import ready, started, final_evidence

    with ready(tmp_path / "coordination.sqlite3") as store:
        _, fence = started(store)
        fence = final_evidence(store, fence)
        before = store.snapshots.get("exec")
        with pytest.raises(sqlite3.IntegrityError):
            store.attempts.settle_nonpublication(fence, expected_pointer_revision=1,
                                                outcome=SucceededAttempt())
        assert store.snapshots.get("exec") == before
        assert not store.session._connection.in_transaction
