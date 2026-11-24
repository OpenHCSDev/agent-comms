"""Read real persisted notification assignments without scheduling a model."""
import os
from dataclasses import replace

from agent_comms.cli_bridges.claude import ClaudeBridge
from agent_comms.cli_bridges.harness_delivery_states import DeliveredOutcome, ObservedMonitor
from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.database import CoordinationStore
from agent_comms.coordination.tables.harness_deliveries import HarnessDelivery
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.messaging.human_read_cut import HumanReadCut
from agent_comms.messaging.message_notification import MessageNotification
from agent_comms.messaging.notification_evidence import AllOutcomes
from agent_comms.messaging.notification_reads import LiveNotificationRead
from agent_comms.nominal.field_codec import TurnId
from agent_comms.notification_assignment import NotificationAssignment, NotificationSelection
from agent_comms.planning.goal_actions import SetGoalAction
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.activity import ActivityState
from agent_comms.registry.thread_status import (
    ConnectedThreadStatus,
    NoBridgeThreadStatus,
    StoppedThreadStatus,
)
from agent_comms.registry.threads import ExternalThread, NativeThread
from conftest import installed_comms
from test_coordinated_runtime import _root, tmp_path  # noqa: F401
from transcript_record_fixture import transcript_record


def test_channel_assignments_are_visible_from_agent_view(tmp_path):  # noqa: F811
    _path, _root_id, comms, initial, _people = _root(tmp_path)
    message = initial.message
    per_message = comms.views.message_notifications_for_references(AllOutcomes(), (message.reference,))
    receipts = comms.views.thread_presentations(('beta',)).require('beta').notifications
    assert len(receipts) == 1
    acknowledgement = receipts[0]
    assert acknowledgement.message == initial.envelope
    assert acknowledgement.message.target == '#team'
    assert replace(acknowledgement, message=None) in per_message.outcomes[(message.seq, message.message_id)]
    assert not comms.registry.require('beta').executing


def test_new_reader_has_no_fabricated_receipts(tmp_path):  # noqa: F811

    root = tmp_path / 'new'
    comms = installed_comms(root)
    comms.registry.declare(NativeThread('reader', frozenset(), Checkout(tmp_path)))
    assert comms.views.thread_presentations(('reader',)).require('reader').notifications == ()


def test_human_display_stays_captured_until_the_next_notification_pass(tmp_path):  # noqa: F811
    root, _root_id, comms, _initial, _people = _root(tmp_path)
    human = comms.messaging.user_identity(Checkout(tmp_path))
    message = comms.messaging.send('beta', human.name, 'A direct human read').message
    source = comms.bus.bus.delivery(message.seq)
    assigned = NotificationAssignment.read(
        root, NotificationSelection.of(AllOutcomes(), (source,)))
    cut = HumanReadCut(root)
    key = message.seq, message.message_id
    before = MessageNotification.delivery_window(cut, comms.registry, assigned)
    assert before.outcomes[key] == ()

    comms.bus.mark_read_through(
        human.name, message.seq, target=CommandTarget.named('beta'))
    retained = MessageNotification.delivery_window(cut, comms.registry, assigned)
    assert retained == before
    refreshed = MessageNotification.delivery_window(HumanReadCut(root), comms.registry, assigned)
    row, = refreshed.outcomes[key]
    assert row.displayed_to and row.recipient_identity.recipient_lookup == human.incarnation.lookup


def test_live_turn_never_presents_ready_between_activity_events(tmp_path):  # noqa: F811

    _path, _root_id, comms, _initial, _people = _root(tmp_path)
    lease = comms.agents.begin_turn('beta', TurnId('3c5469ccca7ada4828e500d32e2025e7')).turn_lease
    comms.agents.set_activity('beta', ActivityState.IDLE)
    view = next(v for v in comms.views.thread_views() if v.thread.name == 'beta')
    assert view.presentation.status.is_working()
    state = comms.registry.require('beta').turn_state
    assert state.active.id == lease.turn_id
    assert state.active.phase.summary in view.presentation.summary
    comms.agents.finish_turn(lease)
    view = next(v for v in comms.views.thread_views() if v.thread.name == 'beta')
    assert not view.presentation.status.is_working()


