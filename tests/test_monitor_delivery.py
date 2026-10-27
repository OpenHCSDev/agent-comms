"""Session-owned parts, not stdout writes, confirm monitor delivery."""
from pathlib import Path
from agent_comms.repo import Checkout
from contextlib import asynccontextmanager, suppress
import asyncio
import os
import socket
import sqlite3
from contextlib import closing
from dataclasses import replace
from functools import partial
from html import escape
import json


import pytest

from conftest import declare_test_bus

from agent_comms.installer.practice import PracticeCopy
from agent_comms.locked_store import RootFile

from agent_comms.cli_bridges.claude import ClaudeBridge
from agent_comms.cli_bridges.claude_transcript import ClaudeJsonLines
from agent_comms.child_process import ProcessIdentity
from agent_comms.cli_bridges.monitor import MonitorPeer, MonitorService
from agent_comms.cli_bridges.claude_protocol import (
    ClaudeSession, ClaudeMonitorFrame, ForeignTaskNotification,
    ForeignAttachment, KnownTurn,
)
from agent_comms.cli_bridges.shared import (
    Binding, MonitorPart, ForeignMonitorFrame, Options, Watch, StoredBinding,
    MonitorObservation,
    GivenBinding, MONITOR_FRAME,
)
from agent_comms.harness_delivery_states import (
    DeliveredOutcome, EmittedOutcome, MismatchedMonitor, ObservedMonitor, ReceivingMonitor,
    UnconfirmedOutcome, UnrecordedMonitor,
)
from agent_comms.agent_inbox import TakenInputStart
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_tables.harness_deliveries import HarnessDelivery, HarnessObservedPart
from agent_comms.coordinator import Coordination
from agent_comms.field_codec import FieldCodec
from agent_comms.message_reference import BridgeStart
from agent_comms.runtime_updates import HiddenItem
from agent_comms.turn_item import NativeTurnId
from agent_comms.messages import InfoKind, Message
from agent_comms.threads import Thread


async def stored_attempts(bridge) -> tuple[HarnessDelivery, ...]:
    """This bridge's attempts for its recipient, as the coordinator stores them."""
    return await bridge.coordinator_read(lambda db: tuple(HarnessDelivery.select(
        db, bridge=type(bridge), recipient_lookup=bridge.recipient_lookup, order_by=("seq",))))


@pytest.mark.asyncio
async def test_empty_journal_starts_at_binding_and_ignores_user_reads(comms, tmp_path):
    declare_test_bus(comms, threads=tuple(
        Thread(name, frozenset(), Checkout(tmp_path)) for name in ('viewer', 'peer')
    ))
    before = comms.messaging.send('peer', 'viewer', 'before this session was bound').message
    state = tmp_path / 'bridge-state'
    with Binding.open(state) as db:
        binding = GivenBinding({
            'root': str(comms.root), 'bus_thread': 'viewer', 'session': 'session',
        }).bind(db).registered(comms).bind(db)
        selected = ClaudeBridge(Options(Watch(), StoredBinding(), state, state.parent), binding.address(), db)
        await selected.options.operation.prepare(selected)
        fresh = comms.messaging.send(
            'peer', 'viewer', 'after binding, already read in Toad').message
        comms.bus.mark_read_through('viewer', fresh.seq)
        user_reads = comms.bus.reads.read()
        bridge_socket, monitor_socket = socket.socketpair()
        bridge_peer = MonitorPeer(*await asyncio.open_connection(sock=bridge_socket))
        monitor_reader, monitor_writer = await asyncio.open_connection(sock=monitor_socket)
        service = MonitorService(selected, ProcessIdentity.capture(os.getpid()), lambda: None)
        attachment = asyncio.create_task(selected.reader.attach(service, bridge_peer))
        watching = None
        try:
            async with asyncio.timeout(15):
                await monitor_reader.readline()  # SessionReady, followed by the real probe.
                probe = MONITOR_FRAME.validate_json(await monitor_reader.readline())
                await probe.confirm(selected, ObservedMonitor('probe', 1.))
                watching = asyncio.create_task(selected.watch_child())
                line = (await monitor_reader.readline()).decode().rstrip('\n')
                for update in notification(line).updates(ClaudeSession()):
                    await update.observed(selected)
                row, = await stored_attempts(selected)
                assert row.reference == fresh.reference
                assert row.outcome is DeliveredOutcome
                assert isinstance(row.monitor, ObservedMonitor)
                assert comms.bus.reads.read() == user_reads
                unread = comms.messaging.send(
                    'peer', 'viewer', 'delivery records this model read').message
                line = (await monitor_reader.readline()).decode().rstrip('\n')
                for update in notification(line, record_id='second-delivery').updates(
                        ClaudeSession()):
                    await update.observed(selected)
                rows = await stored_attempts(selected)
                assert {row.reference for row in rows} == {fresh.reference, unread.reference}
                assert all(row.outcome is DeliveredOutcome for row in rows)
                assert all(isinstance(row.monitor, ObservedMonitor) for row in rows)
                # The agent's view drops what its harness took; the user's read changed nothing.
                assert [item.reference for item in comms.bus.inbox(
                    'viewer', after=TakenInputStart()).page.messages] == [before.reference]
        finally:
            if watching is not None:
                watching.cancel()
                with suppress(asyncio.CancelledError):
                    await watching
            monitor_writer.close()
            await monitor_writer.wait_closed()
            await attachment
            await bridge_peer.close()


