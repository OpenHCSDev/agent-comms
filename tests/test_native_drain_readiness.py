"""A failed request warning cannot decide availability of the recipient inbox."""
from dataclasses import replace

import pytest

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
    with pytest.raises(RelationViolationError):
        ready.require_available()
    assert ready.presentation(
        Activity('receiver', ActivityState.IDLE).ordinary_presentation('receiver')
    ).status.details == (ready.diagnostic.summary,)