def test_individual_view_keeps_alias_goal_and_active_owner_semantics(tmp_path):  # noqa: F811

    _path, _root_id, comms, _initial, _people = _root(tmp_path)
    comms.goals.update_goal('beta', SetGoalAction(text='Finish native delivery'))
    comms.registry.rename('beta', 'renamed')
    for name in ('beta', 'renamed'):
        individual = comms.views.thread_presentations((name,)).require(name)
        view = next(v for v in comms.views.thread_views() if v.thread.name == 'renamed')
        assert individual.title == view.presentation.title
        assert individual.summary == view.presentation.summary
        assert individual.read_identity.thread.name == view.thread.name
    lease = comms.agents.begin_turn(
        'renamed', TurnId('6d520fcb678df3e43b0fa663d3e618be')
    ).turn_lease
    comms.agents.set_activity('renamed', ActivityState.IDLE)
    assert comms.views.thread_presentations(('beta',)).require('beta').status.is_working()
    state = comms.registry.require('renamed').turn_state
    assert state.active.id == lease.turn_id
    assert state.active.phase.summary in comms.views.thread_presentations(('beta',)).require('beta').summary
    comms.agents.finish_turn(lease)
    assert not comms.views.thread_presentations(('beta',)).require('beta').status.is_working()


def test_a_live_batch_beyond_one_window_chunk_keeps_every_message(tmp_path):  # noqa: F811
    """The live operation reads every 120-reference chunk into one window; no batch cap."""

    _path, _root_id, comms, initial, _people = _root(tmp_path)
    messages = [initial.message, *(
        comms.messaging.send('sender', '#team', f'Batch {number}').message
        for number in range(NotificationSelection.window_limit + 5)
    )]
    window = LiveNotificationRead(tuple(message.reference for message in messages)).read(comms)
    assert set(window.outcomes) == {(message.seq, message.message_id) for message in messages}
    for message in (messages[0], messages[-1]):
        alone = LiveNotificationRead((message.reference,)).read(comms)
        key = (message.seq, message.message_id)
        assert window.outcomes[key] == alone.outcomes[key] and window.outcomes[key]


def test_offline_unseen_recipients_are_omitted_while_online_and_seen_or_answered_stay(tmp_path):  # noqa: F811
    comms = installed_comms(tmp_path / "wire")
    project = Checkout(tmp_path)
    comms.registry.declare(NativeThread("sender", frozenset({"team"}), project))
    comms.registry.declare(
        ExternalThread(
            "online-unread",
            frozenset({"team"}),
            project,
            process_identity=ProcessIdentity.capture(os.getpid()),
        ),
        ConnectedThreadStatus(),
    )
    comms.registry.declare(
        NativeThread("stopped-unread", frozenset({"team"}), project), StoppedThreadStatus(),
    )
    comms.registry.declare(
        ExternalThread("nobridge-unread", frozenset({"team"}), project), NoBridgeThreadStatus(),
    )
    comms.registry.declare(
        ExternalThread("offline-read", frozenset({"team"}), project), NoBridgeThreadStatus(),
    )
    comms.registry.declare(
        NativeThread("offline-answered", frozenset({"team"}), project), StoppedThreadStatus(),
    )
    sent = comms.messaging.send("sender", "#team", "Check receipts").message
    source = comms.bus.bus.delivery(sent.seq)
    by_name = {recipient.canonical_thread: recipient for recipient in source.recipients}

    with Coordination(str(CoordinationStore.path_in(comms.root))) as store, \
            store.session.writing() as db:
        attempt = HarnessDelivery(
            ClaudeBridge,
            by_name["offline-read"].recipient_lookup,
            sent.message_id,
            sent.seq,
            DeliveredOutcome,
            ObservedMonitor(transcript_record("complete-original"), sent.timestamp),
        )
        assert attempt.begin(store.assignments, db)
        attempt.record(store.assignments, db)

    comms.messaging.send("offline-answered", "#team", "Got it", answers=(sent.reference,))

    window = comms.views.message_notifications_for_references(AllOutcomes(), (sent.reference,))
    shown = {item.recipient: item for item in window.outcomes[(sent.seq, sent.message_id)]}
    assert set(shown) == {"online-unread", "offline-read", "offline-answered"}
    assert shown["online-unread"].label == "Sent"
    assert shown["offline-read"].state.is_consumed()
    assert shown["offline-answered"].label.startswith("Answered ")
