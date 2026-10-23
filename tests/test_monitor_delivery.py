"""Session-owned parts, not stdout writes, confirm monitor delivery."""
from contextlib import closing
from html import escape
import json
import sqlite3

from collections import Counter

import pytest

from conftest import declare_test_bus

from agent_comms.installer.practice import PracticeCopy
from agent_comms.locked_store import RootFile

from agent_comms.cli_bridges.claude import ClaudeBridge, ClaudeJsonLines
from agent_comms.cli_bridges.claude_protocol import (
    ClaudeSession, ClaudeMonitorFrame, ForeignTaskNotification,
    ForeignAttachment,
)
from agent_comms.cli_bridges.shared import (
    BridgeStateTable, Binding, DeliveryRow, ObservedPart, ReceivingMonitor, UnrecordedMonitor, MonitorPart,
    ForeignMonitorFrame, Options, ObservedMonitor, MismatchedMonitor, Watch, StoredBinding, Operation,
)
from agent_comms.errors import MessageAcknowledgmentError
from agent_comms.cli_protocol import UnconfirmedOutcome, EmittedOutcome, DeliveredOutcome
from agent_comms.messages import InfoKind, Message
from agent_comms.read_basis import ExplicitWatchAcknowledgment
from agent_comms.threads import Thread


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
    binding = Binding(str(root), 'viewer', 'session')
    with Binding.open(state) as db:
        binding.bind(db)
        yield ClaudeBridge(Options(Watch(), StoredBinding(), state), binding, db)


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
        reopened = ClaudeBridge(Options(Watch(), StoredBinding(), state), binding, db)
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
        assert DeliveryRow.select(db)[0].outcome is DeliveredOutcome
        assert DeliveryRow.select(db)[0].monitor == ObservedMonitor('record-4', 1792168805.123)
        assert len(ObservedPart.select(db)) == 7


@pytest.mark.asyncio
async def test_failed_core_acknowledgment_stays_unconfirmed_and_is_logged(bridge, emitted_delivery, capsys):
    delivery = emitted_delivery('not routed to this viewer', target='peer')
    lines = [bridge.encode(part) for part in bridge.incoming_frame(delivery.message).parts]
    before = bridge.comms.bus.reads.read()

    class Confirm(Operation):
        @classmethod
        def serves(cls, bridge_type):
            return False  # This controlled transcript operation is not a CLI command.

        async def execute(self, selected):
            for number, line in enumerate(lines):
                for update in notification(line, record_id=f'failed-{number}').updates(ClaudeSession()):
                    await update.observed(selected)

    bridge.options.operation = Confirm()
    with pytest.raises(ExceptionGroup) as caught:
        await bridge.run()
    assert tuple(type(error) for error in caught.value.exceptions) == (MessageAcknowledgmentError,)
    error, = caught.value.exceptions
    assert error.recipient == bridge.binding.bus
    assert error.references == (delivery.message.reference,)
    saved, = DeliveryRow.select(bridge.db, id=delivery.id)
    assert saved.outcome is EmittedOutcome
    assert saved.monitor == delivery.monitor
    assert bridge.comms.bus.reads.read() == before
    assert delivery.id in capsys.readouterr().err


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


# delivery.sqlite3 as bridges wrote it before the sequence index was declared:
# its outcome index held the name the sequence index now declares.
_OLD_DELIVERY_FILE = (
    'CREATE TABLE "binding" ("root" TEXT NOT NULL, "bus" TEXT NOT NULL, "thread" TEXT NOT NULL) STRICT',
    'CREATE TABLE "delivery" ("id" TEXT NOT NULL, "payload" TEXT NOT NULL, "outcome" TEXT NOT NULL'
    ' CHECK ("outcome" IN (\'filtered\', \'unconfirmed\', \'delivered\', \'emitted\')),'
    ' "monitor" TEXT NOT NULL, PRIMARY KEY ("id")) STRICT',
    'CREATE INDEX "delivery_0_idx" ON "delivery" ("outcome")',
    'CREATE INDEX "delivery_sequence" ON "delivery" (json_extract(payload, \'$.seq\'))',
    'CREATE TABLE "observed_part" ("id" TEXT NOT NULL, "number" INTEGER NOT NULL, "sender" TEXT NOT NULL,'
    ' "body" TEXT NOT NULL, "record_id" TEXT NOT NULL, "timestamp" REAL NOT NULL,'
    ' PRIMARY KEY ("id", "number")) STRICT',
)


_OLD_DELIVERY_ROWS = (
    ('binding', ('/root', 'viewer', 'session')),
    ('delivery', ('m1', '{"seq":7}\n', 'unconfirmed', '{"kind":"receiving","total":1}')),
    ('delivery', ('m2', '{"seq":8}\n', 'emitted', '{"kind":"unrecorded"}')),
    ('observed_part', ('m1', 1, 'peer', 'part one', 'record-1', 12.5)),
)


def _old_delivery_file(state):
    path = state / BridgeStateTable.filename
    with closing(sqlite3.connect(path)) as db:
        for statement in _OLD_DELIVERY_FILE:
            db.execute(statement)
        for table, values in _OLD_DELIVERY_ROWS:
            db.execute(f'INSERT INTO "{table}" VALUES ({",".join("?" for _ in values)})', values)
        db.commit()
    return path


def test_old_delivery_index_set_is_carried_onto_the_declaration_with_every_row(tmp_path):
    """A durable store derives its declared schema on the writer's open (a5b3cd8a7): the old
    index set is replaced by the declared one and no row is lost."""
    _old_delivery_file(tmp_path)
    with Binding.open(tmp_path) as db:
        assert not BridgeStateTable.schema_differs(db)
        assert {table: db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                for table in ('binding', 'delivery', 'observed_part')} == Counter(
            table for table, _ in _OLD_DELIVERY_ROWS)