@pytest.fixture
def bridge(comms, tmp_path):
    declare_test_bus(comms, threads=tuple(
        Thread(name, frozenset(), Checkout(tmp_path))
        for name in ('claude-code', 'viewer', 'peer')
    ))
    practice = PracticeCopy(comms.root, tmp_path / 'copied-bus')
    practice.include(tuple(RootFile.existing_in(practice.root)))
    (root, _measurement) = practice.path, practice.measurement
    state = tmp_path / 'bridge-state'
    binding = Binding(root=str(root), owner=comms.registry.require('viewer').incarnation,
                      thread='session', start=Binding.bus_head(str(root)))
    with Binding.open(state) as db:
        binding.bind(db)
        selected = ClaudeBridge(Options(Watch(), StoredBinding(), state, state.parent), binding.address(), db)
        asyncio.run(selected.options.operation.prepare(selected))
        yield selected


@pytest.fixture
def emitted_delivery(bridge):
    def emit(body, target='viewer'):
        message = bridge.comms.messaging.send('claude-code', target, body).message
        delivery = replace(bridge.attempt(message, EmittedOutcome),
                           monitor=ReceivingMonitor(len(bridge.incoming_frame(message).parts)))
        assert asyncio.run(bridge.coordinator_write(lambda _, db: delivery.first_write(db)))
        return message
    return emit


