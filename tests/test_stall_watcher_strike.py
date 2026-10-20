import asyncio
import json
import shutil
import subprocess
import sys
from dataclasses import replace
from contextlib import contextmanager
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent_comms.comms import Comms
from agent_comms.active_route import active_route_path
from agent_comms.commander import standing
from agent_comms.commander.config import CommanderConfig
from agent_comms.connector import Connector
from agent_comms.connector_store import ConnectorStore
from agent_comms import coordinator
from agent_comms.thread_identity import StallWatcherIncarnation
from agent_comms.todos import (BlockedTodoState, ChangeTodoState, CorrectCheckTodo,
                              ReleaseTodo, StrikeReason, StrikeTodo, TodoConflict)
from test_commander_problem_orders import create
from test_standing_rules import working


def test_stall_pass_strikes_releases_and_reassigns_on_the_watcher_identity(commander, commander_script):
    comms, _ = commander
    comms.registry.declare(comms.registry.new_thread("spare", frozenset({'comms'}), str(comms.root), created_at=3.0))
    create(commander, 'P1')
    working(comms, 'worker', 2.0)
    working(comms, 'spare', 3.0)
    original = comms.root
    copied = original.with_name("copied-wire")
    shutil.copytree(original, copied)
    comms = Comms(copied)
    standing.UnownedWheelSpin.after_stall(comms)
    todo = comms.goals.todos.get("P1")
    assert [type(s) for s in todo.strikes] == [StrikeTodo]
    assert [type(r) for r in todo.released] == [ReleaseTodo]
    assert todo.strikes[0].actor == todo.released[0].actor == StallWatcherIncarnation.watcher()
    assert todo.holder == "spare" and todo.live_strikes("worker")
    assert todo.held().parent_identity == StallWatcherIncarnation.watcher()
    assert [t.id for t in comms.goals.todos.held_for_commander(comms.registry.require("lead").incarnation)] == ["P1"]
    source = Comms(original).goals.todos.get("P1")
    assert source.holder == "worker" and not source.strikes and not source.released
    (copied.parent / "copied-root-result.json").write_text(json.dumps({
        "original": str(original), "copy": str(copied), "id": todo.id,
        "holder": todo.holder, "revision": todo.revision,
        "strike_actor": todo.strikes[0].actor.to_wire(),
        "release_actor": todo.released[0].actor.to_wire(),
        "assigner": todo.held().parent_identity.to_wire(),
        "original_holder": source.holder, "original_strikes": len(source.strikes),
        "qualification": "Copied fixture root and controlled saved native records; actual history reader, no commander invocation."}, indent=2))


def test_a_failed_stall_pass_logs_and_releases_the_root_before_the_next_pass(commander, monkeypatch, caplog):
    comms, _ = commander
    passes, borrowed = [], []

    @contextmanager
    def opened_wire():
        borrowed.append(comms)
        try:
            yield comms
        finally:
            borrowed.remove(comms)

    def after_stall(cls, c):
        assert borrowed == [c]
        passes.append(c)
        if len(passes) == 1:
            raise FileNotFoundError("no commander config")

    async def wait(seconds):
        assert not borrowed
        if len(passes) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(standing.UnownedWheelSpin, "after_stall", classmethod(after_stall))
    monkeypatch.setattr(coordinator.asyncio, "sleep", wait)
    try:
        asyncio.run(coordinator.Coordination.watch_worker(
            lambda: standing.UnownedWheelSpin.watch_pass(opened_wire)))
    except asyncio.CancelledError:
        pass
    assert len(passes) == 2 and not borrowed
    assert "Service worker pass failed" in caplog.text
    assert "FileNotFoundError: no commander config" in caplog.text


def test_parked_units_come_from_the_existing_commander_configuration(commander, commander_script):
    comms, _ = commander
    config = CommanderConfig.read_root(comms.root).model_copy(update={"parked": frozenset({"worker"})})
    config.write(Path.home())
    working(comms, 'worker', 2.0)
    assert standing.WheelSpin.idle(comms, "lead") == []


def test_blocked_held_orders_are_not_struck_or_released(commander, commander_script):
    original, _ = commander
    original.registry.declare(original.registry.new_thread(
        'spare', frozenset({'comms'}), str(original.root), created_at=3.0))
    todo = create(commander, 'P1')
    blocked = original.goals.todos.apply('P1', ChangeTodoState(
        todo.revision, BlockedTodoState, original.registry.require('lead').incarnation, None))
    copied = original.root.with_name('blocked-copy')
    shutil.copytree(original.root, copied)
    comms = Comms(copied)
    standing.UnownedWheelSpin.after_stall(comms)
    with pytest.raises(TodoConflict):
        standing.UnownedWheelSpin(
            blocked, 'spare', StallWatcherIncarnation.watcher()).enforce(comms, 'lead')
    assert comms.goals.todos.get('P1') == blocked == original.goals.todos.get('P1')


