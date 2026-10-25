"""Queued owner handoff wakes for registry and allocated queue entries."""

import asyncio
import json
import subprocess
import sys
from contextlib import aclosing
from dataclasses import replace

import pytest

import agent_comms.restart_queue as queue
from agent_comms.bus import Bus
from agent_comms.child_process import ProcessIdentity
from agent_comms.field_codec import FieldCodec, RestartId
from agent_comms.launch_runtime import RestartEnvironment
from agent_comms.owner_lifecycle import OwnerRestartSelection
from agent_comms.threads import Thread
from agent_comms.watched import DirectorySource, FileSource


@pytest.fixture
def dead_owner_restart(private_comms):
    comms = private_comms
    comms.registry.declare(Thread('worker', frozenset(), str(comms.root)))
    snapshot = comms.registry.snapshot()
    with subprocess.Popen(
        (sys.executable, '-c', 'import sys; sys.stdin.buffer.read(1)'), stdin=subprocess.PIPE,
    ) as process:
        identity = ProcessIdentity.capture(process.pid)
        process.communicate(b'x', timeout=10)
        assert process.returncode == 0
    selection = OwnerRestartSelection(
        snapshot.owner_identity('worker'), identity,
        snapshot.claim_generation('worker'), sys.executable,
    )
    return queue.QueuedRestart(
        RestartId.mint(), selection,
        queue.RestartTarget(sys.executable, RestartEnvironment(), sys.executable),
        queue.PendingRestart(),
    )


def test_dead_owner_restart_settles_before_selection_checks(private_comms, dead_owner_restart):
    store = queue.RestartQueue(private_comms.root / queue.DIRECTORY)
    owner = dead_owner_restart.selection.identity.incarnation.require_recorded()
    with store.guarded_write():
        store.save(dead_owner_restart)
    queue.step(private_comms)
    with store.guarded_read():
        assert not tuple(store.active_records())
        assert not store.active_path(owner).exists()
        assert list(store.history(owner)) == [replace(
            dead_owner_restart, state=queue.StaleRestart(reason='Owner process exited'),
        )]


def test_history_keeps_interrupted_terminal_visible_until_step_settles_it(
    private_comms, dead_owner_restart,
):
    comms = private_comms
    owner = dead_owner_restart.selection.identity.incarnation.require_recorded()
    record = replace(dead_owner_restart,
                     state=queue.UncertainRestart(reason='interrupted terminal move'))
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