@pytest.mark.asyncio
async def test_busy_confirmation_keeps_monitor_serving_without_replaying(bridge):
    """Real SQLite contention on the confirmation write leaves this attempt uncertain only."""
    first, second = (bridge.comms.messaging.send('peer', 'viewer', body).message
                     for body in ('first message', 'second message'))
    path = CoordinationStore.path_in(bridge.comms.root)
    with Coordination(str(path)):
        pass
    left, right = socket.socketpair()
    peer = MonitorPeer(*await asyncio.open_connection(sock=left))
    reader, writer = await asyncio.open_connection(sock=right)
    service = MonitorService(bridge, ProcessIdentity.capture(os.getpid()), lambda: None)
    async with asyncio.TaskGroup() as tasks:
        attachment = tasks.create_task(bridge.reader.attach(service, peer))
        try:
            async with asyncio.timeout(20):
                await reader.readline()  # The real attachment's ready response.
                probe = MONITOR_FRAME.validate_json(await reader.readline())
                await probe.confirm(bridge, ObservedMonitor('probe', 1.))
                await bridge.accept(first)
                line = (await reader.readline()).decode().rstrip('\n')
                # The store's declared wait expires; no method or error is patched.
                with closing(sqlite3.connect(path, isolation_level=None)) as held:
                    held.execute('BEGIN EXCLUSIVE')
                    try:
                        for update in notification(line, record_id='busy-first').updates(ClaudeSession()):
                            await update.observed(bridge)
                    finally:
                        held.execute('ROLLBACK')
                uncertain, = await bridge.attempts(first.message_id)
                assert uncertain.outcome is EmittedOutcome
                assert uncertain.monitor == ReceivingMonitor(1)
                assert not attachment.done()
                await bridge.accept(second)
                line = (await reader.readline()).decode().rstrip('\n')
                for update in notification(line, record_id='healthy-second').updates(ClaudeSession()):
                    await update.observed(bridge)
                confirmed, = await bridge.attempts(second.message_id)
                assert confirmed.monitor == ObservedMonitor('healthy-second', 1792168805.123)
                assert confirmed.outcome is DeliveredOutcome
                assert not attachment.done()
                # Reopening starts after the journal's latest attempt, never at the
                # uncertain first message. Its original outcome remains untouched.
                assert await bridge.resume_after() == BridgeStart(second.reference)
                assert await bridge.attempts(first.message_id) == (uncertain,)
        finally:
            writer.close()
            await writer.wait_closed()
            attachment.cancel()
            await asyncio.gather(attachment, return_exceptions=True)
            await peer.close()


@pytest.mark.asyncio
async def test_renamed_reader_handoff_and_confirmation_share_the_prepared_owner(tmp_path):
    from bus_record_fixture import prepare_native_comms
    from conftest import accept_initial, publish_initial
    from agent_comms.assignment_states import DeferredAssignment, DeliveredAssignment
    from agent_comms.comms import wire
    from agent_comms.coordination_database import CoordinationStore
    from agent_comms.coordination_tables.assignments import WakeAssignment
    from agent_comms.coordinator import Coordination
    from agent_comms.thread_execution import ExternalThreadExecution

    comms = prepare_native_comms(wire(tmp_path / 'wire'))
    comms.registry.declare(Thread('peer', frozenset(), Checkout(tmp_path)))
    original = comms.registry.declare(Thread(
        'viewer', frozenset(), Checkout(tmp_path), execution=ExternalThreadExecution))
    lookup = original.incarnation.require_recorded().lookup
    root_id = comms.registry.store.prepare_native_root().namespace
    with Coordination(str(CoordinationStore.path_in(comms.root))) as store:
        store.participants.register(lookup, committed=True)
    state = tmp_path / 'bridge-state'
    with Binding.open(state) as db:
        binding = Binding(root=str(comms.root), owner=original.incarnation,
                          thread='session', start=Binding.bus_head(str(comms.root)))
        binding.bind(db)
        bridge = ClaudeBridge(Options(Watch(), StoredBinding(), state, state.parent), binding.address(), db)
        comms.registry.rename('viewer', 'renamed-viewer')
        await bridge.options.operation.prepare(bridge)
        assert bridge.binding.bus == 'renamed-viewer'
        assert bridge.binding.owner == original.incarnation
        with Coordination(str(CoordinationStore.path_in(comms.root))) as store:
            message = publish_initial(comms, 'peer', bridge.binding.bus, 'same reader').message
            accept_initial(comms.bus, root_id, message.seq, store)

        bridge_socket, monitor_socket = socket.socketpair()
        bridge_peer = MonitorPeer(*await asyncio.open_connection(sock=bridge_socket))
        monitor_reader, monitor_writer = await asyncio.open_connection(sock=monitor_socket)
        service = MonitorService(bridge, ProcessIdentity.capture(os.getpid()), lambda: None)
        attachment = asyncio.create_task(bridge.reader.attach(service, bridge_peer))
        accepting = None
        try:
            async with asyncio.timeout(15):
                await monitor_reader.readline()
                probe = MONITOR_FRAME.validate_json(await monitor_reader.readline())
                await probe.confirm(bridge, ObservedMonitor('probe', 1.))
                accepting = asyncio.create_task(bridge.accept(message))
                line = (await monitor_reader.readline()).decode().rstrip('\n')
                await accepting
                with Coordination(str(CoordinationStore.path_in(comms.root))) as store:
                    handed, = WakeAssignment.select(store.session._connection,
                        recipient_lookup=lookup, wire_seq=message.seq)
                    assert type(handed.lifecycle) is DeferredAssignment
                for update in notification(line).updates(ClaudeSession()):
                    await update.observed(bridge)
                with Coordination(str(CoordinationStore.path_in(comms.root))) as store:
                    confirmed, = WakeAssignment.select(store.session._connection,
                        recipient_lookup=lookup, wire_seq=message.seq)
                    assert type(confirmed.lifecycle) is DeliveredAssignment
                assert comms.bus.inbox(bridge.binding.bus).page.messages == ()
        finally:
            if accepting is not None:
                accepting.cancel()
                with suppress(asyncio.CancelledError):
                    await accepting
            monitor_writer.close()
            await monitor_writer.wait_closed()
            await attachment
            await bridge_peer.close()


