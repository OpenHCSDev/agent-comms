"""Session-owned parts, not stdout writes, confirm monitor delivery."""
from pathlib import Path
from agent_comms.repo import Checkout
from contextlib import asynccontextmanager, suppress
import asyncio
import os
import shutil
import socket
import sqlite3
import stat
from contextlib import closing
from dataclasses import replace
from functools import partial
from html import escape
import json


import pytest

from transcript_record_fixture import transcript_record

from conftest import declare_test_bus

from agent_comms.installer.practice import PracticeCopy
from agent_comms.locked_store import RootFile

from agent_comms.cli_bridges.claude import ClaudeBridge
from agent_comms.cli_bridges.claude_transcript import ClaudeJsonLines
from agent_comms.child_process import ProcessIdentity
from agent_comms.cli_bridges.monitor import MonitorPeer, MonitorService
from agent_comms.cli_bridges.claude_protocol import (
    ClaudeSession, ClaudeMonitorFrame, ForeignTaskNotification, ReadResultObserved,
    ForeignAttachment, KnownTurn,
)
from agent_comms.cli_bridges.shared import (
    Binding, MonitorPart, ForeignMonitorFrame, Options, Watch, StoredBinding,
    MonitorObservation, MonitorReadFile,
    GivenBinding, MONITOR_FRAME,
)
from agent_comms.harness_delivery_states import (
    DeliveredOutcome, EmittedOutcome, MismatchedMonitor, ObservedMonitor, ReceivingMonitor,
    UnconfirmedOutcome, UnrecordedMonitor,
)
from agent_comms.agent_inbox import TakenInputStart
from agent_comms.errors import RelationViolationError
from agent_comms.read_ledger import ReadLedger
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_errors import CoordinationReadUnavailable
from agent_comms.coordination_tables.harness_deliveries import HarnessDelivery, HarnessObservedPart
from agent_comms.coordinator import Coordination
from agent_comms.field_codec import FieldCodec, InputId
from agent_comms.message_reference import BridgeStart, MessageReference
from agent_comms.runtime_updates import (
    AllTurnsEnded, FinishedItem, FinishedTurn, HiddenItem, RuntimeUpdate,
)
from agent_comms.turn_item import NativeItemId, NativeTurnId
from agent_comms.external_transcript import ExternalTurnInput
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.messages import AlertKind, InfoKind, Message, NoticeRole, QuestionKind
from agent_comms.channel_targets import ThreadTarget
from agent_comms.diagnostics import record_terminal_failure
from agent_comms.transcript_events import AssistantTranscript
from agent_comms.turn_failure import BackendFailed
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
                await probe.confirm(selected, ObservedMonitor(transcript_record('probe'), 1.))
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
                for update in notification(line, record_id=transcript_record('second-delivery')).updates(
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
    declare_test_bus(comms, threads=(
        Thread('claude-code', frozenset(), Checkout(tmp_path)),
        # A production bridge's session is external: its answers live in the external store.
        Thread('viewer', frozenset(), Checkout(tmp_path), execution=ExternalThreadExecution),
        Thread('peer', frozenset(), Checkout(tmp_path)),
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
    async def emit(body, target='viewer'):
        message = bridge.comms.messaging.send('claude-code', target, body).message
        delivery = replace(bridge.attempt(message, EmittedOutcome),
                           monitor=ReceivingMonitor(len(bridge.incoming_frame(message).parts)))
        assert await bridge.coordinator_write(lambda _, db: delivery.first_write(db))
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
                await probe.confirm(bridge, ObservedMonitor(transcript_record('probe'), 1.))
                await bridge.accept(first)
                line = (await reader.readline()).decode().rstrip('\n')
                # The store's declared wait expires; no method or error is patched.
                with closing(sqlite3.connect(path, isolation_level=None)) as held:
                    held.execute('BEGIN EXCLUSIVE')
                    try:
                        for update in notification(line, record_id=transcript_record('busy-first')).updates(ClaudeSession()):
                            await update.observed(bridge)
                    finally:
                        held.execute('ROLLBACK')
                uncertain, = await bridge.attempts(first.message_id)
                assert uncertain.outcome is EmittedOutcome
                assert uncertain.monitor == ReceivingMonitor(1)
                assert not attachment.done()
                await bridge.accept(second)
                line = (await reader.readline()).decode().rstrip('\n')
                for update in notification(line, record_id=transcript_record('healthy-second')).updates(ClaudeSession()):
                    await update.observed(bridge)
                confirmed, = await bridge.attempts(second.message_id)
                assert confirmed.monitor == ObservedMonitor(transcript_record('healthy-second'), 1792168805.123)
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


async def begin_waits(capsys, message):
    """The bridge's own log names the held message once the store's declared wait expires."""
    seen = ''
    while f'Message {message.message_id} waits for the store' not in seen:
        await asyncio.sleep(.01)
        seen += capsys.readouterr().err


@pytest.mark.asyncio
async def test_busy_read_holds_the_watch_resume_until_the_store_frees(bridge, capsys):
    """Real contention on the read that opens the core watch: the wait keeps the bridge up."""
    path = CoordinationStore.path_in(bridge.comms.root)
    with Coordination(str(path)):
        pass
    with closing(sqlite3.connect(path, isolation_level=None)) as held:
        held.execute('BEGIN EXCLUSIVE')
        resuming = asyncio.create_task(bridge.once_available('Watch resume', bridge.resume_after))
        seen = ''
        async with asyncio.timeout(60):
            while 'Watch resume waits for the store' not in seen:
                await asyncio.sleep(.01)
                seen += capsys.readouterr().err
        assert not resuming.done()
        held.execute('ROLLBACK')
    assert await asyncio.wait_for(resuming, 30) == bridge.binding.start


@pytest.mark.asyncio
async def test_busy_begin_holds_the_same_message_until_the_store_frees(bridge, capsys):
    """Real SQLite contention before the durable begin: the original is held with no attempt
    and no frame, the attachment keeps serving, and after release the same message reaches
    the reader exactly once; a later message needs no reconnect."""
    held_message, later = (bridge.comms.messaging.send('peer', 'viewer', body).message
                           for body in ('held message', 'later message'))
    path = CoordinationStore.path_in(bridge.comms.root)
    with Coordination(str(path)):
        pass
    async with attached_monitor(bridge, timeout=60) as reader:
        with closing(sqlite3.connect(path, isolation_level=None)) as held:
            held.execute('BEGIN EXCLUSIVE')
            try:
                accepting = asyncio.create_task(bridge.accept(held_message))
                await begin_waits(capsys, held_message)
                # The event loop still runs other work while the begin waits.
                assert not accepting.done()
                assert tuple(HarnessDelivery.select(
                    held, bridge=type(bridge), recipient_lookup=bridge.recipient_lookup,
                    message_id=held_message.message_id)) == ()
                with pytest.raises(TimeoutError):
                    async with asyncio.timeout(.3):
                        await reader.readline()
            finally:
                held.execute('ROLLBACK')
        await accepting
        line = await only_event(reader)
        await observe(bridge, notification(line, record_id=transcript_record('held')).updates(
            ClaudeSession()))
        delivered, = await bridge.attempts(held_message.message_id)
        assert delivered.outcome is DeliveredOutcome
        await bridge.accept(later)
        line = await only_event(reader)
        await observe(bridge, notification(line, record_id=transcript_record('later')).updates(
            ClaudeSession()))
        confirmed, = await bridge.attempts(later.message_id)
        assert confirmed.outcome is DeliveredOutcome
    assert await bridge.attempts(held_message.message_id) == (delivered,)
    assert await bridge.resume_after() == BridgeStart(later.reference)


@pytest.mark.asyncio
async def test_cancelled_busy_begin_records_nothing_and_still_owes_the_original(bridge, capsys):
    """Cancelling a begin blocked on real SQLite contention ends the wait: no attempt, no
    frame, and the bridge still starts before the unstarted original."""
    message = bridge.comms.messaging.send('peer', 'viewer', 'never begun').message
    path = CoordinationStore.path_in(bridge.comms.root)
    with Coordination(str(path)):
        pass
    start = await bridge.resume_after()
    async with attached_monitor(bridge, timeout=60) as reader:
        with closing(sqlite3.connect(path, isolation_level=None)) as held:
            held.execute('BEGIN EXCLUSIVE')
            try:
                accepting = asyncio.create_task(bridge.accept(message))
                await begin_waits(capsys, message)
                accepting.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await accepting
            finally:
                held.execute('ROLLBACK')
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(.3):
                await reader.readline()
    assert await bridge.attempts(message.message_id) == ()
    assert await bridge.resume_after() == start


@pytest.mark.asyncio
async def test_a_failure_after_the_write_is_entered_is_never_relabelled_unstarted(bridge):
    """Negative contract control, not contention evidence: an operation that ran and then
    raised keeps its own error, runs once, and its transaction rolls back."""
    message = bridge.comms.messaging.send('peer', 'viewer', 'entered').message
    calls = []

    def entered(assignments, db):
        calls.append(bridge.attempt(message, UnconfirmedOutcome).begin(assignments, db))
        raise CoordinationReadUnavailable(CoordinationReadUnavailable.busy_message)

    with pytest.raises(CoordinationReadUnavailable) as raised:
        await bridge.coordinator_write(entered)
    assert type(raised.value) is CoordinationReadUnavailable
    assert calls == [True]
    assert await bridge.attempts(message.message_id) == ()


@pytest.mark.asyncio
async def test_renamed_reader_handoff_and_confirmation_share_the_prepared_owner(tmp_path):
    from bus_record_fixture import prepare_native_comms
    from conftest import accept_initial, publish_initial
    from agent_comms.assignment_states import DeferredAssignment, DeliveredAssignment
    from agent_comms.comms import wire
    from agent_comms.coordination_database import CoordinationStore
    from agent_comms.coordination_tables.assignments import WakeAssignment
    from agent_comms.coordinator import Coordination

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
        human_reads = ReadLedger.at(comms.root).read()
        try:
            async with asyncio.timeout(15):
                await monitor_reader.readline()
                probe = MONITOR_FRAME.validate_json(await monitor_reader.readline())
                await probe.confirm(bridge, ObservedMonitor(transcript_record('probe'), 1.))
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
                # The harness took it, so the agent's page drops it; the human's unread view and
                # read ledger are a different owner and do not move.
                assert comms.bus.inbox(bridge.binding.bus, after=TakenInputStart()).page.messages == ()
                assert message.reference in {
                    item.reference for item in comms.bus.inbox(bridge.binding.bus).page.messages}
                assert ReadLedger.at(comms.root).read() == human_reads
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
async def attached_monitor(bridge, timeout=15):
    """A real monitor socket answers the bridge's preparation probe."""
    bridge_socket, monitor_socket = socket.socketpair()
    peer = MonitorPeer(*await asyncio.open_connection(sock=bridge_socket))
    reader, writer = await asyncio.open_connection(sock=monitor_socket)
    service = MonitorService(bridge, ProcessIdentity.capture(os.getpid()), lambda: None)
    attachment = asyncio.create_task(bridge.reader.attach(service, peer))
    try:
        async with asyncio.timeout(timeout):
            await reader.readline()
            probe = MONITOR_FRAME.validate_json(await reader.readline())
            await probe.confirm(bridge, ObservedMonitor(transcript_record('probe'), 1.))
            yield reader
    finally:
        writer.close()
        await writer.wait_closed()
        await attachment
        await peer.close()


def decoded_record(data):
    return ClaudeJsonLines().decode_line(json.dumps(data).encode(), 0)


def notification(line, record_id=transcript_record('notification-record'), *, prompt_id='prompt'):
    return decoded_record(dict(
        type='user', uuid=record_id, timestamp='2026-10-16T16:40:05.123Z',
        sessionId='session', promptId=prompt_id, origin=dict(kind='task-notification'),
        message=dict(content='<task-notification><event>'+escape(line)+
                     '</event></task-notification>')))


def attachment_notification(line, record_id=transcript_record('notification-record')):
    return decoded_record(dict(
        type='attachment', uuid=record_id, timestamp='2026-10-16T16:40:05.123Z',
        attachment=dict(type='queued_command', commandMode='task-notification',
            origin=dict(kind='task-notification', producer='session-task'),
            prompt='<task-notification><event>'+escape(line)+'</event></task-notification>')))


@pytest.mark.asyncio
async def test_pages_require_every_line_then_reopen(bridge):
    """Out-of-order and repeated pages never count twice, and survive a reopened bridge."""
    message, export = await exported(bridge, body_of(7))
    state, binding = bridge.options.state, bridge.binding
    text = export.read_text(newline='')
    lines = text.split('\n')[:-1]
    for number in (6, 1, 3, 1, 5, 2):
        await observe(bridge, read_records(export, shown(lines[number + 1], number + 2), f'record-{number}'))
    attempt, = await stored_attempts(bridge)
    assert attempt.outcome is EmittedOutcome
    bridge.db.close()
    with Binding.open(state) as db:
        reopened = ClaudeBridge(Options(Watch(), StoredBinding(), state, state.parent), binding.address(), db)
        await reopened.options.operation.prepare(reopened)
        for number in (0, 4):
            await observe(reopened, read_records(export, shown(lines[number + 1], number + 2), f'record-{number}'))
        confirmed, = await reopened.attempts(message.message_id)
        assert confirmed.outcome is DeliveredOutcome
        # Confirmation can beat the final socket-drain emission report: that report was
        # decided from a row read before the confirmation, and must not overwrite it.
        await reopened.finish(confirmed, partial(replace, outcome=EmittedOutcome))
        stored, = await stored_attempts(reopened)
        assert stored.outcome is DeliveredOutcome
        assert stored.monitor == ObservedMonitor(transcript_record('record-4'), 1792168805.123)
        parts = await reopened.coordinator_read(lambda db: HarnessObservedPart.of(db, stored))
        assert len(parts) == 7




def body_of(parts):
    """A body whose Claude frame has exactly this many parts."""
    return 'x' * 50 if parts == 1 else '\n'.join(('x' * 300,) * parts)


def read_started(session, path, label):
    """Claude's real Read call, still open in this session."""
    return decoded_record(dict(
        type='assistant', uuid=transcript_record(f'{label}-start'),
        timestamp='2026-10-16T16:40:04.000Z', sessionId='session',
        message=dict(content=[dict(type='tool_use', id=f'read-{label}', name='Read',
                                   input=dict(file_path=str(path)))]))).updates(session)


def read_returned(session, text, label, error=False):
    """The visible result of that Read call."""
    return decoded_record(dict(
        type='user', uuid=transcript_record(label), timestamp='2026-10-16T16:40:05.123Z',
        sessionId='session', promptId='prompt', message=dict(content=[dict(
            type='tool_result', tool_use_id=f'read-{label}', content=text,
            is_error=error)]))).updates(session)


def read_records(path, text, label, error=False):
    """One Read call and its visible result, decoded through one session."""
    session = ClaudeSession()
    return (*read_started(session, path, label), *read_returned(session, text, label, error))


def shown(text, start=1):
    """Claude's Read output of physical text: each LF-separated row's number, a tab, then the
    row. Text ending in LF, as a whole export does, shows the empty row after it numbered too."""
    return '\n'.join(f'{number}\t{row}' for number, row in enumerate(text.split('\n'), start))


async def observe(bridge, updates):
    for update in updates:
        await update.observed(bridge)


async def only_event(reader):
    """The one line the reader gets for a message; any second line is a partial message."""
    line = (await reader.readline()).decode().rstrip('\n')
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(.3):
            await reader.readline()
    assert ClaudeMonitorFrame.units(line) <= ClaudeMonitorFrame.EVENT_CHARS
    return line


async def exported(bridge, body):
    """A message accepted through a real reader, and the file its one notice names."""
    message = bridge.comms.messaging.send('peer', 'viewer', body).message
    async with attached_monitor(bridge) as reader:
        await bridge.accept(message)
        notice = ClaudeMonitorFrame.decode_line(await only_event(reader))
    assert notice == MonitorReadFile.naming(bridge.exports, message.reference)
    return message, Path(notice.path)


@pytest.mark.asyncio
async def test_a_body_that_fits_one_event_goes_inline_and_exports_nothing(bridge):
    message = bridge.comms.messaging.send('peer', 'viewer', body_of(1)).message
    async with attached_monitor(bridge) as reader:
        await bridge.accept(message)
        line = await only_event(reader)
        part = ClaudeMonitorFrame.decode_line(line)
        assert (part.id, part.number, part.total) == (message.message_id, 1, 1)
        await observe(bridge, notification(line).updates(ClaudeSession()))
    confirmed, = await bridge.attempts(message.message_id)
    assert confirmed.outcome is DeliveredOutcome
    assert not bridge.exports.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('parts', (2, 8, 23, 25, 46, 26))
async def test_a_larger_message_is_one_notice_and_one_whole_read_confirms_it(bridge, parts):
    """The saved slow orders' part counts, and the smallest message that no longer fits one
    event: one bounded notice before anything is confirmed, then the whole body from one
    visible Read result, never part by part through the window."""
    body = body_of(parts)
    message = bridge.comms.messaging.send('peer', 'viewer', body).message
    human_reads = ReadLedger.at(bridge.comms.root).read()
    async with attached_monitor(bridge) as reader:
        await bridge.accept(message)
        line = await only_event(reader)
        notice = ClaudeMonitorFrame.decode_line(line)
        assert notice == MonitorReadFile.naming(bridge.exports, message.reference)
        export = Path(notice.path)
        assert stat.S_IMODE(export.stat().st_mode) == 0o600
        text = export.read_text(newline='')
        lines = text.split('\n')[:-1]
        assert lines[0] == f'Comms in · peer · {message.message_id}'
        assert '\n'.join(lines[1:]) == body
        # The notice frees its own reservation and proves nothing about the body.
        await observe(bridge, notification(line, record_id=transcript_record('notice')).updates(
            ClaudeSession()))
        waiting, = await bridge.attempts(message.message_id)
        assert waiting.outcome is EmittedOutcome
        assert waiting.monitor == ReceivingMonitor(parts)
        await observe(bridge, read_records(export, shown(text), 'whole-read'))
    confirmed, = await bridge.attempts(message.message_id)
    assert confirmed.outcome is DeliveredOutcome
    assert confirmed.monitor == ObservedMonitor(transcript_record('whole-read'), 1792168805.123)
    observed = await bridge.coordinator_read(lambda db: HarnessObservedPart.of(db, confirmed))
    assert len(observed) == parts
    assert not export.exists()
    assert ReadLedger.at(bridge.comms.root).read() == human_reads


@pytest.mark.asyncio
async def test_only_visible_pages_of_the_exact_export_confirm(bridge):
    message, export = await exported(bridge, body_of(23))
    _, other_export = await exported(bridge, body_of(23) + 'y')
    text = export.read_text(newline='')
    lines = text.split('\n')[:-1]
    copied = export.parent.parent / 'copied.txt'
    shutil.copyfile(export, copied)
    whole = shown(text)
    truncated = lines[-1][:-1]
    assert truncated != lines[-1]
    # Only the one numbered empty row after the final LF is the Read's terminator.
    for path, read, label, error in (
        (export, whole, 'failed-read', True),
        (copied, whole, 'foreign-path', False),
        (export, shown(other_export.read_text(newline='')), 'foreign-message', False),
        (export, shown('\n'.join(lines[:3])) + '\n5\t' + lines[4], 'numbering-gap', False),
        (export, shown('\n'.join([*lines[:5], lines[5][:100]])), 'truncated-line', False),
        (export, shown('\n'.join([*lines[:-1], truncated]) + '\n'), 'truncated-final', False),
        (export, shown(text.replace('\n', '\n\n', 1)), 'interior-empty', False),
        (export, shown(text + '\n'), 'two-terminal-empties', False),
        (export, whole.rpartition('\n')[0] + f'\n{len(lines) + 2}\t', 'terminal-gap', False),
        (export, shown(text + 'junk'), 'foreign-terminal', False),
    ):
        await observe(bridge, read_records(path, read, label, error))
        pending, = await bridge.attempts(message.message_id)
        assert pending.outcome is EmittedOutcome, label
        assert await bridge.coordinator_read(
            lambda db: HarnessObservedPart.of(db, pending)) == (), label
    # Pages read out of order and repeated accumulate until every original part matches; a
    # page reaching the end shows the terminator, a page stopping before it does not.
    for page, start, label in (('\n'.join(lines[12:]) + '\n', 12, 'last-page'),
                               ('\n'.join(lines[12:]), 12, 'repeated-page'),
                               ('\n'.join(lines[:12]), 0, 'first-page')):
        await observe(bridge, read_records(export, shown(page, start + 1), label))
    confirmed, = await bridge.attempts(message.message_id)
    assert confirmed.outcome is DeliveredOutcome
    assert confirmed.monitor == ObservedMonitor(transcript_record('first-page'), 1792168805.123)
    assert not export.exists()
    assert other_export.exists() and copied.exists()


@pytest.mark.asyncio
async def test_an_export_copy_at_another_sequence_confirms_nothing(bridge):
    """The same message id at another sequence is not the attempted reference, even when every
    byte and part matches: the export names its exact reference."""
    message, export = await exported(bridge, body_of(23))
    text = export.read_text(newline='')
    copy = Path(MonitorReadFile.naming(bridge.exports, MessageReference(
        message.seq + 1000, message.message_id)).path)
    shutil.copyfile(export, copy)
    await observe(bridge, read_records(copy, shown(text), 'wrong-sequence'))
    pending, = await bridge.attempts(message.message_id)
    assert pending.outcome is EmittedOutcome
    assert pending.monitor == ReceivingMonitor(23)
    assert await bridge.coordinator_read(lambda db: HarnessObservedPart.of(db, pending)) == ()
    assert export.exists() and copy.exists()
    await observe(bridge, read_records(export, shown(text), 'exact-sequence'))
    confirmed, = await bridge.attempts(message.message_id)
    assert confirmed.outcome is DeliveredOutcome
    assert confirmed.monitor == ObservedMonitor(transcript_record('exact-sequence'), 1792168805.123)
    assert not export.exists() and copy.exists()


@pytest.mark.asyncio
async def test_a_wrong_body_in_the_export_never_confirms(bridge, capsys):
    message, export = await exported(bridge, body_of(23))
    text = export.read_text(newline='')
    forged = text.replace('xxxx', 'xxxy', 1)
    assert forged != text
    await observe(bridge, read_records(export, shown(forged), 'wrong-body'))
    stored, = await bridge.attempts(message.message_id)
    assert stored.outcome is EmittedOutcome
    assert stored.monitor == ReceivingMonitor(23)
    assert export.exists()
    await observe(bridge, read_records(export, shown(text), 'honest-read'))
    confirmed, = await bridge.attempts(message.message_id)
    assert confirmed.outcome is DeliveredOutcome


@pytest.mark.asyncio
@pytest.mark.parametrize('separator', ('\u2028', '\x85', '\u2029\x1c'))
async def test_line_separators_inside_a_body_stay_inside_their_part(bridge, separator):
    """Encoded parts keep U+2028, U+0085 and friends raw; only a physical LF ends a record,
    inline in an event and in a Read result alike."""
    short = bridge.comms.messaging.send('peer', 'viewer', f'a{separator}b').message
    async with attached_monitor(bridge) as reader:
        await bridge.accept(short)
        line = await only_event(reader)
        assert separator[0] in line
        await observe(bridge, notification(line).updates(ClaudeSession()))
    inline, = await bridge.attempts(short.message_id)
    assert inline.outcome is DeliveredOutcome
    body = body_of(23)[:-80] + (separator + '\n\r"\\😀') * 10
    message, export = await exported(bridge, body)
    lines = export.read_text(newline='').split('\n')[:-1]
    assert '\n'.join(lines[1:]) == body
    # Two visible pages of the actual numbered Read output, the later page, reaching the end
    # and its terminator, first.
    session = ClaudeSession()
    for start, page in ((10, '\n'.join(lines[10:]) + '\n'), (0, '\n'.join(lines[:10]))):
        await observe(bridge, (*read_started(session, export, f'page-{start}'),
                               *read_returned(session, shown(page, start + 1),
                                              f'page-{start}')))
    confirmed, = await bridge.attempts(message.message_id)
    assert confirmed.outcome is DeliveredOutcome
    observed = await bridge.coordinator_read(lambda db: HarnessObservedPart.of(db, confirmed))
    assert ''.join(part.body for part in sorted(observed, key=lambda part: part.number)) == body
    assert not export.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ('succeeded', 'failed'))
async def test_a_consumed_export_stays_until_no_other_read_of_it_is_open(bridge, ending):
    message, export = await exported(bridge, body_of(23))
    text = shown(export.read_text(newline=''))
    session = ClaudeSession()
    starts = (*read_started(session, export, 'first'), *read_started(session, export, 'second'))
    await observe(bridge, (*starts, *read_returned(session, text, 'first')))
    confirmed, = await bridge.attempts(message.message_id)
    assert confirmed.outcome is DeliveredOutcome
    assert export.exists()
    await observe(bridge, read_returned(session, text, 'second', error=ending == 'failed'))
    assert await bridge.attempts(message.message_id) == (confirmed,)
    assert not export.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(('swap', 'refusal'), (
    ('symlink', NotADirectoryError), ('shared', RelationViolationError)))
async def test_an_export_directory_that_is_not_private_refuses(bridge, tmp_path, swap, refusal):
    """make_parents leaves an existing directory unchecked; the export pins and checks it."""
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir(mode=0o700)
    if swap == 'symlink':
        bridge.exports.symlink_to(elsewhere)
    else:
        bridge.exports.mkdir(mode=0o755)
        bridge.exports.chmod(0o755)
    message = bridge.comms.messaging.send('peer', 'viewer', body_of(23)).message
    notice = MonitorReadFile.naming(bridge.exports, message.reference)
    with pytest.raises(refusal):
        notice.export(('"part"',))
    assert list(elsewhere.iterdir()) == []


@pytest.mark.asyncio
async def test_an_export_leaf_symlink_refuses(bridge, tmp_path):
    message = bridge.comms.messaging.send('peer', 'viewer', body_of(23)).message
    notice = MonitorReadFile.naming(bridge.exports, message.reference)
    bridge.exports.mkdir(mode=0o700)
    target = tmp_path / 'target.txt'
    target.write_text('"part"\n')
    Path(notice.path).symlink_to(target)
    with pytest.raises(OSError):
        notice.export(('"part"',))
    assert target.read_text() == '"part"\n'


@pytest.mark.asyncio
async def test_an_export_refuses_other_bytes_at_its_path(bridge):
    message, export = await exported(bridge, body_of(23))
    notice = MonitorReadFile.naming(bridge.exports, message.reference)
    notice.export(tuple(export.read_text(newline='').split('\n')[:-1]))
    with pytest.raises(RelationViolationError):
        notice.export(('"not this message"',))


@pytest.mark.asyncio
async def test_an_uncertain_export_stays_and_reopening_never_replays_it(bridge):
    message, export = await exported(bridge, body_of(23))
    text = export.read_text(newline='')
    # The coordinator's declared wait expires on a held lock; the read stays unproved.
    path = CoordinationStore.path_in(bridge.comms.root)
    with closing(sqlite3.connect(path, isolation_level=None)) as held:
        held.execute('BEGIN EXCLUSIVE')
        try:
            await observe(bridge, read_records(export, shown(text), 'busy-read'))
        finally:
            held.execute('ROLLBACK')
    uncertain, = await bridge.attempts(message.message_id)
    assert uncertain.outcome is EmittedOutcome
    assert export.exists()
    state, binding = bridge.options.state, bridge.binding
    bridge.db.close()
    with Binding.open(state) as db:
        reopened = ClaudeBridge(Options(Watch(), StoredBinding(), state, state.parent),
                                binding.address(), db)
        await reopened.options.operation.prepare(reopened)
        assert await reopened.resume_after() == BridgeStart(message.reference)
        assert await reopened.attempts(message.message_id) == (uncertain,)
        # The same complete visible Read, seen after reopening, still confirms it.
        await observe(reopened, read_records(export, shown(text), 'later-read'))
        confirmed, = await reopened.attempts(message.message_id)
        assert confirmed.outcome is DeliveredOutcome
        assert not export.exists()


@pytest.mark.parametrize('body', ['x'*3000, ('\n"\\😀\t')*500, 'short', 'line one\n\nline three\n'])
@pytest.mark.parametrize('sender', ['claude-code', 'a · b " 😀'])
def test_frame_parts_rebuild_the_body_and_cut_only_at_line_ends(body, sender):
    message=Message(sender=sender,target='viewer',type=InfoKind,body=body,timestamp=1.,seq=1)
    frame=ClaudeMonitorFrame(message)
    shown_rows=tuple(enumerate(frame.export_lines,1))
    assert ''.join(part.body for part in frame.read_parts(shown_rows))==body
    assert all(part.sender==sender for part in frame.read_parts(shown_rows))
    assert all(len(line)<=frame.READ_LINE_CHARS for line in frame.export_lines[1:])
    assert frame.fits_one_event==(frame.event_size(frame.inline)<=frame.EVENT_CHARS)


def test_a_heartbeat_sized_message_is_one_readable_file_not_fragments():
    body='\n'.join(f'row {number} '+'word '*40 for number in range(40))
    message=Message(sender='user',target='viewer',type=InfoKind,body=body,timestamp=1.,seq=1)
    frame=ClaudeMonitorFrame(message)
    assert frame.export_lines[1:]==tuple(body.split('\n'))
    assert sum(line.startswith(frame.HEADER) for line in frame.export_lines)==1


def test_foreign_task_notifications_are_named_and_malformed_matching_xml_fails():
    record=decoded_record(dict(type='user',uuid=transcript_record('record'),timestamp='2026-10-16T16:40:05.123Z',
        sessionId='session',promptId='prompt',origin=dict(kind='task-notification'),
        message=dict(content='Another task producer')))
    assert isinstance(record.message.content[0].text,ForeignTaskNotification)
    assert record.updates(ClaudeSession())==()
    from xml.etree.ElementTree import ParseError
    with pytest.raises(ParseError):
        decoded_record(dict(type='user',uuid=transcript_record('record'),timestamp='2026-10-16T16:40:05.123Z',
            sessionId='session',promptId='prompt',origin=dict(kind='task-notification'),
            message=dict(content='<task-notification>')))
    assert isinstance(ClaudeMonitorFrame.decode_line('ordinary unrelated monitor event'),ForeignMonitorFrame)
    assert isinstance(ClaudeMonitorFrame.decode_line('"Comms in · old format...(truncated)'),ForeignMonitorFrame)


def queued_prompt(kind, prompt):
    return decoded_record(dict(type='attachment', uuid=transcript_record('queued-record'),
        timestamp='2026-10-16T16:40:05.123Z', attachment=dict(type='queued_command',
        origin=dict(kind=kind), prompt=prompt)))


def test_non_task_attachments_and_pending_queue_records_do_not_confirm():
    session=ClaudeSession(turn=KnownTurn(FieldCodec.decode(NativeTurnId, 'turn')))
    for kind in ('peer', 'human'):
        updates=queued_prompt(kind, 'Not a Monitor notification').updates(session)
        assert not any(isinstance(update, MonitorObservation) for update in updates)
    hook=decoded_record(dict(type='attachment', uuid=transcript_record('foreign-record'),
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
    message = await emitted_delivery('original')
    frame = MonitorPart(id=message.message_id, sender=message.sender, number=1, total=1, body='different')
    await frame.confirm(bridge, ObservedMonitor(transcript_record('record'), 1.))
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

@pytest.fixture(params=(FinishedTurn, lambda _turn: AllTurnsEnded()))
def reply_end(request: pytest.FixtureRequest) -> Callable[[NativeTurnId], RuntimeUpdate]:
    return request.param


async def confirm_and_finish_reply(
    bridge: ClaudeBridge, message: Message, expected: tuple[str, ...],
    end: Callable[[NativeTurnId], RuntimeUpdate],
) -> None:
    """Real monitor consumption feeds the ordinary staged output and terminal update."""
    output = bridge.output_type.for_comms(
        bridge.comms.transcripts.external(bridge.binding.bus), bridge.comms)
    async with attached_monitor(bridge) as reader:
        await bridge.accept(message)
        line = await only_event(reader)
        updates = [await update.observed(bridge)
                   for update in notification(line).updates(ClaudeSession())]
        # A long diagnostic path can make the harness request a Read instead of inline text.
        exported = Path(MonitorReadFile.naming(bridge.exports, message.reference).path)
        if exported.is_file():
            for update in read_records(exported, shown(exported.read_text()), 'reply-read'):
                updates.append(await update.observed(bridge))
    delivered, = await bridge.attempts(message.message_id)
    assert delivered.outcome is DeliveredOutcome
    assert isinstance(delivered.monitor, ObservedMonitor)
    assert message.reference not in {
        row.reference for row in bridge.comms.bus.inbox(
            bridge.binding.bus, after=TakenInputStart()).page.messages}

    def replies() -> tuple[tuple[str, str], ...]:
        return tuple((row.target, row.body) for row in bridge.comms.bus.bus.message_history()
                     if row.sender == bridge.binding.bus)

    output.publish_batch(updates)
    # Only the turn the delivery opened answers it.
    with output.transcript.snapshot() as connection:
        opened = ExternalTurnInput.select_keys(connection, (
            dict(thread=output.transcript.lookup, reference=message.reference),))
    # A delivery that opens no turn leaves any other turn's final with nothing to answer.
    turn = next((row.turn_id for row in opened), NativeTurnId('unopened-turn'))
    item = NativeItemId('answer')
    output.publish_batch([FinishedItem(turn, item, (AssistantTranscript(
        'the opened turn final', timestamp=2., position=item),))])
    assert replies() == ()
    output.publish_batch([end(turn)])
    assert replies() == tuple((target, 'the opened turn final') for target in expected)
    # Completion is idempotent, and another turn cannot inherit these routes.
    later, later_item = NativeTurnId('later-turn'), NativeItemId('later-answer')
    output.publish_batch([FinishedTurn(turn), AllTurnsEnded(), FinishedItem(
        later, later_item, (AssistantTranscript(
            'a later final', timestamp=3., position=later_item),)), AllTurnsEnded()])
    assert replies() == tuple((target, 'the opened turn final') for target in expected)


@pytest.mark.asyncio
async def test_diagnostic_notice_is_consumed_without_replying_to_the_failed_sender(bridge, reply_end):
    diagnostic = record_terminal_failure(
        bridge.comms.root, subject=InputId.mint(), thread='peer',
        failure=BackendFailed, sequences=(), source_error=RuntimeError('native request failed'))
    message = bridge.comms.messaging.send(
        'peer', 'viewer', f'Request failed. No automatic retry. [Open diagnostic]({diagnostic.as_uri()})',
        AlertKind, role=NoticeRole()).message
    await confirm_and_finish_reply(bridge, message, (), reply_end)


@pytest.mark.asyncio
@pytest.mark.parametrize(('target', 'body', 'kind', 'expected'), (
    ('viewer', 'ordinary direct request', QuestionKind, ('claude-code',)),
    ('#monitor', 'channel information', InfoKind, ()),
    ('#monitor', '@viewer please answer this channel request', QuestionKind, ()),
))
async def test_confirmed_reply_routes_follow_direct_and_channel_policy(
    bridge, reply_end, target, body, kind, expected,
):
    bridge.comms.channels.create_tag('monitor')
    message = bridge.comms.messaging.send('claude-code', target, body, kind).message
    await confirm_and_finish_reply(bridge, message, expected, reply_end)


@pytest.mark.asyncio
async def test_confirmed_human_direct_request_receives_one_reply(bridge, reply_end):
    message = bridge.comms.messaging.send_user_message(
        ThreadTarget('viewer'), 'please answer once',
        worktree=Checkout(bridge.options.state.parent))
    await confirm_and_finish_reply(bridge, message, (message.sender,), reply_end)


def test_only_commanded_sessions_reply_to_their_delivered_senders(comms):
    """The commander answers its owner in its own transcript: a unit that messaged it
    mid-turn never receives that turn's answer as a direct message."""
    from agent_comms.registry_document import RegisteredCommanderProject
    from agent_comms.runtime_updates import FinishedItem, ReplyingThreadOutput
    from agent_comms.transcript_events import AssistantTranscript
    from agent_comms.turn_item import NativeItemId, NativeTurnId

    lead, unit = (comms.registry.declare(Thread(
        name, frozenset(), Checkout(Path("/tmp")), execution=ExternalThreadExecution)) for name in ("lead", "unit"))
    comms.registry.set_commander_project(RegisteredCommanderProject(lead.incarnation, ()))
    for speaker, sender in ((lead, unit), (unit, lead)):
        output = ReplyingThreadOutput.for_comms(comms.transcripts.external(speaker.name), comms)
        original = comms.messaging.send(sender.name, speaker.name, "Open this answer", QuestionKind).message
        turn = NativeTurnId(f"{speaker.name}-turn")
        output.delivered(turn, (original.reference,))
        item_id = NativeItemId("a")
        output.publish_item(FinishedItem(turn, item_id, (AssistantTranscript(
            f"{speaker.name} answers", timestamp=1.0, position=item_id),)))
        output.end_turn(turn)
    replies = tuple((message.target, message.body) for message in comms.bus.bus.message_history()
                    if message.body in {"lead answers", "unit answers"})
    assert replies == (("lead", "unit answers"),)


@pytest.mark.parametrize(('rows', 'parts'), (
    ((1, 2, 3, 4), ('one\n', 'two')),
    ((1, 2, 3), ('one\n', 'two')),
    ((2, 3, 4), ('one\n', 'two')),
    ((3,), ('two',)),
    ((1,), ()),
    ((1, 2, 3, 4, 5), ()),
))
def test_only_the_one_empty_row_after_the_final_lf_is_the_read_terminator(rows, parts):
    """Claude numbers the empty row after a file's final LF; any row past it is no line of
    this message, so that read confirms nothing."""
    message = Message(sender='peer', target='viewer', type=InfoKind, body='one\ntwo',
                      timestamp=1., seq=1)
    frame = ClaudeMonitorFrame(message)
    physical = (*frame.export_lines, '', '')
    read = tuple((number, physical[number - 1]) for number in rows)
    assert tuple(part.body for part in frame.read_parts(read)) == parts


def test_a_gap_at_the_terminator_number_makes_no_line_count():
    observed = ReadResultObserved('export', '1\t"x"\n3\t',
                                  ObservedMonitor(transcript_record('read'), 1.), frozenset())
    assert observed.rows() == ()
