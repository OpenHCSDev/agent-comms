"""Wire wake admission follows declared file capabilities, not a caller's list."""

import asyncio

import pytest

from agent_comms.locked_store import DurableFile, WireWakeFile
from agent_comms.store_files import store_lock_path
from agent_comms.wire_watch import WatchSource, WireChangeWatch, open_wire_watcher


@pytest.mark.asyncio
async def test_new_declared_owner_wakes_without_a_watcher_inventory(tmp_path):
    class PublishedFile(DurableFile, WireWakeFile):
        filename = "declared-publication"

    published = PublishedFile.path_in(tmp_path)
    source = WatchSource.wire(tmp_path)
    assert published in source.paths()
    assert store_lock_path(published) not in source.paths()
    watcher = open_wire_watcher(source)
    if not isinstance(watcher, WireChangeWatch):
        watcher.close()
        pytest.skip("native file notifications unavailable")
    try:
        await watcher.prepare()
        # Lock-file writes cannot turn an idle reader into its own wake loop.
        store_lock_path(published).write_text("lock churn")
        await asyncio.sleep(0.05)
        assert not watcher.changed.is_set()
        published.write_text("committed publication")
        await asyncio.wait_for(watcher.changed.wait(), 1)
    finally:
        watcher.close()