@asynccontextmanager
async def attached_monitor(bridge):
    """A real monitor socket answers the bridge's preparation probe."""
    bridge_socket, monitor_socket = socket.socketpair()
    peer = MonitorPeer(*await asyncio.open_connection(sock=bridge_socket))
    reader, writer = await asyncio.open_connection(sock=monitor_socket)
    service = MonitorService(bridge, ProcessIdentity.capture(os.getpid()), lambda: None)
    attachment = asyncio.create_task(bridge.reader.attach(service, peer))
    try:
        async with asyncio.timeout(15):
            await reader.readline()
            probe = MONITOR_FRAME.validate_json(await reader.readline())
            await probe.confirm(bridge, ObservedMonitor('probe', 1.))
            yield reader
    finally:
        writer.close()
        await writer.wait_closed()
        await attachment
        await peer.close()


def decoded_record(data):
    return ClaudeJsonLines().decode_line(json.dumps(data).encode(), 0)


def notification(line, record_id='notification-record'):
    return decoded_record(dict(
        type='user', uuid=record_id, timestamp='2026-10-16T16:40:05.123Z',
        sessionId='session', promptId='prompt', origin=dict(kind='task-notification'),
        message=dict(content='<task-notification><event>'+escape(line)+
                     '</event></task-notification>')))


def attachment_notification(line, record_id='notification-record'):
    return decoded_record(dict(
        type='attachment', uuid=record_id, timestamp='2026-10-16T16:40:05.123Z',
        attachment=dict(type='queued_command', commandMode='task-notification',
            origin=dict(kind='task-notification', producer='session-task'),
            prompt='<task-notification><event>'+escape(line)+'</event></task-notification>')))


