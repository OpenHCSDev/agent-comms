"""The append owner schedules optional work after the whole lock lifetime."""

from bus_history import read_history
from conftest import publish_initial
from contextvars import Context
from functools import partial
from tempfile import TemporaryDirectory
import sqlite3
import threading

import pytest

from agent_comms.bus_publication import AgentOrigin
import agent_comms.candidate_maintenance as candidate_maintenance
import agent_comms.selected_claim as selected_claim
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.coordination_response import (
    prepare_fenced_response, publish_fenced_response, resolve_existing_response,
)
from agent_comms.context_segments.optional_awareness import OmittedAwareness
from agent_comms.envelope_claim_transitions import ExistingFileClaim
from agent_comms.errors import RelationViolationError
from agent_comms.messages import InfoKind, Message
from agent_comms.store_files import StoreLock, _async_store_lock, _store_lock
from agent_comms.wake_candidate_index import (
    WakeCandidateIndex, RebuildIndex,
)
from test_claim_bus_read import record_context, selected_turn
from test_coordination_response import _ready
from test_optional_awareness_projection import _root, _accepted, _owner, _projection

_SCHEDULE = candidate_maintenance.schedule_candidate_catchup


def test_release_scope_closes_after_context_switch_and_stale_context_starts_fresh():
    calls = []
    entered, exited = Context(), Context()
    release = StoreLock.releasing()

    entered.run(release.__enter__)
    entered.run(lambda: StoreLock.current_release().callback(calls.append, "first"))
    exited.run(release.__exit__, None, None, None)
    assert calls == ["first"]

    def independent():
        with StoreLock.releasing():
            StoreLock.current_release().callback(calls.append, "second")
            assert calls == ["first"]

    entered.run(independent)
    assert calls == ["first", "second"]


def test_nested_release_scope_runs_only_after_outer_release():
    calls = []
    with StoreLock.releasing() as outer:
        with StoreLock.releasing() as inner:
            assert inner is outer
            StoreLock.current_release().callback(calls.append, "released")
        assert calls == []
    assert calls == ["released"]


@pytest.fixture
def notifications(monkeypatch):
    def activate(comms):
        calls, finished = [], threading.Event()
        drain = candidate_maintenance._drain_candidate

        def complete(root):
            try:
                drain(root)
            finally:
                finished.set()

        def notify(log, through_bytes):
            probed = []

            def probe():
                with (
                    _store_lock(comms._wire_lock_path, blocking=False),
                    log.locked(blocking=False),
                    _store_lock(comms.registry.store.path, blocking=False),
                ):
                    with sqlite3.connect(comms.root / "coordination.sqlite3", timeout=0) as db:
                        db.execute("BEGIN IMMEDIATE")
                        db.rollback()
                probed.append(True)

            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(2)
            assert not thread.is_alive() and probed == [True]
            calls.append(through_bytes)
            _SCHEDULE(log, through_bytes)

        monkeypatch.setattr(candidate_maintenance, "_drain_candidate", complete)
        monkeypatch.setattr(candidate_maintenance, "schedule_candidate_catchup", notify)
        return calls, finished

    return activate


def test_response_catches_up_and_supplies_recipient_awareness(tmp_path, notifications):
    case = _ready(tmp_path, direct=True)
    try:
        index = WakeCandidateIndex(case.bus.log)
        assert index.maintain(start=RebuildIndex())
        calls, finished = notifications(case.comms)
        prepare_fenced_response(case.store, case.bus, case.fence, "Answer",
                                exact_target=case.reply_target, owner_witness=case.witness)
        result = publish_fenced_response(case.store, case.bus, case.fence,
                                        exact_target=case.reply_target, owner_witness=case.witness)
        _, acknowledgement = result.value.require_published_evidence(case.reply_target)
        assert calls == [case.bus.log.path.stat().st_size]
        assert finished.wait(5)
        initial = case.bus.log.read_delivery_cohort(case.root_id, acknowledgement.seq)
        (assignment,) = accept_delivery_cohort(case.bus, case.root_id, acknowledgement.seq, case.store).value.assignments
        sender = _owner(case.comms, "sender")
        awareness = _projection(index, case.store, sender, 0, acknowledgement.seq)(initial, assignment, sender)
        assert awareness.complete
        assert awareness.selected[0].assignment.wire_seq == acknowledgement.seq
        segments = awareness.segments(16384)
        assert segments and not any(isinstance(segment, OmittedAwareness) for segment in segments)
        assert len(read_history(case.comms)) == 2
        resolve_existing_response(case.store, case.bus, case.fence,
                                  exact_target=case.reply_target, owner_witness=case.witness)
        assert calls == [case.bus.log.path.stat().st_size]
    finally:
        case.close()


def test_membership_notices_notify_after_tag_transaction(tmp_path, notifications):
    comms, store, _index, _root_id = _root(tmp_path)
    try:
        calls, finished = notifications(comms)
        comms.channels.update_tags("member000", add=frozenset({"new-tag"}))
        first_bytes = comms.bus.log.path.stat().st_size
        assert calls == [first_bytes]
        assert finished.wait(5)
        finished.clear()
        comms.channels.replace_tags("member000", frozenset({"team"}))
        assert calls == [first_bytes, comms.bus.log.path.stat().st_size]
        assert finished.wait(5)
    finally:
        store.close()