def test_commander_released_backlog_is_not_automatically_reassigned(commander):
    comms, _ = commander
    todo = create(commander, 'P1')
    released = comms.goals.todos.apply('P1', ReleaseTodo(
        todo.revision, comms.registry.require('lead').incarnation, todo.held(),
        StrikeReason('Commander intentionally froze this unfinished order without authorizing automatic reassignment.')))
    standing.UnownedWheelSpin.after_stall(comms)
    assert comms.goals.todos.get('P1') == released


def test_no_spare_leaves_the_old_holder_and_strikes_untouched(commander, commander_script):
    comms, _ = commander
    original = create(commander, 'P1')
    standing.UnownedWheelSpin.after_stall(comms)
    assert comms.goals.todos.get('P1') == original
    comms.registry.declare(comms.registry.new_thread("spare", frozenset({'comms'}), str(comms.root), created_at=3.0))
    working(comms, 'worker', 2.0)
    working(comms, 'spare', 3.0)
    standing.UnownedWheelSpin.after_stall(comms)
    assigned = comms.goals.todos.get('P1')
    assert assigned.holder == 'spare'
    assert len(assigned.strikes) == len(assigned.released) == 1


def test_refused_reassignment_keeps_the_original_owner_and_strikes(commander, commander_script, tmp_path):
    comms, _ = commander
    todo = create(commander, 'P1')
    comms.registry.declare(comms.registry.new_thread('spare', frozenset({'comms'}), str(comms.root), created_at=3.0))
    working(comms, 'spare', 3.0)
    checks = tmp_path / 'checks'
    checks.mkdir()
    before = comms.goals.todos.apply('P1', CorrectCheckTodo(
        todo.revision, comms.registry.require('lead').incarnation, todo.order.check,
        replace(todo.order.check, directory=str(checks)), 'Use this check directory for the original order.'))
    copied = comms.root.with_name('refused-copy')
    shutil.copytree(comms.root, copied)
    checks.rmdir()
    with pytest.raises(ValueError):
        standing.UnownedWheelSpin.after_stall(Comms(copied))
    assert Comms(copied).goals.todos.get('P1') == before == comms.goals.todos.get('P1')


async def test_three_connector_passes_allow_real_store_maintenance_between_passes(
        commander, commander_script, tmp_path, monkeypatch, caplog):
    original, _ = commander
    before = create(commander, 'P1')
    original.registry.declare(original.registry.new_thread(
        'spare', frozenset({'comms'}), str(original.root), created_at=3.0))
    working(original, 'spare', 3.0)
    original.registry.declare(original.registry.new_thread(
        'claude-ai', frozenset(), str(original.root), created_at=4.0))
    copied = original.root.with_name('three-pass-copy')
    shutil.copytree(original.root, copied)
    connector = Connector(ConnectorStore.at(tmp_path / 'auth'), 'http://127.0.0.1:18766', copied)
    done, receipts = asyncio.Event(), []
    maintenance = '''
import fcntl, os, sys
from pathlib import Path
from agent_comms.active_route import active_route_path
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator
from agent_comms.store_files import _store_lock
barrier = MaintenanceBarrier(Path(sys.argv[1]) / 'registry.json')
held = barrier.begin(MaintenanceOperator('P717 copied-root proof'))
try:
    with _store_lock(barrier.wire_path), _store_lock(barrier.registry_path):
        directory = os.open(active_route_path().parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
            print('maintenance acquired wire, registry and route locks')
        finally:
            os.close(directory)
finally:
    barrier.clear(held)
'''

    async def between(seconds):
        run = subprocess.run([sys.executable, '-B', '-c', maintenance, str(copied)],
                             capture_output=True, text=True, timeout=10)
        assert run.returncode == 0, run.stderr
        receipts.append(run.stdout.strip())
        if len(receipts) == 3:
            done.set()
            await asyncio.Future()

    active_route_path().parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(coordinator.asyncio, 'sleep', between)
    async with TestClient(TestServer(connector.application())):
        await asyncio.wait_for(done.wait(), timeout=30)
    assert 'Service worker pass failed' not in caplog.text
    after = Comms(copied).goals.todos.get('P1')
    assert after.holder == 'spare' and len(after.strikes) == len(after.released) == 1
    assert original.goals.todos.get('P1') == before
    (tmp_path / 'three-pass-result.json').write_text(json.dumps({
        'copy': str(copied), 'passes': 3, 'maintenance': receipts,
        'holder': after.holder, 'strikes': len(after.strikes), 'original_unchanged': True}, indent=2))
