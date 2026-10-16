"""Session-owned observations, not stdout writes, confirm monitor delivery."""
from contextlib import closing
from html import escape
import json

import pytest

from agent_comms.cli_bridges.claude import ClaudeBridge
from agent_comms.cli_bridges.claude_protocol import CLAUDE_RECORD, ClaudeSession
from agent_comms.cli_bridges.shared import (
    Binding, DeliveryJournal, DeliveryRow, MonitorAttemptRow, MonitorMessage,
    Options, Outcome, ObservedMonitor, Watch, monitor_notifications,
)
from agent_comms.messages import InfoKind, Message
from agent_comms.read_basis import ExplicitWatchAcknowledgment


def notification(frame, session='session'):
    return CLAUDE_RECORD.validate_python(dict(
        type='user', uuid='notification-record', timestamp='2026-10-16T16:40:05.123Z',
        sessionId=session, promptId='prompt', origin=dict(kind='task-notification'),
        message=dict(content='<task-notification><event>'+escape(frame.model_dump_json())+
                     '</event></task-notification>')))


@pytest.mark.asyncio
async def test_monitor_requires_matching_session_token_and_complete_body_then_reopens(tmp_path, capsys):
    state=tmp_path/'state'
    with closing(DeliveryJournal(state)) as journal:
        binding=Binding(str(tmp_path),'viewer','session')
        journal.bind(binding)
        bridge=ClaudeBridge(Options(Watch(),None,None,None,state),binding,journal)
        acknowledgments=[]
        class Core:
            async def acknowledge(self,message):
                acknowledgments.append(message.reference)
        bridge.core=Core()
        message=Message(sender='peer',target='viewer',type=InfoKind,body='full body',timestamp=1.,seq=1)
        assert journal.begin(message,json.dumps(message.to_wire()),Outcome.UNCONFIRMED)
        assert await bridge.deliver(message) is Outcome.EMITTED
        journal.finish(message,Outcome.EMITTED)
        frame=MonitorMessage.model_validate_json(capsys.readouterr().out)
        for update in notification(frame,'different-session').updates(ClaudeSession()):
            await update.observed(bridge)
        assert not acknowledgments
        for update in notification(frame.model_copy(update={'token':'wrong'})).updates(ClaudeSession()):
            await update.observed(bridge)
        assert not acknowledgments
        with pytest.raises(ValueError,match='original message'):
            for update in notification(frame.model_copy(update={'text':'partial'})).updates(ClaudeSession()):
                await update.observed(bridge)
        assert not acknowledgments
        for update in notification(frame).updates(ClaudeSession()):
            await update.observed(bridge)
        assert acknowledgments==[message.reference]
        for update in notification(frame).updates(ClaudeSession()):
            await update.observed(bridge)
        assert acknowledgments==[message.reference]  # History overlap is not another delivery.
    with closing(DeliveryJournal(state)) as reopened:
        assert DeliveryRow.select(reopened.db)[0].outcome is Outcome.DELIVERED
        assert MonitorAttemptRow.select(reopened.db)[0].evidence==ObservedMonitor('notification-record',1792168805.123)


def test_truncated_monitor_notification_cannot_confirm_delivery(capsys):
    frame=MonitorMessage(session='session',token='token',id='id',text='complete body')
    assert not monitor_notifications(frame.model_dump_json()[:-5]+'...(truncated)', 'session','record',1.)
    assert 'delivery remains pending' in capsys.readouterr().err
    assert not monitor_notifications('ordinary unrelated monitor event', 'session','record',1.)


def test_old_emission_has_no_invented_evidence_or_replay(tmp_path):
    message=Message(sender='peer',target='viewer',type=InfoKind,body='retained',timestamp=1.,seq=1)
    with closing(DeliveryJournal(tmp_path)) as journal:
        journal.begin(message,json.dumps(message.to_wire()),Outcome.EMITTED)
        frame=MonitorMessage(session='session',token='invented',id=message.message_id,text='retained')
        assert journal.confirm_monitor(frame) is None
        assert not journal.begin(message,'do not replace',Outcome.UNCONFIRMED)
        assert DeliveryRow.select(journal.db)[0].outcome is Outcome.EMITTED
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
                taken = await pending
                while taken.reference == first.reference:
                    taken = await anext(stream)
            assert taken.reference == second.reference
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending,return_exceptions=True)
    assert {m.reference for m in wired.bus.inbox('fixer')}=={first.reference,second.reference}
    assert wired.messaging.acknowledge('fixer',(first.reference,))==1
    assert [m.reference for m in wired.bus.inbox('fixer')]==[second.reference]
