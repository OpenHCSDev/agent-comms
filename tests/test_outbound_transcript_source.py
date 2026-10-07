"""Whole conversations derive their original frozen wire sources, in both directions."""

from agent_comms import invoke_tool
from agent_comms.comms import wire
from agent_comms.coordination_errors import StaleRevision
from agent_comms.assignment_states import IgnoredAssignment
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.coordinator import Coordination
from agent_comms.notification_assignment import NotificationAssignment
from agent_comms.errors import RelationViolationError
from agent_comms.message_reference import MessageReference
from agent_comms.presentation import MessageNotification
from agent_comms.wire_log import WireLog
from agent_comms.threads import Thread
from agent_comms.transcripts import TranscriptCursor
from test_coordinated_runtime import _root, tmp_path  # noqa: F401

import pytest


def test_original_read_contention_is_unavailable_then_same_receipts_return(tmp_path):  # noqa: F811
    import sqlite3
    from agent_comms.coordination_errors import CoordinationReadUnavailable
    from agent_comms.native_runtime_input import NativeRuntimeInput
    from agent_comms.recovery_gateway import _snapshot
    from agent_comms.recovery_projection import (
        UnavailableRecoveryProjection, read_recovery_projection,
    )

    root, _root_id, comms, initial, _people = _root(tmp_path)
    database = root / "coordination.sqlite3"
    lookup = stable_thread_lookup(comms.registry.require("beta").created_at)
    predicate, parameters = "w.wire_seq=?", (initial.message.seq,)
    original = NotificationAssignment.select(root, predicate, parameters)
    wire_before = comms.bus.log.path.read_bytes()
    assert original
    with Coordination(str(database)) as writer:
        with writer.session.irreversible_admission():
            for read in (
                lambda: NotificationAssignment.select(root, predicate, parameters),
                lambda: _snapshot(root, database, "beta"),
            ):
                with pytest.raises(CoordinationReadUnavailable) as unavailable:
                    read()
                assert unavailable.value.__cause__.sqlite_errorcode & 0xFF in (
                    sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
                )
            with pytest.raises(CoordinationReadUnavailable):
                with NativeRuntimeInput._publication_read(root):
                    pytest.fail("Busy acquisition reached the native schema decoder")
            assert read_recovery_projection(
                database, owner_lookup=lookup, owner_thread="beta"
            ) == UnavailableRecoveryProjection("busy")
    assert NotificationAssignment.select(root, predicate, parameters) == original
    with NativeRuntimeInput._publication_read(root) as native_read:
        assert native_read is not None
    assert comms.bus.log.path.read_bytes() == wire_before


def test_gateway_uses_original_snapshot_and_one_metadata_read(tmp_path, monkeypatch):  # noqa: F811
    import json
    import threading
    import time
    from contextlib import contextmanager
    from agent_comms.coordination_database import CoordinationStore
    from agent_comms.recovery_gateway import _snapshot
    from agent_comms.recovery_projection import RecoverySelection
    from agent_comms.field_codec import FieldCodec

    root, _root_id, comms, _initial, _people = _root(tmp_path)
    database = root / "coordination.sqlite3"
    lookup = stable_thread_lookup(comms.registry.require("beta").created_at)
    commit_requested = threading.Event()
    committed = threading.Event()
    errors, statements = [], []
    project = RecoverySelection.project
    observe = CoordinationStore.observing
    encode = FieldCodec.encode
    captured, timings = [], {}

    # Instrument the original resource and callback, without substituting SQL,
    # schema, identities, projection results, or transaction ownership.
    @contextmanager
    def traced_read(*args, **kwargs):
        with observe(*args, **kwargs) as db:
            timings["read_started"] = time.monotonic()
            db.set_trace_callback(statements.append)
            yield db
        timings["read_released"] = time.monotonic()

    def writer():
        try:
            with Coordination(str(database)) as store:
                store.session._connection.set_trace_callback(
                    lambda sql: commit_requested.set() if sql == "COMMIT" else None
                )
                store.participants.advance_generation(
                    lookup, "renamed", expected_generation=1
                )
            timings["writer_committed"] = time.monotonic()
            committed.set()
        except BaseException as error:
            errors.append(error)

    changing = threading.Thread(target=writer)

    def during_commit(db, owner_lookup, owner_thread):
        changing.start()
        assert commit_requested.wait(2), errors
        assert not committed.is_set()
        result = project(db, owner_lookup, owner_thread)
        captured.append(result)
        return result

    def after_release(value, annotation=None):
        if captured and value is captured[0]:
            # The real writer must finish while the captured projection still
            # names beta. Encoding cannot need the old SQLite read lock.
            assert "read_released" in timings
            assert committed.wait(2)
            timings["encoding_started"] = time.monotonic()
        return encode(value, annotation)

    monkeypatch.setattr(CoordinationStore, "observing", staticmethod(traced_read))
    monkeypatch.setattr(RecoverySelection, "project", staticmethod(during_commit))
    monkeypatch.setattr(FieldCodec, "encode", staticmethod(after_release))
    try:
        original = json.loads(_snapshot(root, database, "beta"))
    finally:
        changing.join(3)
    assert not changing.is_alive() and not errors
    assert committed.is_set()
    assert original["availability"] == "available" and original["owner"] == "beta"
    assert sum("PRAGMA user_version" in sql for sql in statements) == 1
    assert sum("schema_meta" in sql and sql.startswith("SELECT") for sql in statements) == 1
    print("Original private recovery read:", json.dumps({
        "snapshot_seconds": timings["read_released"] - timings["read_started"],
        "encoding_started_after_writer_commit": committed.is_set(),
        "writer_release_seconds": timings["writer_committed"] - timings["read_released"],
    }))
    monkeypatch.setattr(RecoverySelection, "project", project)
    current = json.loads(_snapshot(root, database, "renamed"))
    assert current["availability"] == "available" and current["owner"] == "renamed"


