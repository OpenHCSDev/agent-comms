"""The real message bus and wire watcher preserve unread delivery custody."""

import asyncio
from contextlib import aclosing
from agent_comms.read_basis import OnReadWatchAcknowledgment


async def test_watch_emits_startup_unread_then_arrival_and_leaves_untaken_unread(wired):
    first = wired.messaging.send_message("PR111", "fixer", "already unread")

    async with aclosing(wired.bus.watch("fixer", acknowledgment=OnReadWatchAcknowledgment)) as watcher:
        initial = await asyncio.wait_for(anext(watcher), timeout=3)
        assert initial.message_id == first.message_id

        # The consumer awaits one arrival, without polling the inbox or wire.
        arrival = asyncio.create_task(anext(watcher))
        try:
            second = wired.messaging.send_message("PR111", "fixer", "new arrival")
            emitted = await asyncio.wait_for(arrival, timeout=3)
            assert emitted.message_id == second.message_id
        finally:
            if not arrival.done():
                arrival.cancel()
                await asyncio.gather(arrival, return_exceptions=True)

        third = wired.messaging.send_message("PR111", "fixer", "not taken")
        # Closing here never advances the iterator to the third message.

    unread = {message.message_id for message in wired.bus.inbox("fixer")}
    assert first.message_id not in unread
    assert third.message_id in unread
