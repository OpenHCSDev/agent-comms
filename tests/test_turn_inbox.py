"""Runnable inbox work is distinct from parked, unwritten input."""



from agent_comms.agent_backend import FollowUpInputPolicy, InputContent, InputRequest, TurnInbox
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


