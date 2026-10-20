"""Measured admission, not estimates; the claim also owns displayed waiting."""
import asyncio
import os
import shutil
import sys
from dataclasses import replace

import pytest

from agent_comms.activity import ActivityState
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.storage_room import (
    BuildRoomWork, RoomClaim, RoomNeed, TestGateRoomWork, UnitStartRoomWork,
    WaitingRoomPhase, WorkMeasurement, WorkPeak,
)
from agent_comms.storage_rules import StorageRule, shell_storage_effects
from agent_comms.thread_status import RunningThreadStatus
from agent_comms.wire_watch import ModifiedWatchSource, open_wire_watcher


def copied_root(tmp_path):
    original = Comms(tmp_path / 'original')
    for name in ('gate-one', 'gate-two'):
        original.registry.declare(original.registry.new_thread(name, frozenset(), str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid())), RunningThreadStatus())
    root = tmp_path / 'copied'
    shutil.copytree(original.root, root)
    comms = Comms(root)
    comms.registry.storage_rule = StorageRule(volume=tmp_path, floor_gib=0, memory_gib=0,
        checkout_base=tmp_path, scratch_base=tmp_path / 'scratch')
    return comms


async def real_gate():
    process = await asyncio.create_subprocess_exec(sys.executable, '-B', '-c',
        'import sys; data=bytearray(4*1024*1024); print("running",flush=True);sys.stdin.readline()',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    assert await process.stdout.readline() == b'running\n'
    return process


async def finish_gate(process):
    process.stdin.write(b'done\n')
    await process.stdin.drain()
    process.stdin.close()
    assert await process.wait() == 0


@pytest.mark.asyncio
async def test_two_real_gates_wait_then_run_on_copied_root(tmp_path, monkeypatch):
    comms = copied_root(tmp_path)
    rule = comms.registry.storage_rule
    one, two = (comms.registry.require(name) for name in ('gate-one', 'gate-two'))
    # Calibration really runs a child and measures it. No guessed requirement.
    calibration = await rule.wait_for_room((TestGateRoomWork,), one, comms.root)
    gate = await real_gate()
    await asyncio.sleep(WorkMeasurement.sample_seconds * 3)
    await finish_gate(gate)
    await asyncio.to_thread(calibration.finish)
    with rule.room_ledger.snapshot() as db:
        measured = WorkPeak.one(db, work=TestGateRoomWork).need
    assert measured.memory > 0
    # Controlled OS reading: enough for one measured gate, but not two.
    available = measured + RoomNeed(measured.memory // 2, measured.disk // 2)
    monkeypatch.setattr(StorageRule, 'free_room', lambda self: available)
    first = await rule.wait_for_room((TestGateRoomWork,), one, comms.root)
    gate_one = await real_gate()
    source = ModifiedWatchSource(rule.room_ledger.root, frozenset(
        os.fsencode(path.name) for path in rule.room_ledger.source_paths(rule.room_ledger.root)))
    watcher = open_wire_watcher(source)
    await watcher.prepare()
    second = asyncio.create_task(rule.wait_for_room((TestGateRoomWork,), two, comms.root))
    async with asyncio.timeout(5):
        await watcher.wait()
    watcher.close()
    assert not second.done()
    activity = comms.agents.activity_of(two.name, snapshot=comms.registry.snapshot())
    assert activity.state == ActivityState.WAITING_FOR_ROOM
    assert 'Waiting For Room' in activity.ordinary_presentation(two.name).summary
    print('timeline: gate-one running; gate-two waiting for room')
    await finish_gate(gate_one)
    await asyncio.to_thread(first.finish)
    async with asyncio.timeout(5):
        next_reservation = await second
    gate_two = await real_gate()
    activity = comms.agents.activity_of(two.name, snapshot=comms.registry.snapshot())
    assert activity.state != ActivityState.WAITING_FOR_ROOM
    print('timeline: gate-one ended/released; gate-two running')
    await finish_gate(gate_two)
    await asyncio.to_thread(next_reservation.finish)
    with rule.room_ledger.snapshot() as db:
        assert RoomClaim.count(db) == 0
    assert not list(original for original in (tmp_path / 'original').glob('work_room*'))


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
    with rule.room_ledger.writing() as db:
        RoomClaim('waiting', str(comms.root), thread.incarnation,
            ProcessIdentity.capture(os.getpid()), (TestGateRoomWork,), WaitingRoomPhase(), 0).insert(db)
    original = comms.agents.activity.current(thread.name)
    observed = comms.agents.activity_of(thread.name, snapshot=comms.registry.snapshot())
    assert observed.state == ActivityState.WAITING_FOR_ROOM
    assert comms.agents.activity.current(thread.name) == original
    rule.room_ledger.release('waiting')
    assert comms.agents.activity_of(thread.name, snapshot=comms.registry.snapshot()).state != observed.state
