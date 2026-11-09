"""Session-owned parts, not stdout writes, confirm monitor delivery."""
import asyncio
import json
import os
import shutil
import socket
import sqlite3
import stat
from collections.abc import Callable
from contextlib import asynccontextmanager, closing, contextmanager, suppress
from dataclasses import replace
from functools import partial
from html import escape
from pathlib import Path

import pytest

from agent_comms.cli_bridges.claude import ClaudeBridge
from agent_comms.cli_bridges.claude_protocol import (
    ClaudeMonitorFrame,
    ClaudeSession,
    ForeignAttachment,
    ForeignTaskNotification,
    KnownTurn,
    ReadResultObserved,
)
from agent_comms.cli_bridges.claude_transcript import ClaudeJsonLines
from agent_comms.cli_bridges.harness_delivery_states import (
    DeliveredOutcome,
    EmittedOutcome,
    MismatchedMonitor,
    ObservedMonitor,
    ReceivingMonitor,
    UnconfirmedOutcome,
    UnrecordedMonitor,
)
from agent_comms.cli_bridges.monitor import MonitorPeer
from agent_comms.cli_bridges.runtime_updates import (
    AllTurnsEnded,
    FinishedItem,
    FinishedTurn,
    HiddenItem,
    RuntimeUpdate,
)
from agent_comms.cli_bridges.shared import (
    MONITOR_FRAME,
    Binding,
    CoreWatchEnded,
    ForeignMonitorFrame,
    GivenBinding,
    MonitorObservation,
    MonitorPart,
    MonitorReadFile,
    MonitorService,
    Options,
    StoredBinding,
    Watch,
    core_watch_failure,
)
from agent_comms.comms import wire
from agent_comms.coordination.assignment_states import DeferredAssignment, DeliveredAssignment
from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.database import CoordinationStore
from agent_comms.coordination.errors import CoordinationReadUnavailable
from agent_comms.coordination.tables.assignments import WakeAssignment
from agent_comms.coordination.tables.harness_deliveries import HarnessDelivery, HarnessObservedPart
from agent_comms.errors import RelationViolationError
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.diagnostics import record_terminal_failure
from agent_comms.execution.launch_runtime import (
    InstalledSource,
    LaunchRuntime,
    RuntimeReplacedError,
)
from agent_comms.execution.turn_failure import BackendFailed
from agent_comms.execution.turn_item import (
    NativeItemId,
    NativeTurnId,
)
from agent_comms.installer.practice import PracticeCopy
from agent_comms.messaging.agent_inbox import TakenInputStart
from agent_comms.messaging.channel_targets import ThreadTarget
from agent_comms.messaging.message_reference import BridgeStart, MessageReference
from agent_comms.messaging.messages import AlertKind, InfoKind, Message, NoticeRole, QuestionKind
from agent_comms.messaging.read_ledger import ReadLedger
from agent_comms.nominal.field_codec import FieldCodec, InputId
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import ExternalThread, NativeThread
from agent_comms.storage.locked_store import RootFile
from agent_comms.storage.maintenance_barrier import (
    MaintenanceBarrier,
    MaintenanceHeldError,
    MaintenanceOperator,
)
from agent_comms.transcript.external_transcript_rows import ExternalTurnInput
from agent_comms.transcript.transcript_events import AssistantTranscript
from bus_record_fixture import prepare_native_comms
from conftest import accept_initial, declare_test_bus, publish_initial
from transcript_record_fixture import transcript_record


async def stored_attempts(bridge) -> tuple[HarnessDelivery, ...]:
    """This bridge's attempts for its recipient, as the coordinator stores them."""
    return await bridge.coordinator_read(lambda db: tuple(HarnessDelivery.select(
        db, bridge=type(bridge), recipient_lookup=bridge.recipient_lookup, order_by=("seq",))))