@pytest.mark.parametrize('direct', [False, True])
def test_saved_notification_uses_exact_owner_drain_readiness(tmp_path, direct):  # noqa: F811
    from dataclasses import replace
    from agent_comms.activity import StoppedDrainDiagnostic

    _path, _root_id, comms, initial, _people = _root(tmp_path, direct=direct)
    owner = comms.registry.snapshot().owner_identity('beta')
    diagnostic = StoppedDrainDiagnostic(owner, 'NativePiUnavailable', 'Original outcome uncertain')
    assert comms.agents.set_drain_diagnostic('beta', owner, diagnostic)
    notice = next(item for item in comms.views.recent_notifications('sender')
                  if item.recipient=='beta')
    assert notice.state=='Waiting for recovery'
    assert diagnostic.summary in notice.detail
    assert notice.message.reference==initial.message.reference
    assert not notice.busy
    # A diagnostic from another owner generation cannot block a current
    # participant; neither its text nor a running PID grants that relation.
    foreign = replace(diagnostic,owner=replace(owner,generation=owner.generation+1))
    activity = comms.agents.activity_of('beta').source_event()
    comms.agents.activity.emit(replace(activity,diagnostic=foreign))
    current = next(item for item in comms.views.recent_notifications('sender')
                   if item.recipient=='beta')
    assert current.state=='Pending'


def test_frozen_unhandled_delivery_projects_recovery_without_claiming(tmp_path):  # noqa: F811
    from agent_comms.activity import StoppedDrainDiagnostic
    from agent_comms.coordination_tables.assignments import WakeAssignment

    root, _root_id, comms, _initial, _people = _root(tmp_path)
    owner = comms.registry.snapshot().owner_identity('beta')
    diagnostic = StoppedDrainDiagnostic(owner, 'NativePiUnavailable', 'Original uncertain outcome')
    assert comms.agents.set_drain_diagnostic('beta', owner, diagnostic)
    message = comms.messaging.send_user_message('#team', 'Independent saved original',
                                               worktree=str(tmp_path))
    with Coordination(str(root/'coordination.sqlite3')) as store:
        before = WakeAssignment.select(store.session._connection)
    wire_before = comms.bus.log.path.read_bytes()
    notices = comms.views.message_notifications((message,))[message.seq,message.message_id]
    beta = next(notice for notice in notices if notice.recipient=='beta')
    assert beta.state=='Waiting for recovery' and diagnostic.summary in beta.detail
    assert beta.recipient_identity.recipient_lookup==stable_thread_lookup(
        comms.registry.require('beta').created_at)
    with Coordination(str(root/'coordination.sqlite3')) as store:
        assert WakeAssignment.select(store.session._connection)==before
    assert not any(row.wire_seq==message.seq for row in before)
    assert comms.bus.log.path.read_bytes()==wire_before
    # The immutable source's recipient survives rename; current readiness must
    # join its original birth rather than manufacture a new membership/claim.
    comms.registry.rename('beta','renamed')
    renamed = comms.views.message_notifications((message,))[message.seq,message.message_id]
    assert {item.recipient_identity for item in renamed}=={item.recipient_identity for item in notices}


