"""Queued owner handoff wakes for registry and allocated queue entries."""

import asyncio
from contextlib import aclosing

import agent_comms.restart_queue as queue
from agent_comms.bus import Bus
from agent_comms.watched import DirectorySource, FileSource


async def test_inotify_wakes_for_registry_and_queue_changes(tmp_path):
    root = tmp_path / "wire"
    root.mkdir()
    directory = root / queue.DIRECTORY
    directory.mkdir()
    async with aclosing(Bus.watch_sources(
        FileSource(root / "registry.json"), DirectorySource(directory),
    )) as observations:
        await anext(observations)
        (root / "registry.json").write_text("changed")
        await asyncio.wait_for(anext(observations), 1)
        (directory / "next.json").write_text("queued")
        await asyncio.wait_for(anext(observations), 1)
