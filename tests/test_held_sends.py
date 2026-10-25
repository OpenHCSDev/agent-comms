"""CLI send entrypoints wait through a private bus hold and publish once; native tool
publications refuse while held (test_connector.test_publishing_tool_held_mid_call_refuses_at_once)."""


import asyncio
import os
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from agent_comms.comms import wire
from agent_comms.coordination_tables.refusals import SendRefusal
from agent_comms.coordination_database import CoordinationStore
from agent_comms.errors import ToolRefusedError
from agent_comms.maintenance_barrier import MaintenanceOperator
from agent_comms.store_files import ExclusiveLock, StoreLockContention, _store_lock
from test_locked_store import event, process
from agent_comms.threads import Thread


def record_refusal(root, started):
    started.set()
    SendRefusal.record(root, "sender", "recipient", ToolRefusedError("retained refusal"))


def test_refusal_writer_keeps_registry_permission_until_its_sql_write_finishes(private_comms):
    root = private_comms.root
    started = event()
    with closing(sqlite3.connect(CoordinationStore.path_in(root), isolation_level=None)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        with process(record_refusal, root, started):
            try:
                assert started.wait(5)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        with _store_lock(private_comms.registry.store.path, ExclusiveLock(), StoreLockContention(0)):
                            pass
                    except BlockingIOError:
                        break
                    time.sleep(.01)
                else:
                    pytest.fail("The blocked refusal writer released its registry permission")
            finally:
                writer.execute("ROLLBACK")
    assert tuple(row.reason for row in SendRefusal.recent(root)) == ("retained refusal",)


@pytest.fixture
def held_bus(tmp_path):
    comms = wire(tmp_path)
    for name in ('sender', 'recipient'):
        comms.registry.declare(Thread(name, frozenset(), str(tmp_path)))
    assert comms.bus.inbox('recipient').page.messages == ()
    barrier = comms.owners.maintenance
    hold = barrier.begin(MaintenanceOperator.current())
    try:
        yield comms
    finally:
        if barrier.read() == hold:
            barrier.clear(hold)


def assert_published_once(comms):
    messages = comms.bus.inbox('recipient').page.messages
    assert len(messages) == 1
    assert messages[0].body == 'held publication'
    assert SendRefusal.recent(comms.root) == ()


@pytest.mark.parametrize('arguments', (
    ('send', '--from', 'sender'),
    ('user-send', '--worktree', '{project}'),
))
async def test_cli_child_waits_then_publishes_once(held_bus, arguments):
    comms = held_bus
    before = comms.bus.bus.revision()
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
    child = await asyncio.create_subprocess_exec(
        sys.executable, '-m', 'agent_comms.cli', '--root', str(comms.root),
        *(argument.format(project=comms.root) for argument in arguments),
        '--to', 'recipient', '--body', 'held publication',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    completed = asyncio.create_task(child.communicate())
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(completed), 1)
        assert child.returncode is None
        assert comms.bus.bus.revision() == before
    except BaseException:
        child.kill()
        await completed
        raise
    comms.owners.maintenance.clear(comms.owners.maintenance.read())
    stdout, stderr = await asyncio.wait_for(completed, 15)
    assert child.returncode == 0, (stdout.decode(), stderr.decode())
    assert_published_once(comms)
    print(f'{arguments[0]} child: held without publication; exit 0; published exactly once; no refusal')
