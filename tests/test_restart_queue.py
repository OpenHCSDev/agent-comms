"""Queued owner handoff wakes for registry and allocated queue entries."""

import asyncio
import json
import sys
from contextlib import aclosing

import agent_comms.restart_queue as queue
from agent_comms.bus import Bus
from agent_comms.child_process import ProcessIdentity
from agent_comms.field_codec import FieldCodec, RestartId
from agent_comms.launch_runtime import RestartEnvironment
from agent_comms.owner_lifecycle import OwnerRestartSelection
from agent_comms.threads import Thread
from agent_comms.watched import DirectorySource, FileSource


def test_history_keeps_interrupted_terminal_visible_until_step_settles_it(private_comms):
    comms = private_comms
    comms.registry.declare(Thread('worker', frozenset(), str(comms.root)))
    snapshot = comms.registry.snapshot()
    owner = snapshot.require('worker').incarnation.require_recorded()
    selection = OwnerRestartSelection(
        snapshot.owner_identity('worker'), ProcessIdentity(2147483647, 1),
        snapshot.claim_generation('worker'), sys.executable,
    )
    record = queue.QueuedRestart(
        RestartId.mint(), selection,
        queue.RestartTarget(sys.executable, RestartEnvironment(), sys.executable),
        queue.UncertainRestart(reason='interrupted terminal move'),
    )
    store = queue.RestartQueue(comms.root / queue.DIRECTORY)
    active = store.active_path(owner)
    with store.guarded_write():
        active.write_text(json.dumps(FieldCodec.encode(record)))
    original = active.read_bytes()
    with store.guarded_read():
        assert list(store.history(owner)) == [record]
        assert not tuple(store.active_records())
        assert active.read_bytes() == original
    queue.step(comms)
    assert not active.exists()
    with store.guarded_read():
        assert list(store.history(owner)) == [record]
        assert store.settled_path(record).read_bytes() == original


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
