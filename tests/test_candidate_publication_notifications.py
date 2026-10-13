"""Every publication owner notifies derived maintenance after its locks exit."""

import sqlite3
import threading

import pytest

from agent_comms import candidate_maintenance, claim_admission, messaging
from agent_comms.attempt_start import AttemptStart
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.coordination_response import (
    prepare_fenced_response, publish_fenced_response, resolve_existing_response,
)
from agent_comms.coordination_tables.executions import ExecutionOrigin
from agent_comms.envelope_claim_transitions import ExistingFileClaim, WakeAdmission
from agent_comms.owner_fence import prepare_fence_token
from agent_comms.selected_source_batch import SelectedSource
from agent_comms.store_files import _store_lock
from agent_comms.wake_candidate_index import WakeCandidateIndex
from test_coordination_response import _ready
from test_optional_awareness_projection import _root, _accepted, _owner, _projection


@pytest.fixture
def notifications(monkeypatch):
    calls, finished = [], threading.Event()
    schedule, drain = candidate_maintenance.schedule_candidate_catchup, candidate_maintenance._drain_candidate

    def complete(root):
        try:
            drain(root)
        finally:
            finished.set()

    def notify(bus, seq):
        # These physical and SQL writes would refuse while the original
        # publication boundary still held any of its resources.
        with _store_lock(bus.log.path.parent / "wire", blocking=False), bus.log.locked(blocking=False):
            pass
        with sqlite3.connect(bus.log.path.parent / "coordination.sqlite3", timeout=0) as db:
            db.execute("BEGIN IMMEDIATE")
            db.rollback()
        calls.append(seq)
        schedule(bus, seq)

    monkeypatch.setattr(messaging, "schedule_candidate_catchup", lambda *_: None)
    monkeypatch.setattr(candidate_maintenance, "_drain_candidate", complete)
    monkeypatch.setattr(candidate_maintenance, "schedule_candidate_catchup", notify)
    # The baseline has no notification at this owner at all.
    monkeypatch.setattr(claim_admission, "schedule_candidate_catchup", notify, raising=False)
    return calls, finished


def test_response_catches_up_and_supplies_recipient_awareness(tmp_path, notifications):
    calls, finished = notifications
    case = _ready(tmp_path, direct=True)
    try:
        index = WakeCandidateIndex(case.bus)
        assert index.maintain(rebuild=True)
        prepare_fenced_response(case.store, case.bus, case.fence, "Answer",
                                exact_target=case.reply_target, owner_witness=case.witness)
        result = publish_fenced_response(case.store, case.bus, case.fence,
                                        exact_target=case.reply_target, owner_witness=case.witness)
        _, receipt = result.value.require_published_evidence(case.reply_target)
        assert calls == [receipt.seq]
        assert finished.wait(5)
        initial = case.bus.log.read_delivery_cohort(case.root_id, receipt.seq)
        (assignment,) = accept_delivery_cohort(case.bus, case.root_id, receipt.seq, case.store).value.assignments
        sender = _owner(case.comms, "sender")
        awareness = _projection(index, case.store, sender, 0, receipt.seq)(initial, assignment, sender)
        assert awareness.complete
        assert awareness.selected[0].assignment.wire_seq == receipt.seq
        assert len(case.bus.log.full_history()) == 2
        # Read-only settlement never schedules maintenance or creates a row.
        resolve_existing_response(case.store, case.bus, case.fence,
                                  exact_target=case.reply_target, owner_witness=case.witness)
        assert calls == [receipt.seq]
    finally:
        case.close()


def test_membership_notices_notify_after_tag_transaction(tmp_path, notifications):
    calls, finished = notifications
    comms, store, index, _root_id = _root(tmp_path)
    try:
        comms.channels.update_tags("member000", add=frozenset({"new-tag"}))
        assert calls == [1]
        assert finished.wait(5)
        finished.clear()
        comms.channels.replace_tags("member000", frozenset({"team"}))
        assert calls == [1, 2]
        assert finished.wait(5)
    finally:
        store.close()


def test_selected_claim_and_release_notify_after_their_boundaries(tmp_path, notifications):
    calls, finished = notifications
    comms, store, index, root_id = _root(tmp_path, recipients=1)
    try:
        initial, assignment = _accepted(comms, store, root_id, "member000", "Investigate")
        owner = _owner(comms, "member000")
        person = store.participants.get(stable_thread_lookup(owner.created_at))
        store.executions.create("claim-exec", ExecutionOrigin.WIRE, person.lookup,
                                owner.name, 1, sources=(SelectedSource(assignment.assignment_id, store.assignments, initial),))
        pending = store.executions.mark_pending("claim-exec", expected_revision=1).value
        store.attempts.start(AttemptStart("claim-exec", 1, owner.name, person.participant_generation,
                                         prepare_fence_token(), expected_execution_revision=pending.execution.revision,
                                         expected_pointer_revision=pending.pointer_revision))
        engaged = store.assignments.get(assignment.assignment_id)
        admission = WakeAdmission(wire_root_id=root_id, source_seq=initial.message.seq,
                                  source_message_id=initial.message.message_id, wake_assignment_id=assignment.assignment_id,
                                  wake_revision=engaged.revision, recipient_lookup=person.lookup, execution_id="claim-exec",
                                  operation_id="f" * 32, owner_admission_generation=owner.active_turn.admission_generation,
                                  turn_id=owner.active_turn.id, participant_generation=person.participant_generation,
                                  attempt_ordinal=1)
        resource = tmp_path / "module.py"
        resource.write_text("value = 1\n")
        claim = claim_admission.publish_selected_resource_claim(comms, store, admission, owner.name, ExistingFileClaim(resource))
        assert calls == [claim.seq]
        assert finished.wait(5)
        finished.clear()
        claim_admission.release_selected_resources(comms, store, admission, owner.name, (claim,))
        assert calls == [claim.seq, claim.seq + 1]
        assert finished.wait(5)
        assert not comms.bus.log.claim_projection()
    finally:
        store.close()


def test_omission_logs_original_read_error_and_index_path(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(messaging, "schedule_candidate_catchup", lambda *_: None)
    comms, store, index, root_id = _root(tmp_path, recipients=1)
    try:
        initial, assignment = _accepted(comms, store, root_id, "member000", "first")
        owner = _owner(comms, "member000")
        assert index.maintain(rebuild=True)
        later = comms.messaging.send_initial_cohort("sender", "member000", "later")
        omitted = _projection(index, store, owner, 0, later.seq)(initial, assignment, owner)
        assert not omitted.complete
        (record,) = [r for r in caplog.records if r.name.endswith("optional_awareness_projection")]
        assert str(index.path) in record.getMessage()
        assert str(record.exc_info[1]) == "candidate projection is stale or unrelated"
        assert "Optional awareness omitted (ProjectionUnavailableError)" in omitted.segments(16384)[0].text()
    finally:
        store.close()