@pytest.mark.asyncio
@pytest.mark.parametrize('record_factory', (notification, attachment_notification))
async def test_parts_require_complete_exact_body_then_reopen(bridge, emitted_delivery, record_factory):
    message = emitted_delivery('x' * 3000)
    state, binding = bridge.options.state, bridge.binding
    lines = [bridge.encode(part) for part in bridge.incoming_frame(message).parts]
    assert len(lines) == 7
    assert all(len(line.encode('utf-16-le')) // 2 <= ClaudeMonitorFrame.EVENT_CHARS for line in lines)
    # Out-of-order observations and repeated history must not count twice.
    for number in (6, 1, 3, 1, 5, 2):
        for update in record_factory(lines[number], record_id=f'record-{number}').updates(ClaudeSession()):
            await update.observed(bridge)
    attempt, = await stored_attempts(bridge)
    assert attempt.outcome is EmittedOutcome
    bridge.db.close()
    with Binding.open(state) as db:
        reopened = ClaudeBridge(Options(Watch(), StoredBinding(), state, state.parent), binding.address(), db)
        await reopened.options.operation.prepare(reopened)
        for number in (0, 4):
            for update in record_factory(lines[number], record_id=f'record-{number}').updates(ClaudeSession()):
                await update.observed(reopened)
        confirmed, = await reopened.attempts(message.message_id)
        assert confirmed.outcome is DeliveredOutcome
        for line in lines:
            for update in record_factory(line, record_id='later-overlap').updates(ClaudeSession()):
                await update.observed(reopened)
        # Confirmation can beat the final socket-drain emission report: that report was
        # decided from a row read before the confirmation, and must not overwrite it.
        await reopened.finish(confirmed, partial(replace, outcome=EmittedOutcome))
        stored, = await stored_attempts(reopened)
        assert stored.outcome is DeliveredOutcome
        assert stored.monitor == ObservedMonitor('record-4', 1792168805.123)
        parts = await reopened.coordinator_read(lambda db: HarnessObservedPart.of(db, stored))
        assert len(parts) == 7




@pytest.mark.parametrize('body', ['x'*3000, ('\n"\\😀\t')*500, 'short'])
@pytest.mark.parametrize('sender', ['claude-code', 'a · b " 😀'])
def test_frame_cap_and_exact_reconstruction(body, sender):
    message=Message(sender=sender,target='viewer',type=InfoKind,body=body,timestamp=1.,seq=1)
    frame=ClaudeMonitorFrame(message)
    decoded=[ClaudeMonitorFrame.decode_line(json.dumps(part,ensure_ascii=False)) for part in frame.parts]
    assert all(isinstance(part,MonitorPart) for part in decoded)
    assert ''.join(part.body for part in decoded)==body
    assert all(part.sender==sender for part in decoded)
    assert all(frame.event_size(part)<=frame.EVENT_CHARS for part in frame.parts)
    assert [part.number for part in decoded]==list(range(1,len(decoded)+1))


def test_foreign_task_notifications_are_named_and_malformed_matching_xml_fails():
    record=decoded_record(dict(type='user',uuid='record',timestamp='2026-10-16T16:40:05.123Z',
        sessionId='session',promptId='prompt',origin=dict(kind='task-notification'),
        message=dict(content='Another task producer')))
    assert isinstance(record.message.content[0].text,ForeignTaskNotification)
    assert record.updates(ClaudeSession())==()
    from xml.etree.ElementTree import ParseError
    with pytest.raises(ParseError):
        decoded_record(dict(type='user',uuid='record',timestamp='2026-10-16T16:40:05.123Z',
            sessionId='session',promptId='prompt',origin=dict(kind='task-notification'),
            message=dict(content='<task-notification>')))
    assert isinstance(ClaudeMonitorFrame.decode_line('ordinary unrelated monitor event'),ForeignMonitorFrame)
    assert isinstance(ClaudeMonitorFrame.decode_line('"Comms in · old format...(truncated)'),ForeignMonitorFrame)


def queued_prompt(kind, prompt):
    return decoded_record(dict(type='attachment', uuid='queued-record',
        timestamp='2026-10-16T16:40:05.123Z', attachment=dict(type='queued_command',
        origin=dict(kind=kind), prompt=prompt)))


def test_non_task_attachments_and_pending_queue_records_do_not_confirm():
    session=ClaudeSession(turn=KnownTurn(FieldCodec.decode(NativeTurnId, 'turn')))
    for kind in ('peer', 'human'):
        updates=queued_prompt(kind, 'Not a Monitor notification').updates(session)
        assert not any(isinstance(update, MonitorObservation) for update in updates)
    hook=decoded_record(dict(type='attachment', uuid='foreign-record',
        timestamp='2026-10-16T16:40:05.123Z', attachment=dict(type='hook_success')))
    assert isinstance(hook.attachment, ForeignAttachment)
    assert hook.updates(ClaudeSession())==()
    queued=decoded_record(dict(type='queue-operation', operation='enqueue',
        sessionId='session', content='<task-notification><event>not yet received</event></task-notification>'))
    assert queued.updates(ClaudeSession())==()


def test_remote_control_session_record_decodes_without_updates():
    record = decoded_record({'type': 'bridge-session',
        'sessionId': '00000000-0000-4000-8000-000000000001',
        'bridgeSessionId': 'cse_000000000000000000000000', 'lastSequenceNum': 0,
        'ownerAccountUuid': '00000000-0000-4000-8000-000000000002',
        'ownerOrganizationUuid': '00000000-0000-4000-8000-000000000003'})
    assert record.updates(ClaudeSession()) == ()


@pytest.mark.asyncio
async def test_wrong_complete_parts_stay_pending_without_claiming_delivery(bridge, emitted_delivery, capsys):
    message = emitted_delivery('original')
    frame = MonitorPart(id=message.message_id, sender=message.sender, number=1, total=1, body='different')
    await frame.confirm(bridge, ObservedMonitor('record', 1.))
    stored, = await stored_attempts(bridge)
    assert stored.outcome is EmittedOutcome
    assert isinstance(stored.monitor, MismatchedMonitor)
    assert 'message remains pending' in capsys.readouterr().err


def test_old_emission_has_no_invented_evidence_or_replay(bridge):
    message = bridge.comms.messaging.send('peer', 'viewer', 'retained').message
    emitted = bridge.attempt(message, EmittedOutcome)
    with Coordination(str(CoordinationStore.path_in(Path(bridge.binding.root)))) as store, \
            store.session.writing() as db:
        assert emitted.first_write(db)
        assert not bridge.attempt(message, UnconfirmedOutcome).first_write(db)
        stored, = emitted.stored(db)
        assert isinstance(stored.monitor, UnrecordedMonitor)
        assert stored.outcome is EmittedOutcome
        assert HarnessObservedPart.of(db, stored) == ()


@pytest.mark.asyncio
async def test_explicit_watch_does_not_mark_streamed_messages_read(wired):
    from contextlib import aclosing
    import asyncio
    first=wired.messaging.send('PR111','fixer','first').message
    async with aclosing(wired.watch('fixer')) as stream:
        assert (await anext(stream)).reference==first.reference
        pending=asyncio.create_task(anext(stream))
        try:
            second=wired.messaging.send('PR111','fixer','second').message
            async with asyncio.timeout(3):
                taken=await pending
                while taken.reference==first.reference:
                    taken=await anext(stream)
            assert taken.reference==second.reference
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending,return_exceptions=True)
    assert {m.reference for m in wired.bus.inbox('fixer').page.messages}=={first.reference,second.reference}
    wired.bus.mark_read_through('fixer', first.seq)
    assert [m.reference for m in wired.bus.inbox('fixer').page.messages]==[second.reference]


