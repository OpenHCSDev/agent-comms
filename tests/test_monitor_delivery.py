"""Session-owned parts, not stdout writes, confirm monitor delivery."""
from contextlib import suppress
import asyncio
import os
import socket
from html import escape
import json


import pytest

from conftest import declare_test_bus

from agent_comms.installer.practice import PracticeCopy
from agent_comms.locked_store import RootFile

from agent_comms.cli_bridges.claude import ClaudeBridge, ClaudeJsonLines
from agent_comms.child_process import ProcessIdentity
from agent_comms.cli_bridges.monitor import MonitorPeer, MonitorService
from agent_comms.cli_bridges.claude_protocol import (
    ClaudeSession, ClaudeMonitorFrame, ForeignTaskNotification,
    ForeignAttachment,
)
from agent_comms.cli_bridges.shared import (
    Binding, DeliveryRow, ObservedPart, ReceivingMonitor, UnrecordedMonitor, MonitorPart,
    ForeignMonitorFrame, Options, ObservedMonitor, MismatchedMonitor, Watch, StoredBinding,
    GivenBinding, MONITOR_FRAME,
)
from agent_comms.cli_protocol import UnconfirmedOutcome, EmittedOutcome, DeliveredOutcome
from agent_comms.messages import InfoKind, Message
from agent_comms.read_basis import ExplicitWatchAcknowledgment
from agent_comms.threads import Thread


@pytest.mark.asyncio
async def test_empty_journal_starts_at_binding_and_ignores_user_reads(comms, tmp_path):
    declare_test_bus(comms, threads=tuple(
        Thread(name, frozenset(), str(tmp_path)) for name in ('viewer', 'peer')
    ))
    before = comms.messaging.send('peer', 'viewer', 'before this session was bound').message
    state = tmp_path / 'bridge-state'
    with Binding.open(state) as db:
        binding = GivenBinding({
            Binding.family_discriminator: Binding.declared_name,
            'root': str(comms.root), 'bus_thread': 'viewer', 'session': 'session',
        }).bind(db)
        selected = ClaudeBridge(Options(Watch(), StoredBinding(), state, state.parent), binding, db)
        fresh = comms.messaging.send(
            'peer', 'viewer', 'after binding, already read in Toad').message
        comms.messaging.acknowledge('viewer', (fresh.reference,))
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
                row, = DeliveryRow.select(db)
                assert row.message.reference == fresh.reference
                assert row.outcome is not DeliveredOutcome
                assert isinstance(row.monitor, ObservedMonitor)
                assert comms.bus.reads.read() == user_reads
                unread = comms.messaging.send(
                    'peer', 'viewer', 'delivery records this model read').message
                line = (await monitor_reader.readline()).decode().rstrip('\n')
                for update in notification(line, record_id='second-delivery').updates(
                        ClaudeSession()):
                    await update.observed(selected)
                assert {row.message.reference for row in DeliveryRow.select(db)} == {
                    fresh.reference, unread.reference,
                }
                assert all(row.outcome is not DeliveredOutcome for row in DeliveryRow.select(db))
                assert all(isinstance(row.monitor, ObservedMonitor)
                           for row in DeliveryRow.select(db))
                assert [item.reference for item in comms.bus.inbox('viewer').page.messages] == [
                    before.reference]
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
        Thread(name, frozenset(), str(tmp_path))
        for name in ('claude-code', 'viewer', 'peer')
    ))
    practice = PracticeCopy(comms.root, tmp_path / 'copied-bus')
    practice.include(tuple(RootFile.existing_in(practice.root)))
    (root, _measurement) = practice.path, practice.measurement
    state = tmp_path / 'bridge-state'
    binding = Binding(str(root), 'viewer', 'session', Binding.bus_head(str(root)))
    with Binding.open(state) as db:
        binding.bind(db)
        yield ClaudeBridge(Options(Watch(), StoredBinding(), state, state.parent), binding, db)


