"""Runnable inbox work is distinct from parked, unwritten input."""



import pytest

from agent_comms.agent_backend import (
    ClearQueue, FollowUpInputPolicy, InputContent, InputRequest,
    SendNow, TurnInbox, TurnInboxItem, UseSettings,
)
from agent_comms.native_session_prepare import NativeSessionPreparation
from agent_comms.pi_vocabulary import OffThinkingLevel
from agent_comms.thread_model import UndeclaredModel
from agent_comms.turn_failure import QueuedInputMissing, QueuedInputNotSent
from native_response_fixture import native_response_session
from agent_comms.field_codec import InputId
from agent_comms.turn_inputs import InputForwarding


def test_inbox_owns_ready_work_and_retains_parked_input():
    inbox = TurnInbox()
    inputs = InputForwarding(inbox)
    item = InputRequest(input_id=InputId.mint(), content=InputContent(text="later"),
                        when_busy=FollowUpInputPolicy)
    assert not inbox.has_ready_work and inputs.settled
    inbox.put_nowait(item)
    assert inbox.has_ready_work and inputs.unresolved
    assert inbox.get_nowait() is item
    inbox.hold(item)
    assert inbox.qsize() == 1 and not inbox.has_ready_work and inputs.settled
    inbox.ready.set()
    assert inbox.has_ready_work and inputs.unresolved
    assert inbox.get_nowait() is item
    assert not inbox.has_ready_work and inputs.settled


@pytest.mark.parametrize("control", (
    ClearQueue((InputId.mint(),)),
    SendNow((InputId.mint(),)),
    UseSettings(model=UndeclaredModel(), thinking_level=OffThinkingLevel),
))
def test_returned_controls_are_not_undelivered_user_inputs(
    control: TurnInboxItem, native_response_session: NativeSessionPreparation,
):
    session = native_response_session
    session.inputs.queue.begin(control)
    assert session.inputs.queue.has_ready_work
    assert not session.inputs.unwritten_inputs and session.inputs.settled
    assert not QueuedInputMissing.detected(session, transport_ok=True)
    assert not QueuedInputNotSent.detected(session, transport_ok=True)
    session.inputs.current = session.inputs.queue.get_nowait()
    assert not session.inputs.unwritten_inputs and session.inputs.settled


def test_certainly_unwritten_input_does_not_claim_uncertain_delivery(
    native_response_session: NativeSessionPreparation,
):
    session = native_response_session
    item = InputRequest(input_id=InputId.mint(), content=InputContent(text="not sent"),
                        when_busy=FollowUpInputPolicy)
    session.inputs.queue.begin(item)
    assert session.inputs.unwritten_inputs == (item.input_id,)
    assert QueuedInputNotSent.detected(session, transport_ok=True)
    assert not QueuedInputMissing.detected(session, transport_ok=True)


