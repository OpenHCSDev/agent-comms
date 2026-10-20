"""A failed request warning cannot decide availability of the recipient inbox."""
from dataclasses import replace
import asyncio

import pytest
from conftest import await_event_or_task

from agent_comms.activity import (
    Activity, ActivityState, DrainReadiness, NativeInputDrainDiagnostic,
    NativeInputDrainReadiness, ObservedActivity, ReadyDrainReadiness,
    StoppedDrainDiagnostic, UnavailableDrainDiagnostic, UnavailableDrainReadiness,
)
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.thread_identity import OwnerIdentity
from agent_comms.thread_identity import RecordedThreadIncarnation


def test_native_warning_keeps_fresh_notifications_and_active_paint_available():
    owner = OwnerIdentity(RecordedThreadIncarnation('receiver', 1.0), 2)
    diagnostic = NativeInputDrainDiagnostic(
        owner, 'NativePiTerminalFailure', 'known provider failure', recorded_at=1.0
    )
    event = Activity('receiver', ActivityState.IDLE, diagnostic=diagnostic)
    acquired = ObservedActivity.acquire(event, owner)
    ready = acquired.readiness
    assert isinstance(ready, (NativeInputDrainReadiness, ReadyDrainReadiness))
    ready.require_available()
    assert acquired.source_event().diagnostic is diagnostic
    assert FieldCodec.decode(DrainReadiness, FieldCodec.encode(ready)) == ready
    busy = replace(acquired, state=ActivityState.THINKING, detail='Handling fresh input')
    assert busy.presentation('receiver').summary == 'Thinking · Handling fresh input'
    assert busy.presentation('receiver').status.details == (diagnostic.summary,)
    idle = acquired.presentation('receiver')
    assert idle.status.details == (diagnostic.summary,)
    assert 'known provider failure' in idle.summary
    assert 'Inbox unavailable' not in idle.summary
    successor = replace(owner, generation=3)
    assert ObservedActivity.acquire(event, successor).readiness.source_diagnostic() is None


@pytest.mark.parametrize('kind', [UnavailableDrainDiagnostic, StoppedDrainDiagnostic])
def test_real_inbox_failures_refuse_without_owning_message_labels(kind):
    owner = OwnerIdentity(RecordedThreadIncarnation('receiver', 1.0), 2)
    ready = DrainReadiness.acquire(
        kind(owner, 'OSError', 'real read failure', recorded_at=1.0), owner
    )
    assert isinstance(ready, UnavailableDrainReadiness)
    with pytest.raises(RelationViolationError, match='real read failure'):
        ready.require_available()
    assert ready.presentation(
        Activity('receiver', ActivityState.IDLE).ordinary_presentation('receiver')
    ).status.details == (ready.diagnostic.summary,)


@pytest.mark.asyncio
async def test_real_saved_native_failure_then_distinct_fresh_input(native_backend):
    """Real saved ACP/native process; only localhost provider responses are controlled."""
    from acp import RequestError
    from acp.schema import TextContentBlock
    from agent_comms.activity import NativeInputDrainDiagnostic

    native = native_backend
    await native.author_history()
    async with native.open_owner() as (agent, session_id):
        native.provider.status = 503
        requested = asyncio.Event()

        class RequestObserved:
            async def wait(self):
                requested.set()
                return True

        native.provider.response_gate = RequestObserved()
        failed = asyncio.create_task(agent.prompt(
            session_id, [TextContentBlock(type='text', text='isolated failed input')]))
        try:
            with pytest.raises(RequestError):
                await await_event_or_task(requested.wait(), failed)
                await failed
        finally:
            native.provider.response_gate = None
        failed_posts = native.provider.posts
        assert failed_posts == 4  # One request plus Pi's three declared provider retries.
        comms = agent._comms
        owner = comms.registry.snapshot().owner_identity(session_id)
        # The warning is the original observer's retained diagnostic. Its presence
        # must not alter the actual saved ACP/native entrypoint for a later input.
        comms.agents.set_drain_diagnostic(session_id, owner,
            NativeInputDrainDiagnostic(
                owner, 'NativePiTerminalFailure', 'controlled HTTP 503', recorded_at=1.0
            ))
        native.provider.status = 200
        native.provider.text = 'Distinct fresh native reply.'
        fresh = await agent.prompt(session_id, [TextContentBlock(type='text', text='isolated fresh input')])
        assert native.provider.posts == failed_posts + 1
        saved = [message['content'] for message in native.saved_inputs()]
        assert sum('isolated failed input' in str(content) for content in saved) == 1
        assert sum('isolated fresh input' in str(content) for content in saved) == 1
        assert fresh.stop_reason == 'end_turn'
        assert native.provider.text in native.session.read_text()
        comms.agents.activity_of(session_id, snapshot=comms.registry.snapshot()).readiness.require_available()
