"""Runnable inbox work is distinct from parked, unwritten input."""



from agent_comms.agent_backend import InputContent, InputId, InputRequest, TurnInbox, WhenBusy
from agent_comms.turn_inputs import InputForwarding


def test_inbox_owns_ready_work_and_retains_parked_input():
    inbox = TurnInbox()
    inputs = InputForwarding(inbox)
    item = InputRequest(input_id=InputId("parked"), content=InputContent(text="later"),
                        when_busy=WhenBusy.FOLLOW_UP)
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