def test_notification_window_preserves_exact_receipts_and_rejects_duplicates(tmp_path):  # noqa: F811
    root, _root_id, comms, initial, _people = _root(tmp_path)
    records = NotificationAssignment.select(root, "w.wire_seq=?", (initial.message.seq,))
    assert records
    projected = tuple(NotificationAssignment.for_deliveries((initial,), records))
    assert projected[0][0] is initial
    assert any(outcome is records[0] for outcome in projected[0][1])
    with pytest.raises(ValueError, match="multiple handling receipts"):
        tuple(NotificationAssignment.for_deliveries((initial,), (*records, records[0])))
    # Duplicate receipts outside the acquired deliveries do not change their result.
    assert tuple(NotificationAssignment.for_deliveries((), (*records, records[0]))) == ()


def test_publication_intent_joins_original_sender_and_target_only(tmp_path):  # noqa: F811
    from dataclasses import replace
    from agent_comms.field_codec import FieldCodec
    from agent_comms.routing import MessageRoute, TurnRouting
    from agent_comms.transcript_receipts import AssignedTranscriptSource

    _path, _root_id, comms, initial, _people = _root(tmp_path)
    reply = comms.messaging.send_message("beta", "sender", "First original reply")
    source = AssignedTranscriptSource.for_thread(
        comms.root, comms.registry.require("beta"), comms.bus.log
    )
    original = source.rows("w.seq=?", (reply.seq,))
    lookup = stable_thread_lookup(source.recipient.created_at)
    intent = TurnRouting((initial.message.reference,), MessageRoute("beta", ("sender", "alpha")))
    # A later send can fail; this first committed subset stays a valid relation.
    intent.require_publications(original, lookup)
    reference_only = FieldCodec.encode(intent)["requests"]
    assert reference_only == [FieldCodec.encode(initial.message.reference)]
    assert TurnRouting.from_wire(intent.to_wire()) == intent
    with pytest.raises((TypeError, ValueError)):
        TurnRouting.from_wire({"requests": [initial.message.to_wire()], "reply": None})
    with pytest.raises(RelationViolationError):
        intent.require_publications(original, "foreign-incarnation")
    with pytest.raises(RelationViolationError):
        replace(intent, reply=MessageRoute("beta", ("alpha",))).require_publications(original, lookup)
    with pytest.raises(RelationViolationError):
        replace(intent, publications=(reply.reference,)).require_publications(original, lookup)


def test_open_sender_source_advances_without_a_native_tool_copy(tmp_path):  # noqa: F811
    _path, _root_id, comms, initial, _people = _root(tmp_path)
    before = comms.transcripts.capture_page_read("beta")
    receipt = invoke_tool(
        comms,
        "comms_send",
        {
            "from": "beta",
            "to": "sender",
            "body": "Original outgoing ownership reply",
        },
    )
    message = comms.bus.log.message_by_id(receipt["id"])
    after = comms.transcripts.capture_page_read("beta")
    assert after.identity.receipt_frontier.sequence == message.seq
    assert not before.current()
    with pytest.raises(StaleRevision):
        before.read()
    events = after.read().events
    assert [event.text for event in events] == [initial.message.body, message.body]
    assert [event.timestamp for event in events] == [initial.message.timestamp, message.timestamp]
    sent = events[-1]
    assert sent.declared_name == "sent"
    assert sent.source == message.reference
    assert sent.routing.reply.targets == (message.target,)
    # Renaming cannot reassign the old sender's source to today's spelling.
    comms.registry.rename("beta", "renamed")
    reopened = wire(comms.root).transcripts.capture_page_read("renamed").read()
    assert reopened.events == events
    assert comms.bus.log.full_history()[-1] == message


