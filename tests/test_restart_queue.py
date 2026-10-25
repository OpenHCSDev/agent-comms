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


def test_cancel_settles_dead_pending_owner_without_a_watcher(
    private_comms, dead_owner_restart,
):
    store = queue.RestartQueue(private_comms.root / queue.DIRECTORY)
    owner = dead_owner_restart.selection.identity.incarnation.require_recorded()
    with store.guarded_write():
        store.save(dead_owner_restart)
    queue.cancel(private_comms, 'worker')
    with store.guarded_read():
        assert not store.active_path(owner).exists()
        assert list(store.history(owner)) == [replace(
            dead_owner_restart, state=queue.StaleRestart(reason='Owner process exited'),
        )]


async def test_restore_owner_keeps_blocked_goal_unchanged(
    private_comms, tmp_path, source_programs, native_runtime_package,
):
    from agent_comms.cli_commands import RestoreOwnerCliCommand, TargetActionCliCommand, TargetBatchResult
    from agent_comms.goal_actions import SetGoalAction, BlockedGoalAction
    from agent_comms.runtime import socket_path

    source_programs.bind(tmp_path)
    comms = private_comms
    comms.registry.declare(Thread('worker', frozenset(), str(tmp_path)))
    comms.goals.update_goal('worker', SetGoalAction(text='Preserve this blocked task'))
    blocked = comms.goals.update_goal(
        'worker', BlockedGoalAction(block_reason='Await an explicit owner retry'))
    command = TargetActionCliCommand(
        operation=RestoreOwnerCliCommand.declared_name, target=('worker',))
    result = await asyncio.to_thread(command.apply, comms)
    decoded = FieldCodec.decode(TargetBatchResult, FieldCodec.encode(result))
    assert decoded.exit_code() == 0
    assert decoded.reconnect_targets() == ('worker',)
    pid = comms.registry.require('worker').require_process().pid
    try:
        async with asyncio.timeout(20):
            while not socket_path(comms.root, pid).exists():
                assert comms.registry.require('worker').process_alive
                await asyncio.sleep(0.02)
        assert comms.registry.require('worker').goal == blocked
        repeated = await asyncio.to_thread(command.apply, comms)
        assert repeated.exit_code() == 0
        assert not repeated.reconnect_targets()
        assert comms.registry.require('worker').require_process().pid == pid
        assert comms.registry.require('worker').goal == blocked
    finally:
        await asyncio.to_thread(comms.owners.stop, 'worker')


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
