"""Session-owned parts, not stdout writes, confirm monitor delivery."""
from contextlib import closing
from html import escape
import json
import sqlite3

import pytest

from agent_comms.cli_bridges.claude import ClaudeBridge, ClaudeJsonLines
from agent_comms.cli_bridges.claude_protocol import (
    ClaudeSession, ClaudeMonitorFrame, ForeignTaskNotification,
    ForeignAttachment,
)
from agent_comms.cli_bridges.shared import (
    Binding, DeliveryRow, ObservedPart, ReceivingMonitor, UnrecordedMonitor, MonitorPart,
    ForeignMonitorFrame, Options, ObservedMonitor, MismatchedMonitor, Watch,
)
from agent_comms.errors import RelationViolationError
from agent_comms.cli_protocol import UnconfirmedOutcome, EmittedOutcome, DeliveredOutcome
from agent_comms.messages import InfoKind, Message
from agent_comms.read_basis import ExplicitWatchAcknowledgment


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
async def test_parts_require_complete_exact_body_then_reopen(tmp_path, capsys, record_factory):
    state=tmp_path/'state'
    acknowledgments=[]
    class Core:
        def __init__(self):
            self.messaging=self
        def acknowledge(self,thread,references):
            acknowledgments.extend(references)
    with Binding.open(state) as db:
        binding=Binding(str(tmp_path),'viewer','session')
        Binding.bind(db, binding)
        bridge=ClaudeBridge(Options(Watch(),None,state),binding,db)
        bridge.comms=Core()
        message=Message(sender='claude-code',target='viewer',type=InfoKind,body='x'*3000,timestamp=1.,seq=1)
        with db:
            assert DeliveryRow(message.message_id,json.dumps(message.to_wire()),UnconfirmedOutcome).insert_if_absent(db).rowcount
        parts=ClaudeMonitorFrame(message).parts
        with db:
            DeliveryRow.update(db, where='id=?', parameters=(message.message_id,), monitor=ReceivingMonitor(len(parts)), outcome=EmittedOutcome)
        lines=[json.dumps(part,ensure_ascii=False) for part in parts]
        assert len(lines)==7
        assert all(len(line.encode('utf-16-le'))//2 <= ClaudeMonitorFrame.EVENT_CHARS for line in lines)
        # Out-of-order observations and repeated history must not count twice.
        for number in (6,1,3,1,5,2):
            for update in record_factory(lines[number],record_id=f'record-{number}').updates(ClaudeSession()):
                await update.observed(bridge)
        assert not acknowledgments
        assert DeliveryRow.select(db)[0].outcome is EmittedOutcome
    with Binding.open(state) as db:
        bridge=ClaudeBridge(Options(Watch(),None,state),binding,db)
        bridge.comms=Core()
        for number in (0,4):
            for update in record_factory(lines[number],record_id=f'record-{number}').updates(ClaudeSession()):
                await update.observed(bridge)
        assert acknowledgments==[message.reference]
        for line in lines:
            for update in record_factory(line,record_id='later-overlap').updates(ClaudeSession()):
                await update.observed(bridge)
        assert acknowledgments==[message.reference]
    with Binding.open(state) as db:
        assert DeliveryRow.select(db)[0].outcome is DeliveredOutcome
        assert DeliveryRow.select(db)[0].monitor==ObservedMonitor('record-4',1792168805.123)
        assert len(ObservedPart.select(db))==7


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
async def test_wrong_complete_parts_stay_pending_without_claiming_delivery(tmp_path,capsys):
    with Binding.open(tmp_path) as db:
        binding=Binding(str(tmp_path),'viewer','session')
        bridge=ClaudeBridge(Options(Watch(),None,tmp_path),binding,db)
        message=Message(sender='peer',target='viewer',type=InfoKind,body='original',timestamp=1.,seq=1)
        with db:
            DeliveryRow(message.message_id,json.dumps(message.to_wire()),EmittedOutcome,ReceivingMonitor(1)).insert(db)
        frame=MonitorPart(id=message.message_id,sender='peer',number=1,total=1,body='different')
        await frame.confirm(bridge,ObservedMonitor('record',1.))
        assert DeliveryRow.select(db)[0].outcome is EmittedOutcome
        assert isinstance(DeliveryRow.select(db)[0].monitor,MismatchedMonitor)
        assert 'message remains pending' in capsys.readouterr().err


def test_old_emission_has_no_invented_evidence_or_replay(tmp_path):
    message=Message(sender='peer',target='viewer',type=InfoKind,body='retained',timestamp=1.,seq=1)
    with Binding.open(tmp_path) as db:
        DeliveryRow(message.message_id,json.dumps(message.to_wire()),EmittedOutcome).insert(db)
        frame=MonitorPart(id=message.message_id,sender='peer',number=1,total=1,body='retained')
        assert isinstance(DeliveryRow.select(db)[0].monitor,UnrecordedMonitor)
        assert not DeliveryRow(message.message_id,json.dumps(message.to_wire()),UnconfirmedOutcome).insert_if_absent(db).rowcount
        assert DeliveryRow.select(db)[0].outcome is EmittedOutcome
        assert ObservedPart.select(db)==[]


@pytest.mark.asyncio
async def test_explicit_watch_does_not_mark_streamed_messages_read(wired):
    from contextlib import aclosing
    import asyncio
    first=wired.messaging.send('PR111','fixer','first').message
    async with aclosing(wired.bus.watch('fixer', acknowledgment=ExplicitWatchAcknowledgment)) as stream:
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
    assert {m.reference for m in wired.bus.inbox('fixer')}=={first.reference,second.reference}
    assert wired.messaging.acknowledge('fixer',(first.reference,))==1
    assert [m.reference for m in wired.bus.inbox('fixer')]==[second.reference]


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


def test_old_delivery_index_set_is_refused_not_kept(tmp_path):
    path = tmp_path / 'delivery.sqlite3'
    with closing(sqlite3.connect(path)) as db:
        for statement in _OLD_DELIVERY_FILE:
            db.execute(statement)
        db.commit()
    before = path.read_bytes()
    with pytest.raises(RelationViolationError, match='BridgeStateTable differs'):
        with Binding.open(tmp_path):
            pass
    assert path.read_bytes() == before
