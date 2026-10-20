"""Actual queued owner handoff retains history and accepts a fresh native input."""

import asyncio
import sys

import pytest

import agent_comms.restart_queue as queue
from agent_comms.wire_watch import DirectoryWatchSource, WatchSource, open_wire_watcher


@pytest.mark.skipif(sys.platform != "linux", reason="Linux watcher")
async def test_inotify_wakes_for_registry_and_queue_changes(tmp_path):
    root = tmp_path / "wire"
    root.mkdir()
    directory = root / queue.DIRECTORY
    directory.mkdir()
    watcher = open_wire_watcher(
        WatchSource.file(root / "registry.json"), DirectoryWatchSource(directory)
    )
    try:
        await watcher.prepare()
        (root / "registry.json").write_text("changed")
        await asyncio.wait_for(watcher.wait(), 1)
        await watcher.prepare()
        (directory / "next.json").write_text("queued")
        await asyncio.wait_for(watcher.wait(), 1)
    finally:
        watcher.close()