@pytest.mark.asyncio
async def test_empty_journal_starts_at_binding_and_ignores_user_reads(comms, tmp_path):
    declare_test_bus(comms, threads=tuple(
        NativeThread(name, frozenset(), Checkout(tmp_path)) for name in ('viewer', 'peer')
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
        async with attached_monitor(selected) as monitor_reader:
            watching = asyncio.create_task(selected.watch_bus())
            try:
                line = await event_from(monitor_reader)
                for update in notification(line).updates(ClaudeSession()):
                    await update.observed(selected)
                row, = await stored_attempts(selected)
                assert row.reference == fresh.reference
                assert row.outcome is DeliveredOutcome
                assert isinstance(row.monitor, ObservedMonitor)
                assert comms.bus.reads.read() == user_reads
                unread = comms.messaging.send(
                    'peer', 'viewer', 'delivery records this model read').message
                line = await event_from(monitor_reader)
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
                watching.cancel()
                with suppress(asyncio.CancelledError):
                    await watching


@pytest.fixture
def bridge(comms, tmp_path):
    declare_test_bus(comms, threads=(
        NativeThread('claude-code', frozenset(), Checkout(tmp_path)),
        # A production bridge's session is external: its answers live in the external store.
        ExternalThread('viewer', frozenset(), Checkout(tmp_path)),
        NativeThread('peer', frozenset(), Checkout(tmp_path)),
    ))
    practice = PracticeCopy(comms.root, tmp_path / 'copied-bus')
    practice.include(tuple(RootFile.existing_in(practice.root)))
    root = practice.path
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
                line = await event_from(reader)
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
                line = await event_from(reader)
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


def coordination(comms):
    return Coordination(str(CoordinationStore.path_in(comms.root)))


def native_viewer(tmp_path):
    """A native-root bus whose external viewer is a committed coordination participant."""
    comms = prepare_native_comms(wire(tmp_path / 'wire'))
    comms.registry.declare(NativeThread('peer', frozenset(), Checkout(tmp_path)))
    viewer = comms.registry.declare(ExternalThread(
        'viewer', frozenset(), Checkout(tmp_path)))
    lookup = viewer.incarnation.require_recorded().lookup
    root_id = comms.registry.store.prepare_native_root().namespace
    with coordination(comms) as store:
        store.participants.register(lookup, committed=True)
    return comms, root_id, lookup


def assignment(comms, lookup, message):
    """The viewer's wake assignment for this message, as the coordinator stores it."""
    with coordination(comms) as store:
        row, = WakeAssignment.select(store.session._connection,
                                     recipient_lookup=lookup, wire_seq=message.seq)
        return row.lifecycle


@contextmanager
def bound_bridge(comms, tmp_path, owner):
    """A Claude bridge bound to the bus head for ``owner``; preparation is the caller's."""
    state = tmp_path / 'bridge-state'
    with Binding.open(state) as db:
        binding = Binding(root=str(comms.root), owner=owner,
                          thread='session', start=Binding.bus_head(str(comms.root)))
        binding.bind(db)
        yield ClaudeBridge(Options(Watch(), StoredBinding(), state, state.parent), binding.address(), db)


@pytest.mark.asyncio
async def test_renamed_reader_handoff_and_confirmation_share_the_prepared_owner(tmp_path):
    comms, root_id, lookup = native_viewer(tmp_path)
    original = comms.registry.require('viewer')
    with bound_bridge(comms, tmp_path, original.incarnation) as bridge:
        comms.registry.rename('viewer', 'renamed-viewer')
        await bridge.options.operation.prepare(bridge)
        assert bridge.binding.bus == 'renamed-viewer'
        assert bridge.binding.owner == original.incarnation
        with coordination(comms) as store:
            message = publish_initial(comms, 'peer', bridge.binding.bus, 'same reader').message
            accept_initial(comms.bus, root_id, message.seq, store)
        human_reads = ReadLedger.at(comms.root).read()
        async with attached_monitor(bridge) as monitor_reader:
            accepting = asyncio.create_task(bridge.accept(message))
            try:
                line = await event_from(monitor_reader)
                await accepting
                assert type(assignment(comms, lookup, message)) is DeferredAssignment
                for update in notification(line).updates(ClaudeSession()):
                    await update.observed(bridge)
                assert type(assignment(comms, lookup, message)) is DeliveredAssignment
                # The harness took it, so the agent's page drops it; the human's unread view and
                # read ledger are a different owner and do not move.
                assert comms.bus.inbox(bridge.binding.bus, after=TakenInputStart()).page.messages == ()
                assert message.reference in {
                    item.reference for item in comms.bus.inbox(bridge.binding.bus).page.messages}
                assert ReadLedger.at(comms.root).read() == human_reads
            finally:
                accepting.cancel()
                with suppress(asyncio.CancelledError):
                    await accepting


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
    return 'x' * 50 if parts == 1 else '\n'.join(('x' * 1500,) * parts)


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


async def event_from(reader):
    """The lines the reader gets for one message, joined as Monitor joins one batch."""
    lines = [(await reader.readline()).decode().rstrip('\n')]
    try:
        while True:
            async with asyncio.timeout(.3):
                lines.append((await reader.readline()).decode().rstrip('\n'))
    except TimeoutError:
        return '\n'.join(lines)


async def only_event(reader):
    """One message's event: every line under the line cap, the batch under the batch cap."""
    event = await event_from(reader)
    assert all(ClaudeMonitorFrame.units(line) <= ClaudeMonitorFrame.EVENT_CHARS
               for line in event.split('\n'))
    assert ClaudeMonitorFrame.units(event) <= ClaudeMonitorFrame.BATCH_CHARS
    return event


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
        shown, = ClaudeMonitorFrame.decode_event(line)
        assert shown.reference == message.reference
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
        assert lines[0] == ClaudeMonitorFrame(message).header.removesuffix('\n')
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
@pytest.mark.parametrize('separator', ('\u2028', '\x85', '\u2029'))
async def test_line_separators_inside_a_body_stay_inside_their_part(bridge, separator):
    """A transcript keeps U+2028, U+0085 and U+2029 raw; only a physical LF ends a line,
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
@pytest.mark.parametrize('character', ('\x1c', '\r', '\x07'))
async def test_a_character_the_xml_envelope_cannot_carry_sends_the_body_to_the_file(bridge, character):
    """A transcript records a Monitor event inside XML, which cannot hold a control character
    raw and reads a CR as a line break, so such a body is the file, byte for byte."""
    body = f'a{character}b'
    message, export = await exported(bridge, body)
    assert export.read_text(newline='').split('\n', 1)[1] == body + '\n'


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
    assert frame.fits_one_event==(frame.units(frame.shown_inline())<=frame.BATCH_CHARS
                                  and frame.shows_safely_inline)


def test_a_heartbeat_sized_message_is_one_readable_file_not_fragments():
    body='\n'.join(f'row {number} '+'word '*40 for number in range(40))
    message=Message(sender='user',target='viewer',type=InfoKind,body=body,timestamp=1.,seq=1)
    frame=ClaudeMonitorFrame(message)
    assert frame.export_lines[1:]==tuple(body.split('\n'))
    assert sum(line.startswith(frame.HEADER) for line in frame.export_lines)==1


def test_foreign_task_notifications_are_named_and_one_without_an_event_is_foreign():
    record=decoded_record(dict(type='user',uuid=transcript_record('record'),timestamp='2026-10-16T16:40:05.123Z',
        sessionId='session',promptId='prompt',origin=dict(kind='task-notification'),
        message=dict(content='Another task producer')))
    assert isinstance(record.message.content[0].text,ForeignTaskNotification)
    assert record.updates(ClaudeSession())==()
    unended=decoded_record(dict(type='user',uuid=transcript_record('record'),timestamp='2026-10-16T16:40:05.123Z',
        sessionId='session',promptId='prompt',origin=dict(kind='task-notification'),
        message=dict(content='<task-notification>')))
    assert isinstance(unended.message.content[0].text,ForeignTaskNotification)
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


def test_session_owner_record_decodes_without_updates():
    record = decoded_record({'type': 'session-owner',
        'accountUuid': '00000000-0000-4000-8000-000000000002',
        'organizationUuid': '00000000-0000-4000-8000-000000000003',
        'sessionId': '00000000-0000-4000-8000-000000000001'})
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
    from agent_comms.coordination.cohort import pending_sealed_assignments

    comms, root_id, lookup = native_viewer(tmp_path)
    with coordination(comms) as store:

        def publish(body):
            message = publish_initial(comms, 'peer', 'viewer', body).message
            accept_initial(comms.bus, root_id, message.seq, store)
            return message

        taken, confirmed, waiting = (publish(body) for body in (
            'taken, start unproved', 'taken and confirmed', 'never reached the harness'))

    def owed():
        with coordination(comms) as store:
            return tuple(row.wire_seq for row in pending_sealed_assignments(store, lookup, after_seq=0))

    with bound_bridge(comms, tmp_path, comms.registry.require('viewer').incarnation) as bridge:
        await bridge.options.operation.prepare(bridge)
        async with attached_monitor(bridge) as reader:
            assert owed() == (taken.seq, confirmed.seq, waiting.seq)
            await bridge.accept(taken)
            await event_from(reader)
            await bridge.accept(confirmed)
            line = await event_from(reader)
            for update in notification(line).updates(ClaudeSession()):
                await update.observed(bridge)
            await bridge.accept(taken)

    # Whatever execution owns the thread next reads this one record: only the
    # message no harness took is still owed, and the UNKNOWN one never replays.
    assert owed() == (waiting.seq,)
    unknown = assignment(comms, lookup, taken)
    assert unknown == DeferredAssignment.build(unknown.mode, None, None)
    assert type(assignment(comms, lookup, confirmed)) is DeliveredAssignment


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
    comms, root_id, lookup = native_viewer(tmp_path)
    with coordination(comms) as store:
        message = publish_initial(comms, 'peer', 'viewer', 'held during install').message
        accept_initial(comms.bus, root_id, message.seq, store)

    barrier = MaintenanceBarrier(comms.registry.store.path)
    holds = []

    async def waits_until_cleared(pending):
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(.2):
                await asyncio.shield(pending)
        barrier.clear(holds.pop())

    with bound_bridge(comms, tmp_path, comms.registry.require('viewer').incarnation) as bridge:
        await bridge.options.operation.prepare(bridge)
        async with attached_monitor(bridge) as reader:
            holds.append(barrier.begin(MaintenanceOperator.current()))
            pending = asyncio.create_task(bridge.accept(message))
            await waits_until_cleared(pending)  # the hand-off to the harness
            await asyncio.wait_for(pending, 5)
            line = await event_from(reader)
            holds.append(barrier.begin(MaintenanceOperator.current()))

            async def observed():
                for update in notification(line).updates(ClaudeSession()):
                    await update.observed(bridge)

            pending = asyncio.create_task(observed())
            await waits_until_cleared(pending)  # the confirmation record
            await asyncio.wait_for(pending, 5)
    assert type(assignment(comms, lookup, message)) is DeliveredAssignment



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
    output.publish_batch([FinishedItem(turn_id=turn, item_id=item, events=(AssistantTranscript(
        'the opened turn final', timestamp=2., position=item),))])
    assert replies() == ()
    output.publish_batch([end(turn)])
    assert replies() == tuple((target, 'the opened turn final') for target in expected)
    # Completion is idempotent, and another turn cannot inherit these routes.
    later, later_item = NativeTurnId('later-turn'), NativeItemId('later-answer')
    output.publish_batch([FinishedTurn(turn), AllTurnsEnded(), FinishedItem(
        turn_id=later, item_id=later_item, events=(AssistantTranscript(
            'a later final', timestamp=3., position=later_item),)), AllTurnsEnded()])
    assert replies() == tuple((target, 'the opened turn final') for target in expected)


@pytest.mark.asyncio
async def test_diagnostic_notice_is_consumed_without_replying_to_the_failed_sender(bridge, reply_end):
    diagnostic = record_terminal_failure(
        bridge.comms.root, subject=InputId.mint(), thread='peer',
        failure=BackendFailed('native request failed'), sequences=(),
        source_error=RuntimeError('native request failed'))
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


def test_independent_sessions_share_the_delivered_message_reply_policy(comms, tmp_path):
    """Storage colleague and peer follow the same original-message reply policy."""
    from agent_comms.cli_bridges.runtime_updates import ReplyingThreadOutput
    from agent_comms.registry.registry_document import RegisteredTeamProject

    colleague, teammate = (comms.registry.declare(ExternalThread(
        name, frozenset(), Checkout(tmp_path))) for name in ("colleague", "teammate"))
    comms.registry.set_team_project(RegisteredTeamProject(()))
    for speaker, sender in ((colleague, teammate), (teammate, colleague)):
        output = ReplyingThreadOutput.for_comms(comms.transcripts.external(speaker.name), comms)
        original = comms.messaging.send(sender.name, speaker.name, "Open this answer", QuestionKind).message
        turn = NativeTurnId(f"{speaker.name}-turn")
        output.delivered(turn, (original.reference,))
        item_id = NativeItemId("a")
        output.publish_item(FinishedItem(turn_id=turn, item_id=item_id, events=(AssistantTranscript(
            f"{speaker.name} answers", timestamp=1.0, position=item_id),)))
        output.end_turn(turn)
    replies = tuple((message.target, message.body) for message in comms.bus.bus.message_history()
                    if message.body in {"colleague answers", "teammate answers"})
    assert replies == (("teammate", "colleague answers"), ("colleague", "teammate answers"))


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


async def delivered_line(selected, reader, record):
    """The real monitor receives one message's event and confirms it, as the session would."""
    line = await event_from(reader)
    for update in notification(line, record_id=transcript_record(record)).updates(ClaudeSession()):
        await update.observed(selected)
    return line


async def stopped(task):
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_the_bus_read_survives_a_restart_without_repeats(bridge):
    async with attached_monitor(bridge) as reader:
        first = bridge.comms.messaging.send('peer', 'viewer', 'arrives before the read').message
        watching = asyncio.create_task(bridge.watch_bus())
        await delivered_line(bridge, reader, 'first')
        during = bridge.comms.messaging.send('peer', 'viewer', 'arrives during the read').message
        await delivered_line(bridge, reader, 'during')
        await stopped(watching)
        after = bridge.comms.messaging.send('peer', 'viewer', 'arrives while it is down').message
        watching = asyncio.create_task(bridge.watch_bus())
        await delivered_line(bridge, reader, 'after')
        await stopped(watching)
        rows = await stored_attempts(bridge)
        assert [row.reference for row in rows] == [first.reference, during.reference, after.reference]


@pytest.mark.asyncio
async def test_a_bridge_whose_runtime_an_install_replaced_stops_instead_of_reading_on(bridge, tmp_path, monkeypatch):
    """when_open re-runs the watch after every maintenance hold, and every install holds one: a
    bridge running the replaced runtime ends loudly, reads nothing, and is started anew."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    newer = tmp_path / "runtimes/core-newer"
    newer.mkdir(parents=True)
    pointer = LaunchRuntime.current_pointer()
    pointer.parent.mkdir(parents=True)
    pointer.symlink_to(newer, target_is_directory=True)
    bridge.running_source = InstalledSource(tmp_path / "site")
    bridge.comms.messaging.send('peer', 'viewer', 'never read by the old runtime')
    with pytest.raises(RuntimeReplacedError, match="Start it again"):
        await asyncio.wait_for(bridge.comms.bus.when_open(bridge.watch_current_bus), 10)
    assert await stored_attempts(bridge) == ()


@pytest.mark.asyncio
async def test_a_maintenance_hold_waits_and_resumes_without_loss_or_repeat(bridge):
    async with attached_monitor(bridge) as reader:
        before = bridge.comms.messaging.send('peer', 'viewer', 'before the hold').message
        barrier = MaintenanceBarrier(bridge.comms.registry.store.path)
        held = barrier.begin(MaintenanceOperator.current())
        watching = None
        try:
            watching = asyncio.create_task(bridge.comms.bus.when_open(bridge.watch_bus))
            await asyncio.sleep(0.2)
            assert not watching.done()
        finally:
            barrier.clear(held)
        await delivered_line(bridge, reader, 'before-hold')
        after = bridge.comms.messaging.send('peer', 'viewer', 'after the hold').message
        await delivered_line(bridge, reader, 'after-hold')
        await stopped(watching)
        rows = await stored_attempts(bridge)
        assert [row.reference for row in rows] == [before.reference, after.reference]


@pytest.mark.asyncio
@pytest.mark.parametrize('failure, stops', (
    (OSError('the read failed'), CoreWatchEnded),
    (MaintenanceHeldError('held'), MaintenanceHeldError),
))
async def test_a_read_failure_stops_only_after_the_delivery_under_way_finishes(failure, stops):
    lines = asyncio.Queue()
    release = asyncio.Event()
    finished = []

    async def delivery():
        await release.wait()
        finished.append('delivered')

    lines.put_nowait(delivery)
    delivering = asyncio.create_task(ClaudeBridge.deliver_lines(lines))
    await asyncio.sleep(0)
    with core_watch_failure(lines):
        raise failure
    await asyncio.sleep(0)
    assert not delivering.done() and not finished
    release.set()
    with pytest.raises(stops):
        await delivering
    assert finished == ['delivered']


def test_a_failure_outside_the_reported_family_crashes_instead_of_queueing():
    lines = asyncio.Queue()
    with pytest.raises(LookupError):
        with core_watch_failure(lines):
            raise LookupError('a defect')
    assert lines.empty()


@pytest.mark.asyncio
async def test_a_held_store_during_the_read_keeps_the_bridge_up_and_delivers_once(bridge, capsys):
    """Real contention on the store a busy read reports: it waits, it does not end the watch."""
    path = CoordinationStore.path_in(bridge.comms.root)
    async with attached_monitor(bridge, timeout=120) as reader:
        sent = bridge.comms.messaging.send('peer', 'viewer', 'read while the store is held').message
        with closing(sqlite3.connect(path, isolation_level=None)) as held:
            held.execute('BEGIN EXCLUSIVE')
            watching = asyncio.create_task(bridge.comms.bus.when_open(bridge.watch_bus))
            seen = ''
            async with asyncio.timeout(60):
                while 'Watch read waits for the store' not in seen:
                    await asyncio.sleep(.01)
                    seen += capsys.readouterr().err
            assert not watching.done(), seen
            held.execute('ROLLBACK')
        await delivered_line(bridge, reader, 'held-store')
        assert not watching.done()
        await stopped(watching)
        rows = await stored_attempts(bridge)
        assert [row.reference for row in rows] == [sent.reference]
