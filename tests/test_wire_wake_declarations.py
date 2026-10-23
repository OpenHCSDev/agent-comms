"""Wire wake admission follows declared file capabilities, not a caller's list."""

import asyncio
from contextlib import aclosing

from agent_comms.bus import Bus
from agent_comms.locked_store import DurableFile, WireWakeFile
from agent_comms.store_files import store_lock_path


async def test_new_declared_owner_wakes_without_a_watcher_inventory(tmp_path):
    class PublishedFile(DurableFile, WireWakeFile):
        filename = "declared-publication"

    published = PublishedFile.path_in(tmp_path)
    bus = Bus.at(tmp_path)
    assert published in WireWakeFile.watched_paths_in(tmp_path)
    assert store_lock_path(published) not in WireWakeFile.watched_paths_in(tmp_path)
    async with aclosing(bus.watch()) as observations:
        await anext(observations)
        waiting = asyncio.create_task(anext(observations))
        try:
            store_lock_path(published).write_text("lock churn")
            await asyncio.sleep(0.05)
            assert not waiting.done()
            published.write_text("committed publication")
            await asyncio.wait_for(waiting, 1)
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