def test_selected_claim_and_terminal_release_notify_after_their_boundaries(tmp_path, monkeypatch, notifications):
    monkeypatch.setattr("test_claim_bus_read.TemporaryDirectory", partial(TemporaryDirectory, dir=tmp_path))
    monkeypatch.setattr("test_claim_bus_read.prepare_fence_token", lambda: "notification-token")
    with selected_turn() as (comms, store, selected, resource):
        calls, finished = notifications(comms)
        claim = selected_claim.publish_selected_resource_claim(comms, store, selected, "Alice", ExistingFileClaim(resource))
        first_bytes = comms.bus.log.path.stat().st_size
        assert calls == [first_bytes]
        assert finished.wait(5)
        finished.clear()
        from test_claim_bus_read import settle_failed
        settle_failed(store)
        assert calls == [first_bytes, comms.bus.log.path.stat().st_size]
        assert finished.wait(5)
        assert not comms.bus.log.claim_projection()
        release = read_history(comms)[-1].claim_transition.releases[0]
        assert (release.resource, release.generation) == (claim.resource, claim.generation)


def test_observations_advance_bytes_without_allocating_sequences(tmp_path, notifications):
    comms, store, index, root_id = _root(tmp_path, recipients=1)
    try:
        calls, finished = notifications(comms)
        record_context(comms, "member000", 1)
        first_bytes = comms.bus.log.path.stat().st_size
        assert calls == [first_bytes]
        assert finished.wait(5)
        checkpoint = index._verified_checkpoint(root_id)
        assert checkpoint.last_seq == 0 and checkpoint.byte_offset == first_bytes
        finished.clear()
        record_context(comms, "member000", 2)
        final_bytes = comms.bus.log.path.stat().st_size
        assert calls == [first_bytes, final_bytes] and final_bytes > first_bytes
        assert finished.wait(5)
        checkpoint = index._verified_checkpoint(root_id)
        assert checkpoint.last_seq == 0 and checkpoint.byte_offset == final_bytes
    finally:
        store.close()


def test_successful_append_is_not_lost_when_later_outer_work_fails(tmp_path, notifications):
    comms, store, _index, _root_id = _root(tmp_path, recipients=1)
    try:
        calls, finished = notifications(comms)
        with pytest.raises(ValueError, match="later work failed"):
            with _store_lock(comms._wire_lock_path):
                comms.bus.publisher.publish_ordinary(Message("sender", "member000", "committed", InfoKind), origin=AgentOrigin())
                assert not calls
                raise ValueError("later work failed")
        assert calls == [comms.bus.log.path.stat().st_size]
        assert finished.wait(5)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_joined_worker_append_waits_for_outer_async_lock(tmp_path, notifications):
    from contextvars import ContextVar
    from agent_comms.coordinator import Coordination

    comms, store, _index, _root_id = _root(tmp_path, recipients=1)
    try:
        calls, finished = notifications(comms)
        permission = ContextVar("unrelated_human_permission", default="unbound")
        permission.set("human-controller")

        def append():
            assert permission.get() == "unbound"
            return comms.bus.publisher.publish_ordinary(Message("sender", "member000", "joined", InfoKind), origin=AgentOrigin())

        async with _async_store_lock(comms._wire_lock_path):
            await Coordination.run_worker(append)
            assert calls == []
        assert calls == [comms.bus.log.path.stat().st_size]
        assert finished.wait(5)
    finally:
        store.close()


def test_uncertain_checkpoint_does_not_schedule_or_retry_append(tmp_path, monkeypatch, notifications):
    comms, store, _index, _root_id = _root(tmp_path, recipients=1)
    try:
        calls, _finished = notifications(comms)

        def fail(*_):
            raise OSError("controlled checkpoint failure")

        monkeypatch.setattr("agent_comms.private_bus_checkpoint.append_private_bus_checkpoint_unlocked", fail)
        with pytest.raises(RelationViolationError, match="outcome UNKNOWN"):
            publish_initial(comms, "sender", "member000", "one original")
        assert calls == []
        # The controlled fault happens AFTER the source fsync. It is not
        # absence, and no second attempt is made to repair or replay it.
        assert len(comms.bus.log.path.read_bytes().splitlines()) == 1
    finally:
        store.close()


def test_retiring_worker_does_not_delete_successor_notification(tmp_path, monkeypatch):
    comms, store, index, root_id = _root(tmp_path, recipients=1)
    try:
        publish_initial(comms, "sender", "member000", "first")
        assert index.maintain(start=RebuildIndex())
        root = index.log.path.parent
        candidate_maintenance._pending[root] = (index.log, index.log.path.stat().st_size)
        original_guard = candidate_maintenance._guard
        successor = []

        class InterleavedLock:
            def __enter__(self):
                original_guard.acquire()
                self.present = root in candidate_maintenance._pending

            def __exit__(self, *_):
                retired = self.present and root not in candidate_maintenance._pending
                original_guard.release()
                if retired and not successor:
                    successor.append(True)
                    publish_initial(comms, "sender", "member000", "successor")

        monkeypatch.setattr(candidate_maintenance, "_guard", InterleavedLock())
        monkeypatch.setattr(candidate_maintenance, "schedule_candidate_catchup", _SCHEDULE)
        monkeypatch.setattr(threading.Thread, "start", lambda _: None)
        candidate_maintenance._drain_candidate(root)
        assert successor
        assert candidate_maintenance._pending[root][1] == index.log.path.stat().st_size
        # Complete the retained successor without launching another daemon.
        candidate_maintenance._drain_candidate(root)
        assert root not in candidate_maintenance._pending
        assert index._verified_checkpoint(root_id).last_seq == 2
    finally:
        store.close()
