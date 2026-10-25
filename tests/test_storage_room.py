"""Measured admission, not estimates; the claim also owns displayed waiting."""

import asyncio
import os
import shutil

import pytest

from agent_comms.activity import ActivityState
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.storage_room import (
    BuildRoomWork, RoomClaim, TestGateRoomWork, UnitStartRoomWork,
    WaitingRoomPhase,
)
from agent_comms.storage_rules import StorageRule
from agent_comms.shell_effects import shell_storage_effects
from agent_comms.thread_status import RunningThreadStatus
from agent_comms.threads import Thread
from agent_comms.field_codec import RoomTicket


def copied_root(tmp_path):
    original = wire(tmp_path / 'original')
    for name in ('gate-one', 'gate-two'):
        original.registry.declare(Thread(name, frozenset(), str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid())), RunningThreadStatus())
    root = tmp_path / 'copied'
    shutil.copytree(original.root, root)
    comms = wire(root)
    comms.registry.storage_rule = StorageRule(volume=tmp_path, floor_gib=0, memory_gib=0,
        checkout_base=tmp_path, scratch_base=tmp_path / 'scratch')
    return comms


@pytest.mark.asyncio
async def test_cancelled_waiter_releases_only_its_own_claim(tmp_path):
    comms = copied_root(tmp_path)
    rule = comms.registry.storage_rule
    one, two = (comms.registry.require(name) for name in ('gate-one', 'gate-two'))
    first = await rule.wait_for_room((BuildRoomWork,), one, comms.root)
    # Every unmeasured kind calibrates alone while the first holds observed room.
    pending = asyncio.create_task(rule.wait_for_room((UnitStartRoomWork,), two, comms.root))
    await asyncio.sleep(.1)
    assert not pending.done()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    with rule.room_ledger.snapshot() as db:
        assert RoomClaim.count(db) == 1
    await asyncio.to_thread(first.finish)
    with rule.room_ledger.snapshot() as db:
        assert RoomClaim.count(db) == 0


@pytest.mark.asyncio
async def test_committed_release_wakes_a_waiter_without_os_refresh(tmp_path):
    comms = copied_root(tmp_path)
    rule = comms.registry.storage_rule
    one, two = (comms.registry.require(name) for name in ('gate-one', 'gate-two'))
    first = await rule.wait_for_room((BuildRoomWork,), one, comms.root)
    waiting = asyncio.create_task(rule.wait_for_room((UnitStartRoomWork,), two, comms.root))
    try:
        await asyncio.sleep(0.05)
        assert not waiting.done()
        await asyncio.to_thread(first.finish)
        second = await asyncio.wait_for(waiting, 0.5)
        await asyncio.to_thread(second.finish)
    finally:
        if not waiting.done():
            waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await asyncio.to_thread(first.finish)


@pytest.mark.parametrize('command,kind', [
    ('python -B -m pytest x', TestGateRoomWork), ('pytest x', TestGateRoomWork),
    ('python /some/build_runtime.py', BuildRoomWork), ('build_runtime.py', BuildRoomWork),
])
def test_measured_command_declarations_select_one_effect(command, kind, tmp_path):
    effects = shell_storage_effects(command, tmp_path)
    assert len(effects) == 1 and effects[0].work == (kind,)


def test_waiting_sibling_wins_status_without_an_activity_copy(tmp_path):
    comms = copied_root(tmp_path)
    rule = comms.registry.storage_rule
    rule.room_ledger.root.mkdir(parents=True)
    thread = comms.registry.require('gate-one')
    ticket = RoomTicket.mint()
    with rule.room_ledger.writing() as db:
        RoomClaim(ticket, str(comms.root), thread.incarnation,
            ProcessIdentity.capture(os.getpid()), (TestGateRoomWork,), WaitingRoomPhase(), 0).insert(db)
    original = comms.agents.activity.current(thread.name)
    observed = comms.agents.activity_of(thread.name, snapshot=comms.registry.snapshot())
    assert observed.state == ActivityState.WAITING_FOR_ROOM
    assert comms.agents.activity.current(thread.name) == original
    rule.room_ledger.release(ticket)
    assert comms.agents.activity_of(thread.name, snapshot=comms.registry.snapshot()).state != observed.state
