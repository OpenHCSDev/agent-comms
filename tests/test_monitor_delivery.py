"""Session-owned parts, not stdout writes, confirm monitor delivery."""
from contextlib import closing
from html import escape
import json

import pytest

from agent_comms.cli_bridges.claude import ClaudeBridge
from agent_comms.cli_bridges.claude_protocol import (
    CLAUDE_RECORD, ClaudeSession, ClaudeMonitorFrame, ForeignTaskNotification,
    ForeignAttachment,
)
from agent_comms.cli_bridges.shared import (
    Binding, DeliveryJournal, DeliveryRow, MonitorAttemptRow, MonitorPartRow,
    MonitorPart, ForeignMonitorFrame, Options, UnconfirmedOutcome, EmittedOutcome,
    DeliveredOutcome, ObservedMonitor, MismatchedMonitor, NO_MONITOR_ATTEMPT, Watch,
)
from agent_comms.messages import InfoKind, Message
from agent_comms.read_basis import ExplicitWatchAcknowledgment


def notification(line, record_id='notification-record'):
    return CLAUDE_RECORD.validate_python(dict(
        type='user', uuid=record_id, timestamp='2026-10-16T16:40:05.123Z',
        sessionId='session', promptId='prompt', origin=dict(kind='task-notification'),
        message=dict(content='<task-notification><event>'+escape(line)+
                     '</event></task-notification>')))


def attachment_notification(line, record_id='notification-record'):
    return CLAUDE_RECORD.validate_python(dict(
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
        async def acknowledge(self,message):
            acknowledgments.append(message.reference)
    with closing(DeliveryJournal(state)) as journal:
        binding=Binding(str(tmp_path),'viewer','session')
        journal.bind(binding)
        bridge=ClaudeBridge(Options(Watch(),None,None,None,state),binding,journal)
        bridge.core=Core()
        message=Message(sender='claude-code',target='viewer',type=InfoKind,body='x'*3000,timestamp=1.,seq=1)
        assert journal.begin(message,json.dumps(message.to_wire()),UnconfirmedOutcome)
        parts=ClaudeMonitorFrame(message).parts
        journal.emitting(message,len(parts))
        journal.finish(message,EmittedOutcome)
        lines=[json.dumps(part,ensure_ascii=False) for part in parts]
        assert len(lines)==7
        assert all(len(line.encode('utf-16-le'))//2 <= ClaudeMonitorFrame.EVENT_CHARS for line in lines)
        # Out-of-order observations and repeated history must not count twice.
        for number in (6,1,3,1,5,2):
            for update in record_factory(lines[number],record_id=f'record-{number}').updates(ClaudeSession()):
                await update.observed(bridge)
        assert not acknowledgments
        assert DeliveryRow.select(journal.db)[0].outcome is EmittedOutcome
    with closing(DeliveryJournal(state)) as journal:
        bridge=ClaudeBridge(Options(Watch(),None,None,None,state),binding,journal)
        bridge.core=Core()
        for number in (0,4):
            for update in record_factory(lines[number],record_id=f'record-{number}').updates(ClaudeSession()):
                await update.observed(bridge)
        assert acknowledgments==[message.reference]
        for line in lines:
            for update in record_factory(line,record_id='later-overlap').updates(ClaudeSession()):
                await update.observed(bridge)
        assert acknowledgments==[message.reference]
    with closing(DeliveryJournal(state)) as reopened:
        assert DeliveryRow.select(reopened.db)[0].outcome is DeliveredOutcome
        assert MonitorAttemptRow.select(reopened.db)[0].evidence==ObservedMonitor('record-4',1792168805.123)
        assert len(MonitorPartRow.select(reopened.db))==7


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
    record=CLAUDE_RECORD.validate_python(dict(type='user',uuid='record',timestamp='2026-10-16T16:40:05.123Z',
        sessionId='session',promptId='prompt',origin=dict(kind='task-notification'),
        message=dict(content='Another task producer')))
    assert isinstance(record.message.content[0].text,ForeignTaskNotification)
    assert record.updates(ClaudeSession())==()
    from xml.etree.ElementTree import ParseError
    with pytest.raises(ParseError):
        CLAUDE_RECORD.validate_python(dict(type='user',uuid='record',timestamp='2026-10-16T16:40:05.123Z',
            sessionId='session',promptId='prompt',origin=dict(kind='task-notification'),
            message=dict(content='<task-notification>')))
    assert isinstance(ClaudeMonitorFrame.decode_line('ordinary unrelated monitor event'),ForeignMonitorFrame)
    assert isinstance(ClaudeMonitorFrame.decode_line('"Comms in · old format...(truncated)'),ForeignMonitorFrame)


def test_non_task_attachments_and_pending_queue_records_do_not_confirm():
    record=CLAUDE_RECORD.validate_python(dict(type='attachment', uuid='foreign-record',
        timestamp='2026-10-16T16:40:05.123Z', attachment=dict(type='queued_command',
        origin=dict(kind='peer'), prompt='Not a Monitor notification')))
    assert isinstance(record.attachment, ForeignAttachment)
    assert record.updates(ClaudeSession())==()
    queued=CLAUDE_RECORD.validate_python(dict(type='queue-operation', operation='enqueue',
        sessionId='session', content='<task-notification><event>not yet received</event></task-notification>'))
    assert queued.updates(ClaudeSession())==()


@pytest.mark.asyncio
async def test_wrong_complete_parts_stay_pending_without_claiming_delivery(tmp_path,capsys):
    with closing(DeliveryJournal(tmp_path)) as journal:
        binding=Binding(str(tmp_path),'viewer','session')
        bridge=ClaudeBridge(Options(Watch(),None,None,None,tmp_path),binding,journal)
        message=Message(sender='peer',target='viewer',type=InfoKind,body='original',timestamp=1.,seq=1)
        journal.begin(message,json.dumps(message.to_wire()),EmittedOutcome)
        journal.emitting(message,1)
        frame=MonitorPart(id=message.message_id,sender='peer',number=1,total=1,body='different')
        await frame.confirm(bridge,ObservedMonitor('record',1.))
        assert DeliveryRow.select(journal.db)[0].outcome is EmittedOutcome
        assert isinstance(MonitorAttemptRow.select(journal.db)[0].evidence,MismatchedMonitor)
        assert 'message remains pending' in capsys.readouterr().err


def test_old_emission_has_no_invented_evidence_or_replay(tmp_path):
    message=Message(sender='peer',target='viewer',type=InfoKind,body='retained',timestamp=1.,seq=1)
    with closing(DeliveryJournal(tmp_path)) as journal:
        journal.begin(message,json.dumps(message.to_wire()),EmittedOutcome)
        frame=MonitorPart(id=message.message_id,sender='peer',number=1,total=1,body='retained')
        assert journal.monitor_attempt(frame) is NO_MONITOR_ATTEMPT
        assert not journal.begin(message,'do not replace',UnconfirmedOutcome)
        assert DeliveryRow.select(journal.db)[0].outcome is EmittedOutcome
        assert MonitorAttemptRow.select(journal.db)==[]


@pytest.mark.asyncio
async def test_explicit_watch_does_not_mark_streamed_messages_read(wired):
    from contextlib import aclosing
    import asyncio
    first=wired.messaging.send_message('PR111','fixer','first')
    async with aclosing(wired.bus.watch('fixer', acknowledgment=ExplicitWatchAcknowledgment)) as stream:
        assert (await anext(stream)).reference==first.reference
        pending=asyncio.create_task(anext(stream))
        try:
            second=wired.messaging.send_message('PR111','fixer','second')
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
