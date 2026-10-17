"""Runnable inbox work is distinct from parked, unwritten input."""

import asyncio
from types import SimpleNamespace

import pytest

from agent_comms.agent_backend import InputContent, InputId, InputRequest, TurnInbox, WhenBusy
from agent_comms.turn_inputs import InputForwarding
from agent_comms.turn_stats import StatsRequest


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


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [BrokenPipeError, ConnectionResetError])
async def test_stats_dead_pipe_is_not_a_settled_turn(failure):
    class DeadPipe:
        def write(self, data):
            raise failure("native pipe closed")

    loop = asyncio.get_running_loop()
    session = SimpleNamespace(
        inputs=InputForwarding(), settlement_count=1, persistent_session=object(),
        native=SimpleNamespace(proc=SimpleNamespace(stdin=DeadPipe()), reader=SimpleNamespace(
            track=lambda command: loop.create_future(), command_bytes=lambda command: b"query\n")),
    )
    with pytest.raises(failure, match="native pipe closed"):
        await StatsRequest().request(session)
