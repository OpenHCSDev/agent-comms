"""A failed request warning cannot decide availability of the recipient inbox."""
from dataclasses import replace

import pytest

from agent_comms.activity import (
    Activity, ActivityState, DrainReadiness, NativeInputDrainDiagnostic,
    NativeInputDrainReadiness, ObservedActivity, ReadyDrainReadiness,
    StoppedDrainDiagnostic, UnavailableDrainDiagnostic, UnavailableDrainReadiness,
)
from agent_comms.audience_manifest import FrozenRecipient
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.presentation import MessageNotification
from agent_comms.thread_identity import OwnerIdentity, ThreadIncarnation


def test_native_warning_keeps_fresh_notifications_and_active_paint_available():
    owner = OwnerIdentity(ThreadIncarnation('receiver', 1.0), 2)
    diagnostic = NativeInputDrainDiagnostic(owner, 'NativePiTerminalFailure', 'known provider failure')
    event = Activity('receiver', ActivityState.IDLE, diagnostic=diagnostic)
    acquired = ObservedActivity.acquire(event, owner)
    ready = acquired.readiness
    assert isinstance(ready, (NativeInputDrainReadiness, ReadyDrainReadiness))
    ready.require_available()
    notice = MessageNotification(FrozenRecipient('recipient', 'receiver'), 'Pending', 'fresh message')
    assert ready.pending_notification(notice) is notice
    assert acquired.source_event().diagnostic is diagnostic
    assert FieldCodec.decode(DrainReadiness, FieldCodec.encode(ready)) == ready
    busy = replace(acquired, state=ActivityState.THINKING, detail='Handling fresh input')
    assert busy.presentation('receiver').summary == 'Thinking · Handling fresh input'
    assert not busy.presentation('receiver').attention
    idle = acquired.presentation('receiver')
    assert idle.attention and 'known provider failure' in idle.summary
    assert 'Inbox unavailable' not in idle.summary
    successor = replace(owner, generation=3)
    assert ObservedActivity.acquire(event, successor).readiness.source_diagnostic() is None


@pytest.mark.parametrize('kind', [UnavailableDrainDiagnostic, StoppedDrainDiagnostic])
def test_real_inbox_failures_still_refuse_and_hold_saved_notifications(kind):
    owner = OwnerIdentity(ThreadIncarnation('receiver', 1.0), 2)
    ready = DrainReadiness.acquire(kind(owner, 'OSError', 'real read failure'), owner)
    assert isinstance(ready, UnavailableDrainReadiness)
    with pytest.raises(RelationViolationError, match='real read failure'):
        ready.require_available()
    notice = MessageNotification(FrozenRecipient('recipient', 'receiver'), 'Pending', 'fresh message')
    assert ready.pending_notification(notice).state == 'Waiting for recovery'
    assert ready.presentation(Activity('receiver', ActivityState.IDLE).presentation('receiver'), busy=False).attention


@pytest.mark.asyncio
async def test_real_background_native_failure_then_distinct_fresh_input(native_backend):
    """Only provider replies are controlled; the actual background drain owns the warning."""
    import asyncio

    native = native_backend
    await native.author_history()
    async with native.open_owner(runtime_enabled=True, auto_wake=True) as (agent, session_id):
        comms = agent._comms
        comms.threads.claim_thread('sender', tags=frozenset(), worktree=str(native.project))
        native.provider.status = 503
        failed = comms.messaging.send_message('sender', session_id, 'isolated failed input')
        async with asyncio.timeout(25):
            while not isinstance(comms.agents.activity_of(session_id).readiness, NativeInputDrainReadiness):
                await asyncio.sleep(0.02)
        assert native.provider.posts == 1
        original_warning = comms.agents.activity_of(session_id).readiness.source_diagnostic()
        assert original_warning.error_type == 'NativePiTerminalFailure'
        task = agent.inputs.drain_tasks[session_id]
        assert not task.done()
        native.provider.status = 200
        fresh = comms.messaging.send_message('sender', session_id, 'isolated fresh input')
        native.provider.text = 'Distinct fresh native reply.'
        async with asyncio.timeout(25):
            while not any(message.body == 'Distinct fresh native reply.'
                          for message in comms.bus.inbox('sender')):
                await asyncio.sleep(0.02)
        assert native.provider.posts == 2
        saved = [message['content'] for message in native.saved_inputs()]
        assert sum('isolated failed input' in str(content) for content in saved) == 1
        assert sum('isolated fresh input' in str(content) for content in saved) == 1
        assert comms.bus.log.message_by_id(failed.message_id) == failed
        assert not task.done()
        comms.agents.activity_of(session_id).readiness.require_available()


from test_backend_native_lifecycle import native_backend as native_backend
