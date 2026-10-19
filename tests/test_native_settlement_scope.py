"""Historical release on real isolated stores, never on a retained user input."""

from agent_comms.messages import NoticeRole
import sqlite3

import pytest

from agent_comms.attempt_states import SucceededAttempt

from agent_comms.coordination_session import (
    CoordinationSession,
    NativeBoundary,
    NativeCoordinationSession,
)
from agent_comms.coordinator import Coordination, StandaloneCoordination
from agent_comms.envelope_claim_transitions import ExistingFileClaim
from agent_comms.selected_claim import publish_selected_resource_claim
from agent_comms.threads import Thread
from test_claim_bus_read import selected_turn, settle_failed


def strand_terminal(monkeypatch, store):
    # Recreate the old installed settlement's omitted release, not an input retry.
    with monkeypatch.context() as patch:
        patch.setattr(NativeCoordinationSession, "release_resources", lambda *_: None)
        settle_failed(store)
    terminal = store.snapshots.get("execution")
    assert terminal.attempt.lifecycle.terminal and not terminal.is_current
    return terminal


def test_historical_terminal_release_keeps_recorded_incarnation_and_path(monkeypatch, *, thread_registration):
    with selected_turn() as (comms, store, selected, resource):
        claimed = publish_selected_resource_claim(comms, store, selected, "Alice", ExistingFileClaim(resource))
        terminal = strand_terminal(monkeypatch, store)
        original = comms.registry.require("Alice")
        other_project = resource.parent.parent / "new-project"
        other_project.mkdir()
        later_file = other_project / "later.py"
        later_file.write_text("later\n")
        comms.registry.remove("Alice")
        comms.registry.declare(thread_registration.new_thread('Alice', original.tags, str(other_project), created_at=original.created_at + 100))
        comms.messaging.send("Alice", "sender", "Independent manual claim",
                                     claims=(ExistingFileClaim(later_file),)).message
        later = comms.bus.log.claim_projection()[str(later_file)]
        assert later.incarnation != claimed.incarnation
        with store.session.settlement() as boundary:
            store.session.release_resources(terminal, terminal.attempt.lifecycle, boundary)
        assert dict(comms.bus.log.claim_projection()) == {str(later_file): later}
        release = list(comms.views.full_history_page().messages)[-1]
        assert release.target == terminal.assignments[0].lifecycle.exact_target
        assert isinstance(release.role, NoticeRole)
        assert release.claim_transition.incarnation == claimed.incarnation
        assert release.claim_transition.releases[0].generation == claimed.generation


def test_old_terminal_cleanup_preserves_later_generation_of_same_file(monkeypatch):
    with selected_turn() as (comms, store, selected, resource):
        publish_selected_resource_claim(comms, store, selected, "Alice", ExistingFileClaim(resource))
        terminal = strand_terminal(monkeypatch, store)
        comms.messaging.send("Alice", "sender", "Replace old generation", releases=(str(resource),)).message
        comms.messaging.send("Alice", "sender", "Later manual generation",
                                     claims=(ExistingFileClaim(resource),)).message
        later = comms.bus.log.claim_projection()[str(resource)]
        before = (comms.root / "bus.jsonl").read_bytes()
        with store.session.settlement() as boundary:
            store.session.release_resources(terminal, terminal.attempt.lifecycle, boundary)
        assert (comms.root / "bus.jsonl").read_bytes() == before
        assert dict(comms.bus.log.claim_projection()) == {str(resource): later}


def test_native_boundary_preserves_the_lock_owner_default_wait_policy():
    assert NativeBoundary(object()).contention is None
    assert NativeBoundary(object(), blocking=False).contention is None


def test_native_coordination_defers_registry_read_until_bus_use(tmp_path, monkeypatch):
    from agent_comms.registration import Registration

    reads = []
    original = Registration.__init__

    def observed(registration, path):
        reads.append(path)
        original(registration, path)

    monkeypatch.setattr(Registration, "__init__", observed)
    with Coordination(tmp_path / "coordination.sqlite3") as store:
        assert reads == []
        assert store.session.bus.log.path == tmp_path / "bus.jsonl"
        assert reads == [tmp_path / "registry.json"]
        assert store.session.bus is store.session.bus
        assert reads == [tmp_path / "registry.json"]


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
        with pytest.raises(sqlite3.IntegrityError, match="completed execution requires all original responses"):
            store.attempts.settle_nonpublication(fence, expected_pointer_revision=1,
                                                outcome=SucceededAttempt())
        assert store.snapshots.get("exec") == before
        assert not store.session._connection.in_transaction
