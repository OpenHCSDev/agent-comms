import asyncio
import json
import shutil
from contextlib import contextmanager
from pathlib import Path

from agent_comms.comms import Comms
from agent_comms.commander import standing
from agent_comms.commander.config import CommanderConfig
from agent_comms.thread_identity import StallWatcherIncarnation
from agent_comms.todos import BlockedTodoState, ChangeTodoState, ReleaseTodo, StrikeReason, StrikeTodo
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
    monkeypatch.setattr(standing.asyncio, "sleep", wait)
    try:
        asyncio.run(standing.UnownedWheelSpin.watch(opened_wire))
    except asyncio.CancelledError:
        pass
    assert len(passes) == 2 and not borrowed
    assert "Stall watcher pass failed" in caplog.text
    assert "FileNotFoundError: no commander config" in caplog.text


def test_parked_units_come_from_the_existing_commander_configuration(commander, commander_script):
    comms, _ = commander
    config = CommanderConfig.installed().model_copy(update={"parked": frozenset({"worker"})})
    config.write(Path.home())
    CommanderConfig.installed.cache_clear()
    working(comms, 'worker', 2.0)
    assert standing.WheelSpin.idle(comms, "lead") == []


def test_blocked_held_orders_are_not_struck_or_released(commander):
    comms, _ = commander
    todo = create(commander, 'P1')
    blocked = comms.goals.todos.apply('P1', ChangeTodoState(
        todo.revision, BlockedTodoState, comms.registry.require('lead').incarnation, None))
    standing.UnownedWheelSpin.after_stall(comms)
    assert comms.goals.todos.get('P1') == blocked


def test_commander_released_backlog_is_not_automatically_reassigned(commander):
    comms, _ = commander
    todo = create(commander, 'P1')
    released = comms.goals.todos.apply('P1', ReleaseTodo(
        todo.revision, comms.registry.require('lead').incarnation, todo.held(),
        StrikeReason('Commander intentionally froze this unfinished order without authorizing automatic reassignment.')))
    standing.UnownedWheelSpin.after_stall(comms)
    assert comms.goals.todos.get('P1') == released


def test_watcher_release_waits_for_a_spare_without_striking_twice(commander, commander_script):
    comms, _ = commander
    create(commander, 'P1')
    standing.UnownedWheelSpin.after_stall(comms)
    waiting = comms.goals.todos.get('P1')
    assert waiting.holder is None and len(waiting.strikes) == len(waiting.released) == 1
    comms.registry.declare(comms.registry.new_thread("spare", frozenset({'comms'}), str(comms.root), created_at=3.0))
    working(comms, 'worker', 2.0)
    working(comms, 'spare', 3.0)
    standing.UnownedWheelSpin.after_stall(comms)
    assigned = comms.goals.todos.get('P1')
    assert assigned.holder == 'spare'
    assert len(assigned.strikes) == len(assigned.released) == 1