@pytest.mark.asyncio
async def test_harness_outcome_lives_on_the_threads_claim_across_a_rehome(tmp_path):
    from bus_record_fixture import prepare_native_comms
    from conftest import publish_initial
    from agent_comms.assignment_states import DeferredAssignment, DeliveredAssignment
    from agent_comms.comms import wire
    from conftest import accept_initial
    from agent_comms.coordination_cohort import pending_sealed_assignments
    from agent_comms.coordination_database import CoordinationStore
    from agent_comms.coordination_tables.assignments import WakeAssignment
    from agent_comms.coordinator import Coordination
    from agent_comms.thread_execution import ExternalThreadExecution

    comms = prepare_native_comms(wire(tmp_path / 'wire'))
    comms.registry.declare(Thread('peer', frozenset(), Checkout(tmp_path)))
    comms.registry.declare(Thread('viewer', frozenset(), Checkout(tmp_path),
                                  execution=ExternalThreadExecution))
    root_id = comms.registry.store.prepare_native_root().namespace
    lookup = comms.registry.require('viewer').incarnation.require_recorded().lookup
    with Coordination(str(CoordinationStore.path_in(comms.root))) as store:
        store.participants.register(lookup, committed=True)

        def publish(body):
            message = publish_initial(comms, 'peer', 'viewer', body).message
            accept_initial(comms.bus, root_id, message.seq, store)
            return message

        taken, confirmed, waiting = (publish(body) for body in (
            'taken, start unproved', 'taken and confirmed', 'never reached the harness'))

    def claim(message):
        with Coordination(str(CoordinationStore.path_in(comms.root))) as store:
            row, = WakeAssignment.select(store.session._connection,
                                         recipient_lookup=lookup, wire_seq=message.seq)
            return row.lifecycle

    def owed():
        with Coordination(str(CoordinationStore.path_in(comms.root))) as store:
            return tuple(row.wire_seq for row in pending_sealed_assignments(store, lookup, after_seq=0))

    state = tmp_path / 'bridge-state'
    with Binding.open(state) as db:
        binding = Binding(root=str(comms.root), owner=comms.registry.require('viewer').incarnation,
                          thread='session', start=Binding.bus_head(str(comms.root)))
        binding.bind(db)
        bridge = ClaudeBridge(Options(Watch(), StoredBinding(), state, state.parent), binding.address(), db)
        await bridge.options.operation.prepare(bridge)
        async with attached_monitor(bridge) as reader:
            assert owed() == (taken.seq, confirmed.seq, waiting.seq)
            await bridge.accept(taken)
            await reader.readline()
            await bridge.accept(confirmed)
            line = (await reader.readline()).decode().rstrip('\n')
            for update in notification(line).updates(ClaudeSession()):
                await update.observed(bridge)
            await bridge.accept(taken)

    # Whatever execution owns the thread next reads this one record: only the
    # message no harness took is still owed, and the UNKNOWN one never replays.
    assert owed() == (waiting.seq,)
    unknown = claim(taken)
    assert unknown == DeferredAssignment.build(unknown.mode, None, None)
    assert type(claim(confirmed)) is DeliveredAssignment


