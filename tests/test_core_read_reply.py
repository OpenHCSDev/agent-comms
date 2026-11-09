"""A reply the waiter cannot decode ends its wait instead of stranding the thread."""

import asyncio

import pytest

from agent_comms.ui_model.observation import CoreReadAnswered, CoreReadOperation, PendingRead
from agent_comms.transport.tools.sidebar import CommsChannelTool


class OnePendingRead:
    """Only what the reply touches: the map of reads in flight."""

    def __init__(self, pending):
        self.reads = pending


async def test_a_reply_that_cannot_be_decoded_settles_its_waiter() -> None:
    waiter = asyncio.get_running_loop().create_future()
    asked = CoreReadOperation("/nowhere", CommsChannelTool(name="#comms"), request_id=7)
    process = OnePendingRead({7: PendingRead(asked, waiter)})

    with pytest.raises(Exception):
        await CoreReadAnswered(7, {"not": "a channel"}).received(process, lambda _: None)

    assert waiter.done(), "the waiting thread was left holding a future nobody owns"
    with pytest.raises(Exception):
        waiter.result()
    assert 7 not in process.reads, "a settled read stayed in the map"