def test_original_target_handling_revokes_open_sender_read_without_new_message(
    tmp_path,
):  # noqa: F811
    root, _root_id, comms, initial, _people = _root(tmp_path)
    message = initial.message
    before = comms.transcripts.capture_page_read("sender")
    sender_page = before.read()
    target = comms.registry.require("beta")
    lookup = stable_thread_lookup(target.created_at)
    receipts = comms.views.recent_notifications("sender")
    beta = next(item for item in receipts if item.recipient_identity.recipient_lookup == lookup)
    assert beta.message.reference == message.reference and beta.state == "Pending"
    assignments = NotificationAssignment.select(
        root, "w.wire_seq=? AND w.recipient_lookup=?", (message.seq, lookup)
    )
    original = assignments[0].assignment
    # Both views are already open. Later messages move this original beyond the
    # bounded recent-notification window before its handling changes.
    for index in range(6):
        invoke_tool(
            comms,
            "comms_send",
            {
                "from": "sender",
                "to": "beta",
                "body": f"Later original {index}",
            },
        )
    open_sender = comms.views.thread_presentation("sender")
    open_recipient = comms.views.thread_presentation("beta")
    assert all(item.message.reference != message.reference for item in open_sender.notifications)
    before = comms.transcripts.capture_page_read("sender")
    sender_page = before.read()
    wire_before = comms.bus.log.path.read_bytes()
    with Coordination(str(root / "coordination.sqlite3")) as store:
        published_revision = comms.views.revision().files
        store.assignments.transition_preengagement(
            original.assignment_id, IgnoredAssignment, expected_revision=original.revision
        )
        assert comms.views.revision().files != published_revision
    after = comms.transcripts.capture_page_read("sender")
    assert not before.current()
    assert before.content_current()
    assert before.read() == sender_page
    assert after.identity.receipt_frontier == before.identity.receipt_frontier
    assert after.read() == sender_page
    assert comms.bus.log.path.read_bytes() == wire_before
    sender = comms.views.thread_presentation("sender")
    recipient = comms.views.thread_presentation("beta")
    assert sender.notifications == open_sender.notifications
    assert recipient.notifications == open_recipient.notifications
    assert sender.read_identity != open_sender.read_identity
    assert recipient.read_identity != open_recipient.read_identity
    assert sender.read_identity == after.identity
    outcomes = comms.views.message_notifications((message,))[message.seq, message.message_id]
    assert comms.views.message_notifications_for_references((message.reference,)) == {
        (message.seq, message.message_id): outcomes,
    }
    with pytest.raises(RelationViolationError):
        comms.views.message_notifications_for_references(
            (MessageReference(message.seq + 1, message.message_id),)
        )
    assert comms.views.message_notifications_for_references(
        (message.reference,) * (MessageNotification.window_limit + 1)
    ) == {(message.seq, message.message_id): outcomes}
    target_outcome = next(
        item for item in outcomes if item.recipient_identity.recipient_lookup == lookup
    )
    assert target_outcome.state == "Checked — no response"


def test_unrelated_canonical_append_keeps_open_source_content(tmp_path):  # noqa: F811
    _path, _root_id, comms, _initial, _people = _root(tmp_path)
    before = comms.transcripts.capture_page_read("beta")
    page = before.read()
    comms.messaging.send_message("sender", "alpha", "Unrelated original")
    after = comms.transcripts.capture_page_read("beta")
    assert before.identity.bus_revision != after.identity.bus_revision
    assert before.identity.receipt_frontier == after.identity.receipt_frontier
    assert not before.current()
    assert before.content_current()
    assert before.read() == page
    assert before.identity.content_identity == after.identity.content_identity
    assert hash(before.identity.content_identity) == hash(after.identity.content_identity)
    comms.messaging.send_message("sender", "beta", "Related original")
    assert not before.content_current()


def test_open_source_rejects_replaced_wire_inode(tmp_path):  # noqa: F811
    _path, _root_id, comms, _initial, _people = _root(tmp_path)
    before = comms.transcripts.capture_page_read("beta")
    replacement = comms.bus.log.path.with_suffix(".replacement")
    replacement.write_bytes(comms.bus.log.path.read_bytes())
    replacement.replace(comms.bus.log.path)
    with pytest.raises(RelationViolationError):
        before.content_current()


def test_open_thread_shares_original_notification_and_frontier_read(tmp_path, monkeypatch):  # noqa: F811
    _path, _root_id, comms, initial, _people = _root(tmp_path)
    verify = WireLog.verify_before_read_unlocked
    barriers = []

    def observed(log):
        if log.path == comms.bus.log.path:
            barriers.append(True)
        return verify(log)

    monkeypatch.setattr(WireLog, "verify_before_read_unlocked", observed)
    view = comms.views.thread_presentation("beta")
    assert len(barriers) == 1
    assert view.read_identity.receipt_frontier.sequence == initial.message.seq
    assert view.notifications[0].message.reference == initial.message.reference
    assert view.notifications[0].recipient_identity.recipient_lookup == stable_thread_lookup(
        view.read_identity.thread.created_at
    )