def test_prompt_typed_during_a_turn_is_shown_and_an_agents_is_hidden():
    session=ClaudeSession(turn=KnownTurn(FieldCodec.decode(NativeTurnId, 'turn')))
    shown,=queued_prompt('human', 'typed while you worked').updates(session)
    assert [event.text for event in shown.events]==['typed while you worked']
    assert not isinstance(shown, HiddenItem)
    hidden,=queued_prompt('peer', 'a fork reporting').updates(session)
    assert isinstance(hidden, HiddenItem)


def test_prompt_typed_with_an_image_during_a_turn_shows_its_text():
    session=ClaudeSession(turn=KnownTurn(FieldCodec.decode(NativeTurnId, 'turn')))
    image=dict(type='base64', media_type='image/png', data='AA==')
    prompt=[dict(type='text', text='[Image #16] look'), dict(type='image', source=image)]
    shown,=queued_prompt('human', prompt).updates(session)
    assert [event.text for event in shown.events]==['[Image #16] look']

@pytest.mark.asyncio
async def test_a_bridge_waits_out_maintenance_and_then_records_its_delivery(tmp_path):
    """An install holds the bus: the bridge's harness hand-off and delivery record wait
    for the hold to clear instead of dying on MaintenanceHeldError (install 18, 10-24)."""
    from bus_record_fixture import prepare_native_comms
    from conftest import accept_initial, publish_initial
    from agent_comms.assignment_states import DeliveredAssignment
    from agent_comms.comms import wire
    from agent_comms.coordination_database import CoordinationStore
    from agent_comms.coordination_tables.assignments import WakeAssignment
    from agent_comms.coordinator import Coordination
    from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator
    from agent_comms.thread_execution import ExternalThreadExecution

    comms = prepare_native_comms(wire(tmp_path / 'wire'))
    comms.registry.declare(Thread('peer', frozenset(), Checkout(tmp_path)))
    comms.registry.declare(Thread('viewer', frozenset(), Checkout(tmp_path),
                                  execution=ExternalThreadExecution))
    root_id = comms.registry.store.prepare_native_root().namespace
    lookup = comms.registry.require('viewer').incarnation.require_recorded().lookup
    with Coordination(str(CoordinationStore.path_in(comms.root))) as store:
        store.participants.register(lookup, committed=True)
        message = publish_initial(comms, 'peer', 'viewer', 'held during install').message
        accept_initial(comms.bus, root_id, message.seq, store)

    def claim():
        with Coordination(str(CoordinationStore.path_in(comms.root))) as store:
            row, = WakeAssignment.select(store.session._connection,
                                         recipient_lookup=lookup, wire_seq=message.seq)
            return row.lifecycle

    barrier = MaintenanceBarrier(comms.registry.store.path)
    holds = []

    async def waits_until_cleared(pending):
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(.2):
                await asyncio.shield(pending)
        barrier.clear(holds.pop())

    state = tmp_path / 'bridge-state'
    with Binding.open(state) as db:
        binding = Binding(root=str(comms.root), owner=comms.registry.require('viewer').incarnation,
                          thread='session', start=Binding.bus_head(str(comms.root)))
        binding.bind(db)
        bridge = ClaudeBridge(Options(Watch(), StoredBinding(), state, state.parent), binding.address(), db)
        await bridge.options.operation.prepare(bridge)
        async with attached_monitor(bridge) as reader:
            holds.append(barrier.begin(MaintenanceOperator.current()))
            pending = asyncio.create_task(bridge.accept(message))
            await waits_until_cleared(pending)  # the hand-off to the harness
            await asyncio.wait_for(pending, 5)
            line = (await reader.readline()).decode().rstrip('\n')
            holds.append(barrier.begin(MaintenanceOperator.current()))

            async def observed():
                for update in notification(line).updates(ClaudeSession()):
                    await update.observed(bridge)

            pending = asyncio.create_task(observed())
            await waits_until_cleared(pending)  # the confirmation record
            await asyncio.wait_for(pending, 5)
    assert type(claim()) is DeliveredAssignment

