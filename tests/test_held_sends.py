"""Real send entrypoints wait through a private bus hold and publish once."""
import asyncio
import os
import sys
from pathlib import Path

import pytest

from agent_comms.comms import Comms
from agent_comms.coordination_tables.refusals import SendRefusal
from agent_comms.maintenance_barrier import MaintenanceOperator
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import invoke_tool


@pytest.fixture
def held_bus(tmp_path):
    comms = Comms(tmp_path)
    comms.messaging.initialize_private_initial_protocol()
    for name in ('sender', 'recipient'):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(tmp_path)))
    assert comms.bus.inbox('recipient') == []
    barrier = comms.owners.maintenance
    hold = barrier.begin(MaintenanceOperator.current())
    try:
        yield comms
    finally:
        if barrier.read() == hold:
            barrier.clear(hold)


def assert_published_once(comms):
    messages = comms.bus.inbox('recipient')
    assert len(messages) == 1
    assert messages[0].body == 'held publication'
    assert SendRefusal.recent(comms.root) == ()


@pytest.mark.parametrize('arguments', (
    ('send', '--from', 'sender'),
    ('user-send',),
))
async def test_cli_child_waits_then_publishes_once(held_bus, arguments):
    comms = held_bus
    before = comms.bus.log.path.read_bytes()
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
    child = await asyncio.create_subprocess_exec(
        sys.executable, '-m', 'agent_comms.cli', '--root', str(comms.root),
        *arguments, '--to', 'recipient', '--body', 'held publication',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    completed = asyncio.create_task(child.communicate())
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(completed), 1)
        assert child.returncode is None
        assert comms.bus.log.path.read_bytes() == before
    except BaseException:
        child.kill()
        await completed
        raise
    comms.owners.maintenance.clear(comms.owners.maintenance.read())
    stdout, stderr = await asyncio.wait_for(completed, 15)
    assert child.returncode == 0, (stdout.decode(), stderr.decode())
    assert_published_once(comms)
    print(f'{arguments[0]} child: held without publication; exit 0; published exactly once; no refusal')


async def test_native_thread_caller_waits_then_publishes_once(held_bus):
    comms = held_bus
    before = comms.bus.log.path.read_bytes()
    pending = asyncio.create_task(asyncio.to_thread(
        invoke_tool, comms, 'comms_send',
        {'from': 'sender', 'to': 'recipient', 'body': 'held publication'},
        caller=ThreadCaller('sender')))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(pending), .25)
    assert comms.bus.log.path.read_bytes() == before
    comms.owners.maintenance.clear(comms.owners.maintenance.read())
    await asyncio.wait_for(pending, 15)
    assert_published_once(comms)
    print('native ThreadCaller: held without publication; published exactly once; no refusal')