def test_published_source_binding_keeps_exact_original_request(tmp_path):  # noqa: F811
    _path, _root_id, comms, _initial, _people = _root(tmp_path)
    identity = comms.views.thread_presentation("beta").read_identity
    assert comms.transcripts.bind_page_read("beta", identity).identity is identity
    with pytest.raises(StaleRevision, match="original thread"):
        comms.transcripts.bind_page_read("alpha", identity)
    with pytest.raises(StaleRevision, match="another root"):
        wire(tmp_path / "other").transcripts.bind_page_read("beta", identity)
    with pytest.raises(StaleRevision, match="page window"):
        comms.transcripts.bind_page_read(
            "beta", identity,
            before=TranscriptCursor(identity.session_file, 0, identity.receipt_frontier),
        )
    comms.registry.rename("beta", "renamed")
    assert comms.transcripts.bind_page_read("renamed", identity).identity is identity
    comms.registry.unregister("renamed")
    comms.registry.remove("renamed")
    comms.registry.declare(Thread("renamed", frozenset(), str(tmp_path)))
    with pytest.raises(StaleRevision, match="original thread"):
        comms.transcripts.bind_page_read("renamed", identity)


def test_original_page_reuses_captured_frontier_between_admission_fences(tmp_path, monkeypatch):  # noqa: F811
    _path, _root_id, comms, initial, _people = _root(tmp_path)
    read = comms.transcripts.capture_page_read("beta")
    verify = WireLog.verify_before_read_unlocked
    barriers = []

    def observed(log):
        if log.path == comms.bus.log.path:
            barriers.append(True)
        return verify(log)

    monkeypatch.setattr(WireLog, "verify_before_read_unlocked", observed)
    page = read.read()
    # Before admission, one bounded page query, and after admission. The original
    # frontier is already certified: page preparation does not certify it again.
    assert len(barriers) == 3
    assert page.after.receipts == read.identity.receipt_frontier
    assert tuple(event.source for event in page.events) == (initial.message.reference,)


def test_original_page_rejects_relevant_append_during_preparation(tmp_path, monkeypatch):  # noqa: F811
    _path, _root_id, comms, _initial, _people = _root(tmp_path)
    read = comms.transcripts.capture_page_read("beta")
    prepare = comms.transcripts.thread_transcript_page

    def append_after_page(*args, **kwargs):
        page = prepare(*args, **kwargs)
        comms.messaging.send_message("sender", "beta", "Original concurrent append")
        return page

    monkeypatch.setattr(comms.transcripts, "thread_transcript_page", append_after_page)
    with pytest.raises(StaleRevision, match="during preparation"):
        read.read()


def test_original_window_uses_the_barriers_open_certificate(tmp_path, monkeypatch):  # noqa: F811
    import agent_comms.private_bus_checkpoint as checkpoint

    _path, _root_id, comms, initial, _people = _root(tmp_path)
    saved = checkpoint._saved
    reads = []

    def observed(connection):
        reads.append(True)
        return saved(connection)

    monkeypatch.setattr(checkpoint, "_saved", observed)
    view = comms.views.thread_presentation("beta")
    assert len(reads) == 1
    assert view.notifications[0].message.reference == initial.message.reference


def test_original_open_certificate_expires_with_canonical_lock(tmp_path):  # noqa: F811
    import sqlite3

    _path, _root_id, comms, initial, _people = _root(tmp_path)
    with comms.bus.log.certified_read() as source:
        assert source.connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert tuple(source.references((initial.message.reference,))) == (initial,)
    assert source.stream.closed
    with pytest.raises(sqlite3.ProgrammingError):
        source.connection.execute("SELECT 1")
    with pytest.raises(RelationViolationError, match="lock lifetime"):
        tuple(source.references((initial.message.reference,)))


def test_missing_certified_wire_cannot_be_an_empty_presentation(tmp_path):  # noqa: F811
    _path, _root_id, comms, _initial, _people = _root(tmp_path)
    comms.bus.log.path.unlink()
    with pytest.raises(RelationViolationError, match="inode is missing"):
        comms.views.thread_presentation("beta")


def test_bus_guard_preserves_the_callers_original_failure(tmp_path):  # noqa: F811
    _path, _root_id, comms, _initial, _people = _root(tmp_path)
    failure = OSError("Caller-owned failure after actual durability admission")
    with pytest.raises(OSError) as raised:
        with comms.bus.log.locked():
            raise failure
    assert raised.value is failure
