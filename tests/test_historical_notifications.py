"""Authored archived deliveries use recorded owners, not live availability."""

from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from agent_comms.agent_activity import RecordedRecipientActivity, UnavailableRecipientActivity
from agent_comms.assignment_states import EngagedAssignment
from agent_comms.comms import Comms
from agent_comms.coordination_schema import COORDINATION_SCHEMA_VERSION
from agent_comms.historical_views import HistoricalDisplay
from agent_comms.read_ledger import ReadLedger
from agent_comms.thread_identity import ThreadRole
from agent_comms.threads import Thread
from agent_comms.wake_policy import FullWake


@pytest.fixture
def archived_notification(tmp_path):
    old, live = Comms(tmp_path / "old"), Comms(tmp_path / "live")
    for comms in (old, live):
        comms.registry.declare(Thread("sender", frozenset(), str(tmp_path), created_at=10.0))
        comms.registry.declare(Thread("reader", frozenset(), str(tmp_path),
                                     created_at=11.0,
                                     role=ThreadRole.AGENT if comms is old else ThreadRole.USER))
        comms.registry.declare(Thread("agent", frozenset(), str(tmp_path),
                                     created_at=12.0 if comms is old else 212.0))
    root_id = old.messaging.initialize_private_initial_protocol()
    messages = (
        old.messaging.send_initial_cohort("sender", "agent", "authored pending delivery"),
        old.messaging.send_initial_cohort("sender", "reader", "painted historical delivery"),
        old.messaging.send_initial_cohort("sender", "reader", "unpainted historical delivery"),
    )
    source = live.views.attach_history(old.root)
    return old, live, source, messages, root_id


def test_recorded_notifications_do_not_decode_registry_or_acquire_activity(archived_notification, monkeypatch):
    old, live, source, messages, _root_id = archived_notification
    preserved = {path: path.read_bytes() for path in (old.root / "registry.json", old.root / "bus.jsonl")}

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Recorded notification borrowed live registry/activity")

    monkeypatch.setattr("agent_comms.registration.Registration.snapshot", forbidden)
    monkeypatch.setattr("agent_comms.agent_activity.AgentActivity.observe_recipients", forbidden)
    results = source.notification_references(live.bus.history, tuple(message.reference for message in messages))
    notification, = results[messages[0].seq, messages[0].message_id]
    assert notification.recipient == "agent"
    assert notification.state == "Pending" and not notification.busy
    assert "stopped" not in notification.detail
    assert notification.displayed_to == ()
    assert not (Path(source.root) / "coordination.sqlite3").exists()
    assert all(path.read_bytes() == original for path, original in preserved.items())
    live.bus.history.path.write_text("[]")
    with pytest.raises(ValueError, match="detached"):
        source.notification_references(live.bus.history, (messages[0].reference,))


def test_recorded_display_is_sparse_and_exact_incarnation(archived_notification):
    _old, live, source, messages, _root_id = archived_notification
    basis = live.bus.reads.capture(
        "reader", messages[1:], live.registry.snapshot(), Path(source.root) / "bus.jsonl",
        conversation_snapshot=source.provenance,
    )
    HistoricalDisplay(source, basis.select((messages[1].seq,))).acknowledge(live.bus.history, live.registry)
    results = source.notification_references(live.bus.history, tuple(message.reference for message in messages[1:]))
    painted, = results[messages[1].seq, messages[1].message_id]
    unpainted, = results[messages[2].seq, messages[2].message_id]
    assert painted.displayed_to == (source.provenance.require("reader").incarnation,)
    assert unpainted.displayed_to == ()
    assert not live.bus.reads.seen_sequences("reader", live.registry.snapshot())
    ledger = ReadLedger(Path(source.root) / ReadLedger.filename)
    document = ledger.read()
    reused = replace(basis, viewer_created_at=111.0)
    with pytest.raises(ValueError, match="viewer changed"):
        HistoricalDisplay(source, reused).acknowledge(live.bus.history, live.registry)
    assert ledger.read() == document
    newer_namespace = replace(source.provenance, threads={
        **source.provenance.threads,
        "reader": replace(source.provenance.require("reader"), created_at=111.0),
    })
    assert ledger.historical_displayed_recipient(messages[1], painted.recipient_identity,
                                                newer_namespace, document=document) == ()


def test_recorded_assignment_schema_refusal_is_read_only(archived_notification):
    _old, live, source, messages, _root_id = archived_notification
    database = Path(source.root) / "coordination.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(f"PRAGMA user_version = {COORDINATION_SCHEMA_VERSION - 1}")
    database.chmod(0o600)
    original = database.read_bytes()
    with pytest.raises(ValueError, match="unsupported schema"):
        source.notification_references(live.bus.history, (messages[0].reference,))
    assert database.read_bytes() == original


def test_recorded_engagement_is_not_an_active_turn(archived_notification):
    old, _live, _source, messages, root_id = archived_notification
    recipient, = old.bus.log.read_delivery_cohort(root_id, messages[0].seq).audience.recipients
    assignment = EngagedAssignment.build(FullWake(), "authored-execution", "sender")
    recorded = assignment.notification(recipient, observation=RecordedRecipientActivity(), updated_at_ms=1)
    assert recorded.state == "Response selected" and not recorded.busy
    assert "completion is not recorded" in recorded.detail
    live = assignment.notification(recipient, observation=UnavailableRecipientActivity(), updated_at_ms=1)
    assert live.state == "Paused" and "no matching active turn" in live.detail


def test_live_cli_ack_does_not_become_archived_ack(archived_notification):
    from agent_comms.field_codec import FieldCodec
    from agent_comms.agent_activity import RecipientActivity
    from agent_comms.thread_execution import ExternalThreadExecution

    old, live, source, messages, _root_id = archived_notification
    old.registry.declare(replace(old.registry.require("agent"), execution=ExternalThreadExecution))
    assert old.bus.mark_delivered("agent") == 1
    current, = old.views.message_notifications_for_references((messages[0].reference,))[
        messages[0].seq, messages[0].message_id
    ]
    assert current.state == "Checked by CLI" and not current.displayed_to
    archived, = source.notification_references(live.bus.history, (messages[0].reference,))[
        messages[0].seq, messages[0].message_id
    ]
    assert archived.state == "Pending" and not archived.displayed_to
    assert FieldCodec.decode(RecipientActivity, FieldCodec.encode(RecordedRecipientActivity())) == RecordedRecipientActivity()