@pytest.fixture
def emitted_delivery(bridge):
    def emit(body, target='viewer'):
        message = bridge.comms.messaging.send('claude-code', target, body).message
        delivery = DeliveryRow(message.message_id, json.dumps(message.to_wire()),
            EmittedOutcome, ReceivingMonitor(len(bridge.incoming_frame(message).parts)))
        with bridge.db:
            delivery.insert(bridge.db)
        return delivery
    return emit


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
    delivery = emitted_delivery('x' * 3000)
    message = delivery.message
    state, binding = bridge.options.state, bridge.binding
    lines = [bridge.encode(part) for part in bridge.incoming_frame(message).parts]
    assert len(lines) == 7
    assert all(len(line.encode('utf-16-le')) // 2 <= ClaudeMonitorFrame.EVENT_CHARS for line in lines)
    # Out-of-order observations and repeated history must not count twice.
    for number in (6, 1, 3, 1, 5, 2):
        for update in record_factory(lines[number], record_id=f'record-{number}').updates(ClaudeSession()):
            await update.observed(bridge)
    assert [item.reference for item in bridge.comms.bus.inbox(binding.bus).page.messages] == [message.reference]
    assert DeliveryRow.select(bridge.db)[0].outcome is EmittedOutcome
    bridge.db.close()
    with Binding.open(state) as db:
        reopened = ClaudeBridge(Options(Watch(), StoredBinding(), state, state.parent), binding, db)
        for number in (0, 4):
            for update in record_factory(lines[number], record_id=f'record-{number}').updates(ClaudeSession()):
                await update.observed(reopened)
        assert reopened.comms.bus.inbox(binding.bus).page.messages == ()
        for line in lines:
            for update in record_factory(line, record_id='later-overlap').updates(ClaudeSession()):
                await update.observed(reopened)
        assert reopened.comms.bus.inbox(binding.bus).page.messages == ()
        # Confirmation can beat the final socket-drain emission report.
        confirmed, = DeliveryRow.select(db, id=message.message_id)
        await confirmed.finish(reopened, EmittedOutcome)
    with Binding.open(state) as db:
        assert DeliveryRow.select(db)[0].outcome is EmittedOutcome
        assert DeliveryRow.select(db)[0].monitor == ObservedMonitor('record-4', 1792168805.123)
        assert len(ObservedPart.select(db)) == 7




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


def test_non_task_attachments_and_pending_queue_records_do_not_confirm():
    record=decoded_record(dict(type='attachment', uuid='foreign-record',
        timestamp='2026-10-16T16:40:05.123Z', attachment=dict(type='queued_command',
        origin=dict(kind='peer'), prompt='Not a Monitor notification')))
    assert isinstance(record.attachment, ForeignAttachment)
    assert record.updates(ClaudeSession())==()
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
    delivery = emitted_delivery('original')
    frame = MonitorPart(id=delivery.id, sender=delivery.message.sender, number=1, total=1, body='different')
    await frame.confirm(bridge, ObservedMonitor('record', 1.))
    assert DeliveryRow.select(bridge.db)[0].outcome is EmittedOutcome
    assert isinstance(DeliveryRow.select(bridge.db)[0].monitor, MismatchedMonitor)
    assert 'message remains pending' in capsys.readouterr().err


def test_old_emission_has_no_invented_evidence_or_replay(bridge):
    message = bridge.comms.messaging.send('peer', 'viewer', 'retained').message
    with bridge.db:
        DeliveryRow(message.message_id, json.dumps(message.to_wire()), EmittedOutcome).insert(bridge.db)
    assert isinstance(DeliveryRow.select(bridge.db)[0].monitor, UnrecordedMonitor)
    assert not DeliveryRow(message.message_id, json.dumps(message.to_wire()), UnconfirmedOutcome).insert_if_absent(bridge.db).rowcount
    assert DeliveryRow.select(bridge.db)[0].outcome is EmittedOutcome
    assert ObservedPart.select(bridge.db) == []


@pytest.mark.asyncio
async def test_explicit_watch_does_not_mark_streamed_messages_read(wired):
    from contextlib import aclosing
    import asyncio
    first=wired.messaging.send('PR111','fixer','first').message
    async with aclosing(wired.watch('fixer', acknowledgment=ExplicitWatchAcknowledgment)) as stream:
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
    assert wired.messaging.acknowledge('fixer',(first.reference,))==1
    assert [m.reference for m in wired.bus.inbox('fixer').page.messages]==[second.reference]


@pytest.mark.asyncio
async def test_harness_outcome_lives_on_the_threads_claim_across_a_rehome(tmp_path, monkeypatch):
    from bus_record_fixture import prepare_native_comms
    from conftest import publish_initial
    from agent_comms.assignment_states import DeferredAssignment, DeliveredAssignment
    from agent_comms.comms import wire
    from agent_comms.coordination_cohort import accept_delivery_cohort, pending_sealed_assignments
    from agent_comms.coordination_database import CoordinationStore
    from agent_comms.coordination_tables.assignments import WakeAssignment
    from agent_comms.coordinator import Coordination
    from agent_comms.thread_execution import ExternalThreadExecution

    comms = prepare_native_comms(wire(tmp_path / 'wire'))
    comms.registry.declare(Thread('peer', frozenset(), str(tmp_path)))
    comms.registry.declare(Thread('viewer', frozenset(), str(tmp_path),
                                  execution=ExternalThreadExecution))
    root_id = comms.registry.prepare_native_root().namespace
    lookup = comms.registry.require('viewer').incarnation.require_recorded().lookup
    with Coordination(str(CoordinationStore.path_in(comms.root))) as store:
        store.participants.register(lookup, committed=True)

        def publish(body):
            message = publish_initial(comms, 'peer', 'viewer', body).message
            accept_delivery_cohort(comms.bus, root_id, message.seq, store)
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

    async def emitted(message):
        return EmittedOutcome

    async def attached():
        pass

    state = tmp_path / 'bridge-state'
    with Binding.open(state) as db:
        binding = Binding(str(comms.root), 'viewer', 'session', Binding.bus_head(str(comms.root)))
        binding.bind(db)
        bridge = ClaudeBridge(Options(Watch(), StoredBinding(), state, state.parent), binding, db)
        monkeypatch.setattr(bridge, 'prepare_delivery', attached)
        monkeypatch.setattr(bridge, 'deliver', emitted)
        assert owed() == (taken.seq, confirmed.seq, waiting.seq)
        await bridge.accept(taken)
        await bridge.accept(confirmed)
        row, = DeliveryRow.select(db, id=confirmed.message_id)
        await row.finish(bridge, DeliveredOutcome)
        await bridge.accept(taken)

    # Whatever execution owns the thread next reads this one record: only the
    # message no harness took is still owed, and the UNKNOWN one never replays.
    assert owed() == (waiting.seq,)
    unknown = claim(taken)
    assert unknown == DeferredAssignment.build(unknown.mode, None, None)
    assert type(claim(confirmed)) is DeliveredAssignment
