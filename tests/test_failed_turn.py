"""Escaping faults are settled by the real turn runner, not by the test."""
import json
import os
import shutil
import time
from contextlib import AsyncExitStack
from functools import partial

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.channels import AllChannel
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.coordinator import Coordination
from agent_comms.thread_status import RunningThreadStatus
from agent_comms.turn_lease import FailedTurn
from agent_comms.wire_record import WireRecord


def copied_owner(tmp_path, *, parent='commander'):
    original = Comms(tmp_path / 'original')
    original.registry.declare(original.registry.new_thread('commander', frozenset(), str(tmp_path)))
    owner = original.registry.new_thread('unit', frozenset(), str(tmp_path), parent=parent,
        process_identity=ProcessIdentity.capture(os.getpid()))
    original.registry.declare(owner, RunningThreadStatus())
    shutil.copytree(original.root, tmp_path / 'copy')
    return Comms(tmp_path / 'copy')


def messages(comms):
    return [message for line in comms.bus.log.path.read_text().splitlines()
            for message in WireRecord.public_from_wire(json.loads(line)).messages()]


@pytest.mark.asyncio
@pytest.mark.parametrize('parent', ['commander', None])
async def test_registry_read_failure_escapes_real_runner_then_accepts_fresh_dm(tmp_path, monkeypatch, parent):
    comms = copied_owner(tmp_path, parent=parent)
    agent = CommsAgent(comms, auto_wake=False, runtime_enabled=False, agent_bin='pi')
    agent.sessions.bindings['unit'] = 'unit'
    read = comms.registry.require
    def fail_once(name):
        monkeypatch.setattr(comms.registry, 'require', read)
        raise ValueError('P689 injected registry read failure')
    started = time.monotonic()
    with pytest.raises(ValueError, match='P689 injected registry read failure'):
        async with AsyncExitStack() as resources:
            owner = await Coordination.run_worker(partial(
                agent.turns.acquire_turn, resources, 'unit', 'unit', 'failed-one', 'first task',
            ))
            lease = owner.turn_lease
            before = lease.record.read()
            monkeypatch.setattr(comms.registry, 'require', fail_once)
            await Coordination.run_worker(partial(comms.registry.require, 'unit'))
    assert time.monotonic() - started < 1
    ended = lease.record.read()
    assert isinstance(ended, FailedTurn)
    assert ended.turn == before
    assert 'P689 injected registry read failure' in ended.error
    assert not comms.registry.require('unit').executing
    notices = messages(comms)
    assert len(notices) == 1 and notices[0].target == (parent or AllChannel.declared_name)
    assert ended.error in notices[0].body
    fresh = comms.messaging.send('commander', 'unit', 'Fresh independent DM').message
    async with AsyncExitStack() as resources:
        next_owner = await Coordination.run_worker(partial(
            agent.turns.acquire_turn, resources, 'unit', 'unit', 'fresh-two', fresh.body,
        ))
        assert next_owner.turn_lease.identity.generation > lease.identity.generation
    assert lease.record.read() == ended
    assert len([m for m in messages(comms) if ended.error in m.body]) == 1
    assert not comms.registry.require('unit').executing
    print('P689: real runner failed and posted error; fresh DM accepted without stop')


@pytest.mark.asyncio
async def test_acquisition_read_fault_ends_the_already_created_turn(tmp_path, monkeypatch):
    comms = copied_owner(tmp_path)
    agent = CommsAgent(comms, auto_wake=False, runtime_enabled=False, agent_bin='pi')
    agent.sessions.bindings['unit'] = 'unit'
    snapshot = comms.registry.snapshot
    observed = []
    def fail_after_begin():
        document = snapshot()
        owner = document.threads['unit']
        if owner.executing:
            observed.append(owner.turn_record)
            monkeypatch.setattr(comms.registry, 'snapshot', snapshot)
            raise ValueError('P689 acquisition registry read failure')
        return document
    monkeypatch.setattr(comms.registry, 'snapshot', fail_after_begin)
    with pytest.raises(ValueError, match='P689 acquisition registry read failure'):
        async with AsyncExitStack() as resources:
            await Coordination.run_worker(partial(
                agent.turns.acquire_turn, resources, 'unit', 'unit', 'begin-failed', 'first task',
            ))
    record, = observed
    assert isinstance(record.read(), FailedTurn)
    assert not comms.registry.require('unit').executing
    notice, = messages(comms)
    assert notice.target == 'commander' and 'P689 acquisition registry read failure' in notice.body
