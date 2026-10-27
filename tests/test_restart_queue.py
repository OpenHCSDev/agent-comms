"""Queued owner handoff wakes for registry and allocated queue entries."""

import asyncio
from contextlib import aclosing
from dataclasses import replace


import agent_comms.restart_queue as queue
from agent_comms.repo import Checkout
from agent_comms.threads import Thread
from agent_comms.bus import Bus
from agent_comms.field_codec import FieldCodec, RestartId
from agent_comms.watched import FileSource
from restart_fixture import dead_owner_restart, original_restart_json

def test_dead_owner_restart_settles_before_selection_checks(private_comms, dead_owner_restart):
    store = queue.RestartQueue(private_comms.root)
    owner = dead_owner_restart.selection.identity.incarnation.require_recorded()
    with store.writing() as db:
        dead_owner_restart.insert(db)
    store.step(private_comms)
    with store.snapshot() as db:
        assert not store._active(db)
    assert store.history(owner) == [replace(
        dead_owner_restart, state=queue.StaleRestart(reason='Owner process exited'),
    )]


def test_cancel_settles_dead_pending_owner_without_a_watcher(
    private_comms, dead_owner_restart,
):
    store = queue.RestartQueue(private_comms.root)
    owner = dead_owner_restart.selection.identity.incarnation.require_recorded()
    with store.writing() as db:
        dead_owner_restart.insert(db)
    store.cancel(owner)
    with store.snapshot() as db:
        assert not store._active(db)
    assert store.history(owner) == [replace(
        dead_owner_restart, state=queue.StaleRestart(reason='Owner process exited'),
    )]


async def test_restore_owner_keeps_blocked_goal_unchanged(
    private_comms, tmp_path, source_programs, native_runtime_package,
):
    from agent_comms.cli_verbs.restore_owner import RestoreOwnerCliCommand
    from agent_comms.cli_verbs.target_action import TargetActionCliCommand
    from agent_comms.cli_command import TargetBatchResult
    from agent_comms.goal_actions import SetGoalAction, BlockedGoalAction
    from agent_comms.runtime import socket_path

    source_programs.bind(tmp_path)
    comms = private_comms
    comms.registry.declare(Thread('worker', frozenset(), Checkout(tmp_path)))
    comms.goals.update_goal('worker', SetGoalAction(text='Preserve this blocked task'))
    blocked = comms.goals.update_goal(
        'worker', BlockedGoalAction(block_reason='Await an explicit owner retry'))
    command = TargetActionCliCommand(
        operation=RestoreOwnerCliCommand.declared_name, target=('worker',), project=Checkout(tmp_path))
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
        assert repeated.reconnect_targets() == ('worker',)
        assert comms.registry.require('worker').require_process().pid == pid
        assert comms.registry.require('worker').goal == blocked
    finally:
        await asyncio.to_thread(comms.owners.stop, 'worker')


def test_attempted_and_terminal_records_never_return_to_pending(
    private_comms, dead_owner_restart,
):
    store = queue.RestartQueue(private_comms.root)
    owner = dead_owner_restart.owner
    attempted = replace(dead_owner_restart, state=queue.AttemptingRestart())
    terminal = replace(dead_owner_restart, id=RestartId.mint(),
                       state=queue.UncertainRestart(reason='ambiguous owner restart'))
    with store.writing() as db:
        attempted.insert(db)
        terminal.insert(db)
    store.step(private_comms)
    assert store.cancel(owner) == []
    assert store.history(owner) == [attempted, terminal]
    with store.snapshot() as db:
        assert store._active(db) == [attempted]


async def test_inotify_wakes_for_registry_and_queue_changes(private_comms, dead_owner_restart):
    comms = private_comms
    store = queue.RestartQueue(comms.root)
    sources = (FileSource(comms.registry.store.path),
               *(FileSource(path) for path in type(store).source_paths(comms.root)))
    async with aclosing(Bus.watch_sources(*sources)) as observations:
        await anext(observations)
        comms.registry.declare(Thread('wake', frozenset(), Checkout(comms.root)))
        await asyncio.wait_for(anext(observations), 1)
        with store.writing() as db:
            dead_owner_restart.insert(db)
        await asyncio.wait_for(anext(observations), 1)
        assert store.history(dead_owner_restart.owner) == [dead_owner_restart]


def test_carry_keeps_every_original_and_never_requeues_an_attempt(
    private_comms, original_restart_json, tmp_path,
):
    from cutover.carry_restart_queue import RestartQueueRows
    from agent_comms.locked_store import LiveFilesRequest
    from conftest import maintenance_held

    comms = private_comms
    directory = comms.root / queue.DIRECTORY
    paths = tuple(original_restart_json)
    pending, attempted, terminal, interrupted = original_restart_json.values()
    originals = tuple(path.read_bytes() for path in paths)
    carry = RestartQueueRows(LiveFilesRequest(comms.root), tmp_path / 'spill')
    assert carry.carry(apply=False).changed == 4
    assert not (directory / 'queue.sqlite3').exists()
    with maintenance_held(comms.root):
        assert carry.carry(apply=True).changed == 4
        assert tuple(path.read_bytes() for path in paths) == originals
        # A repeated carry verifies exact rows instead of minting or reclassifying them.
        assert carry.carry(apply=True).changed == 4
        store = queue.RestartQueue(comms.root)
        assert store.history(pending.owner) == [pending, terminal]
        assert store.history(attempted.owner) == [attempted]
        assert store.history(interrupted.owner) == [interrupted]
        assert carry.after_acceptance().changed == 4
        assert not any(path.exists() for path in paths)
        assert store.history(attempted.owner) == [attempted]
    # The original pending process is dead. The watcher can only settle it;
    # the attempted record has no new execution permission.
    store.step(comms)
    assert store.history(attempted.owner) == [attempted]


def test_carry_refuses_to_activate_an_intent_from_settled_history(
        private_comms, original_restart_json, tmp_path):
    import pytest
    from cutover.carry_restart_queue import RestartQueueRows
    from agent_comms.errors import RelationViolationError
    from agent_comms.locked_store import LiveFilesRequest

    path, pending = next(iter(original_restart_json.items()))
    destination = path.parents[1] / "settled" / str(pending.owner.lookup) / f"{pending.id}.json"
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.rename(destination)
    original = destination.read_bytes()
    carry = RestartQueueRows(LiveFilesRequest(private_comms.root), tmp_path / 'spill')
    with pytest.raises(RelationViolationError, match='settled restart'):
        carry.carry(apply=False)
    assert destination.read_bytes() == original
    assert not queue.RestartQueue(private_comms.root).path.exists()