def test_only_commanded_sessions_reply_to_their_delivered_senders(comms):
    """The commander answers its owner in its own transcript: a unit that messaged it
    mid-turn never receives that turn's answer as a direct message."""
    from agent_comms.registry_document import RegisteredCommanderProject
    from agent_comms.runtime_updates import FinishedItem, ReplyingThreadOutput
    from agent_comms.thread_execution import ExternalThreadExecution
    from agent_comms.transcript_events import AssistantTranscript
    from agent_comms.turn_item import NativeItemId, NativeTurnId

    lead, unit = (comms.registry.declare(Thread(
        name, frozenset(), Checkout(Path("/tmp")), execution=ExternalThreadExecution)) for name in ("lead", "unit"))
    comms.registry.set_commander_project(RegisteredCommanderProject(lead.incarnation, ()))
    for speaker, sender in ((lead, unit), (unit, lead)):
        output = ReplyingThreadOutput.for_comms(comms.transcripts.external(speaker.name), comms)
        output.delivered_targets((sender.name,))
        turn = NativeTurnId(f"{speaker.name}-turn")
        item_id = NativeItemId("a")
        output.publish_item(FinishedItem(turn, item_id, (AssistantTranscript(
            f"{speaker.name} answers", timestamp=1.0, position=item_id),)))
        output.end_turn(turn)
    assert comms.bus.pending_count("unit") == 0
    assert comms.bus.pending_count("lead") == 1
