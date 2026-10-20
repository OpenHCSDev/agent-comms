"""Heartbeat acceptance uses the actual rule family, worker, publication and inbox."""

import asyncio
import json
import os
from contextlib import contextmanager, nullcontext
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.commander_heartbeat import CommanderHeartbeat
from agent_comms.coordinator import Coordination
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goals import RequestGoalOrder
from agent_comms.rule_reviews import AnsweredRuleReviewEntry, DrawnRuleReviewEntry, RuleReviews
from agent_comms.standing_rules import DiskFloorLow, DiskLow, RulesDue
from agent_comms.storage_rules import StorageRule
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_status import StoppedThreadStatus
from test_commander_problem_orders import create
from test_order_progress import checkout, commit, git


@pytest.fixture(autouse=True)
def stable_memory_threshold(commander):
    """Keep the condition true while still reading the host's actual changing measurements."""
    comms, _ = commander
    policy = replace(comms.registry.storage_rule, memory_gib=1024.0)
    (comms.root / "storage.json").write_text(json.dumps(FieldCodec.encode(policy)))


def connector_sender(comms):
    comms.registry.declare(
        comms.registry.new_thread("claude-ai", frozenset(), str(comms.root), created_at=10.0)
    )


def external_commander(comms, name):
    comms.registry.declare(replace(comms.registry.require(name), execution=ExternalThreadExecution))
    return comms.registry.external_process(name, ProcessIdentity.capture(os.getpid()))


async def test_real_rules_publish_once_per_change_and_rotate_units(commander, monkeypatch):
    comms, _ = commander
    connector_sender(comms)
    floor = comms.registry.storage_rule.floor_gib
    free = floor + 1
    monkeypatch.setattr(StorageRule, "free_gib", lambda self: free)
    heartbeat = CommanderHeartbeat()
    with external_commander(comms, "lead"):
        await Coordination.run_worker(lambda: heartbeat.watch_pass(lambda: nullcontext(comms)))
    assert not comms.bus.inbox("lead")
    comms.registry.declare(
        comms.registry.new_thread(
            "other",
            frozenset(),
            str(comms.root),
            created_at=4.0,
            execution=ExternalThreadExecution,
        ),
        StoppedThreadStatus(),
    )
    comms.registry.declare(
        replace(comms.registry.require("worker"), execution=ExternalThreadExecution),
        StoppedThreadStatus(),
    )
    comms.goals.update_goal("other", SetGoalAction(text="Other unit brief", goal_id="other-goal"))
    create(commander, "P1")
    create(commander, "P2", owner="other")

    @contextmanager
    def opened():
        yield comms

    with external_commander(comms, "lead"):
        await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
        initial = list(comms.bus.inbox("lead"))
        assert len(initial) == 1
        assert "Review next: @other (P2)" in initial[0].body
        assert "RULE DETACHED worker:" in initial[0].body
        for _ in range(3):
            await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
        assert list(comms.bus.inbox("lead")) == initial
        create(commander, "P3")
        await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
        changed = list(comms.bus.inbox("lead"))
        assert len(changed) == 2
        assert "Review next: @worker (P1, P3)" in changed[-1].body
        assert "RULE HUNT DUE" not in changed[-1].body
        await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
        assert list(comms.bus.inbox("lead")) == changed
        reviews = RuleReviews(comms.root)
        reviews.write("lead", DrawnRuleReviewEntry((
            "Keep each fact with its existing owner and derive all consumers.",
        )))
        reviews.write(
            "lead",
            AnsweredRuleReviewEntry(
                1, "Keep each fact with its existing owner and derive all consumers. "
                   "Applied at src/agent_comms/commander_heartbeat.py:23."
            ),
        )
        await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
        changed = list(comms.bus.inbox("lead"))
        assert len(changed) == 3
        assert "NO LONGER CURRENT: RULES DUE" in changed[-1].body
        assert "RULE HUNT DUE" not in changed[-1].body
        assert not comms.registry.require("lead").goal.require().order.directives
        free = floor - 1
        await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
        changed = list(comms.bus.inbox("lead"))
        assert len(changed) == 4
        assert changed[-1].body.count("RULE DISK LOW:") == 1
        assert "floor nothing allocates" in changed[-1].body
        assert "NO LONGER CURRENT: DISK LOW:" in changed[-1].body
        free -= 0.1
        await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
        assert list(comms.bus.inbox("lead")) == changed
        free = floor + 1
        await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
        changed = list(comms.bus.inbox("lead"))
        assert len(changed) == 5
        assert "NO LONGER CURRENT: DISK LOW:" in changed[-1].body
        assert "floor nothing allocates" in changed[-1].body
    await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
    assert set(heartbeat.reviews) == {
        thread.incarnation for thread in comms.registry.all_threads().values()
    }  # Process baselines survive stopped owners, including a stopped commander.
    assert list(comms.bus.inbox("lead")) == changed


async def test_each_registered_request_holder_has_independent_wakes(commander, monkeypatch):
    comms, _ = commander
    connector_sender(comms)
    comms.registry.declare(
        comms.registry.new_thread("second", frozenset(), str(comms.root), created_at=4.0)
    )
    comms.goals.update_goal(
        "second",
        SetGoalAction(
            text="Second owner request", goal_id="second-request", order=RequestGoalOrder()
        ),
    )
    heartbeat = CommanderHeartbeat()

    @contextmanager
    def opened():
        yield comms

    with external_commander(comms, "lead"), external_commander(comms, "second"):
        await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
        assert not comms.bus.inbox("lead") and not comms.bus.inbox("second")
        monkeypatch.setattr(StorageRule, "free_gib", lambda self: self.floor_gib - 1)
        await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
        assert len(comms.bus.inbox("lead")) == len(comms.bus.inbox("second")) == 1
        assert not comms.bus.inbox("worker")
    await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
    assert set(heartbeat.reviews) == {
        thread.incarnation for thread in comms.registry.all_threads().values()
    }


async def test_directive_backlog_ignores_order_revision_and_summarizes_new_directive(
        commander, monkeypatch):
    from agent_comms.connector import CALLER
    from agent_comms.goal_actions import ActiveGoalAction
    from agent_comms.goals import RequestDirective
    from agent_comms.plan_phases import RecordPlanePhase
    from agent_comms.tools import CommsOrderServeTool, CommsSendTool

    comms, caller = commander
    connector_sender(comms)
    # Expire the real recorded directives immediately, without replacing their owner or clock.
    monkeypatch.setattr(RequestDirective, "window", -1)
    create(commander, "P1")
    CommsOrderServeTool(problem="P1", serves=RecordPlanePhase).apply(comms, caller)

    def direct(number):
        CommsSendTool.invoke(comms, {"from": "claude-ai", "to": "lead",
            "body": f"Directive {number}: keep the request open and order the next repair."},
            caller=CALLER)

    def received():
        return [message for message in comms.bus.inbox("lead")
                if message.body.startswith("COMMANDER HEARTBEAT:")]

    @contextmanager
    def opened():
        yield comms

    heartbeat = CommanderHeartbeat()
    with external_commander(comms, "lead"), external_commander(comms, "worker"):
        await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
        assert not received()
        for number in range(60):
            direct(number)
        initial = comms.registry.require("lead").goal.require()
        assert len(initial.order.directives) == 60
        await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
        first = received()
        assert len(first) == 1
        directive_lines = [line for line in first[0].body.splitlines()
                           if "DIRECTIVE UNACTED:" in line]
        assert len(directive_lines) == 1 and "60 claude-ai directives" in directive_lines[0]
        oldest, newest = initial.order.directives[0], initial.order.directives[-1]
        assert f"oldest {oldest.seq} due {oldest.due_time}" in directive_lines[0]
        assert f"newest {newest.seq} due {newest.due_time}" in directive_lines[0]

        create(commander, "P2")
        CommsOrderServeTool(problem="P2", serves=RecordPlanePhase).apply(comms, caller)
        # Publishing the commander's order progress revises the request, not its directives.
        comms.goals.update_goal("lead", ActiveGoalAction(progress="Recorded P2 on this request."))
        ordered = comms.registry.require("lead").goal.require()
        assert ordered.revision > initial.revision
        assert ordered.id == initial.id and ordered.order.directives == initial.order.directives
        await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
        ordered_wakes = received()
        assert len(ordered_wakes) == 2
        assert ordered_wakes[0] == first[0]
        # P2 is deliberately historical: adding it adds real order alarms, not directives.
        assert 'RULE ORDER OVERDUE P2 worker' in ordered_wakes[-1].body
        assert 'DIRECTIVE UNACTED:' not in ordered_wakes[-1].body
        # Now change only request progress: the violation set really is unchanged.
        comms.goals.update_goal('lead', ActiveGoalAction(progress='Reviewed P2 without a new order.'))
        await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
        assert received() == ordered_wakes

        direct(60)
        await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
        changed = received()
        assert len(changed) == 3
        directive_lines = [line for line in changed[-1].body.splitlines()
                           if "DIRECTIVE UNACTED:" in line]
        assert len(directive_lines) == 1 and "61 claude-ai directives" in directive_lines[0]
        assert not directive_lines[0].startswith("NO LONGER CURRENT:")
        latest = comms.registry.require("lead").goal.require().order.directives[-1]
        assert f"newest {latest.seq} due {latest.due_time}" in directive_lines[0]
        await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
        assert received() == changed


async def test_restarted_heartbeat_sends_only_changes_after_its_baseline(commander, monkeypatch):
    comms, _ = commander
    free = comms.registry.storage_rule.floor_gib + 1
    monkeypatch.setattr(StorageRule, "free_gib", lambda self: free)

    @contextmanager
    def opened():
        yield comms

    heartbeat = CommanderHeartbeat()
    with external_commander(comms, "lead"):
        await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
        assert not comms.bus.inbox("lead")
        free = comms.registry.storage_rule.floor_gib - 1
        await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
        original = list(comms.bus.inbox("lead"))
        assert len(original) == 1
        restarted = CommanderHeartbeat()
        await Coordination.run_worker(lambda: restarted.watch_pass(opened))
        await Coordination.run_worker(lambda: restarted.watch_pass(opened))
        assert list(comms.bus.inbox("lead")) == original
        free -= 0.1  # The same violation with a new measurement is still unchanged.
        await Coordination.run_worker(lambda: restarted.watch_pass(opened))
        assert list(comms.bus.inbox("lead")) == original
        free = comms.registry.storage_rule.floor_gib + 1
        await Coordination.run_worker(lambda: restarted.watch_pass(opened))
        changed = list(comms.bus.inbox("lead"))
        assert len(changed) == 2
        lines = [line for line in changed[-1].body.splitlines()
                 if line.startswith(("RULE ", "NO LONGER CURRENT:"))]
        assert len(lines) == 2
        assert all("DISK LOW:" in line for line in lines)
        assert sum(line.startswith("NO LONGER CURRENT:") for line in lines) == 1
        await Coordination.run_worker(lambda: restarted.watch_pass(opened))
        assert list(comms.bus.inbox("lead")) == changed


async def test_heartbeat_uses_its_registered_sender_and_groups_unowned_rows(commander):
    from agent_comms.commander.standing import UnownedWheelSpin
    from agent_comms.todos import StrikeReason

    comms, _ = commander
    connector_sender(comms)

    @contextmanager
    def opened():
        yield comms

    heartbeat = CommanderHeartbeat()
    with external_commander(comms, "lead"):
        await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
        assert not comms.bus.inbox("lead")
        for number in range(3):
            create(commander, f"P{number}")
        comms.goals.fire("worker", StrikeReason(
            "Release these rows so the heartbeat reports the missing unit honestly."), "lead")
        await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
        messages = list(comms.bus.inbox("lead"))
        assert len(messages) == 1
        assert messages[0].sender == "commander-heartbeat"
        assert messages[0].sender != "claude-ai"
        assert "commander-heartbeat" in comms.registry
        assert "RULE 3 unowned rows wait for a new unit: P0, P1, P2" in messages[0].body
        assert "WHEEL SPIN:" not in messages[0].body
        assert "GROWTH" not in messages[0].body
        assert "unit-p" not in messages[0].body
        before = frozenset(UnownedWheelSpin.violations(comms, "lead"))
        assert len(before) == 3
        assert frozenset(UnownedWheelSpin.violations(comms, "lead")) == before
        create(commander, "P3")
        comms.goals.fire("worker", StrikeReason(
            "Release the additional row to prove the grouped row list changes."), "lead")
        await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
        messages = list(comms.bus.inbox("lead"))
        assert len(messages) == 2
        grouped = [line for line in messages[-1].body.splitlines() if "unowned rows" in line]
        assert grouped == ["RULE 4 unowned rows wait for a new unit: P0, P1, P2, P3"]
        assert not comms.registry.require("lead").goal.require().order.directives


async def test_heartbeat_closes_merged_order_and_survives_maintenance(
        commander, checkout, tmp_path, monkeypatch, caplog):
    from agent_comms.connector import Connector
    from agent_comms.connector_store import ConnectorStore
    from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator, ReadyPhase
    from agent_comms.todos import DoneTodoState, MergedProblemOrder, MergedPullRequest, PullRequests

    comms, _ = commander
    connector_sender(comms)
    todo, unit = checkout
    todo = replace(todo, order=replace(todo.order,
        check=replace(todo.order.check, directory=str(unit))))
    with comms.goals.todos.writing() as db:
        todo.upsert(db)
    commit(unit)
    git(unit, 'switch', '-q', 'main')
    git(unit, 'merge', '-q', '--ff-only', 'fix/p1')
    sha = git(unit, 'rev-parse', 'HEAD')
    merge = MergedPullRequest(1, 'fix/p1', sha, merge=sha, merged_at=500,
                              updated_at=500, orders=(todo.order.id,))
    monkeypatch.setattr(PullRequests, 'read', lambda self, repo, state, order:
                        (merge,) if state == 'merged' else ())
    connector = Connector(ConnectorStore.at(tmp_path / 'auth'), 'http://127.0.0.1', comms.root)
    heartbeat = CommanderHeartbeat()
    barrier = MaintenanceBarrier(comms.registry.store.path)
    pauses = []

    async def between(seconds):
        assert seconds == 60
        pauses.append(seconds)
        if len(pauses) == 1:
            assert comms.goals.todos.get(todo.id) == todo and not heartbeat.reviews
            barrier.advance(held, ReadyPhase)
        else:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, 'sleep', between)
    with external_commander(comms, 'lead'):
        held = barrier.begin(MaintenanceOperator.current())
        try:
            with pytest.raises(asyncio.CancelledError):
                await Coordination.watch_worker(lambda: heartbeat.watch_pass(connector.opened_wire))
            closed = comms.goals.todos.get(todo.id)
            assert closed.state is DoneTodoState and closed.assignment is None
            assert isinstance(closed.order, MergedProblemOrder) and closed.order.sha == sha
            assert closed.revision == todo.revision + 1
            await Coordination.run_worker(lambda: heartbeat.watch_pass(connector.opened_wire))
            assert comms.goals.todos.get(todo.id) == closed
        finally:
            if barrier.read().phase is not ReadyPhase:
                barrier.clear(held)
    assert pauses == [60, 60]
    assert 'MaintenanceHeldError' in caplog.text


@pytest.mark.parametrize("commander_stopped_at_baseline", (False, True))
async def test_holder_process_restart_wakes_once_without_replaying_orders(
        commander, commander_stopped_at_baseline, monkeypatch):
    import subprocess
    from contextlib import ExitStack
    from agent_comms.todos import TodoStore

    comms, _ = commander
    held_reads = []
    held_by = TodoStore.held_by

    def read_held(store, commander, unit):
        held_reads.append(unit)
        return held_by(store, commander, unit)

    monkeypatch.setattr(TodoStore, "held_by", read_held)
    create(commander, "P1")
    create(commander, "P2")
    heartbeat = CommanderHeartbeat()

    def restart_messages():
        return [message for message in comms.bus.inbox("worker")
                if message.sender == heartbeat.sender]

    with ExitStack() as owners:
        if not commander_stopped_at_baseline:
            owners.enter_context(external_commander(comms, "lead"))
        await Coordination.run_worker(lambda: heartbeat.watch_pass(lambda: nullcontext(comms)))
        assert not restart_messages()
        assert not held_reads
        if commander_stopped_at_baseline:
            owners.enter_context(external_commander(comms, "lead"))
        # Register an actual different process through the external-owner lifecycle.
        process = subprocess.Popen(["sleep", "30"])
        try:
            comms.registry.declare(replace(comms.registry.require("worker"),
                                          execution=ExternalThreadExecution))
            with comms.registry.external_process("worker", ProcessIdentity.capture(process.pid)):
                await Coordination.run_worker(lambda: heartbeat.watch_pass(lambda: nullcontext(comms)))
                messages = restart_messages()
                assert len(messages) == 1
                assert messages[0].body == (
                    "Restarted holding order-P1, order-P2; continue them from your branch")
                reads_after_restart = tuple(held_reads)
                assert "worker" in reads_after_restart
                for _ in range(2):
                    await Coordination.run_worker(lambda: heartbeat.watch_pass(lambda: nullcontext(comms)))
                assert tuple(held_reads) == reads_after_restart
                assert restart_messages() == messages
                restarted_connector = CommanderHeartbeat()
                await Coordination.run_worker(lambda: restarted_connector.watch_pass(lambda: nullcontext(comms)))
                assert tuple(held_reads) == reads_after_restart
                assert restart_messages() == messages
                create(commander, "P3")
                await Coordination.run_worker(lambda: heartbeat.watch_pass(lambda: nullcontext(comms)))
                assert restart_messages() == messages
        finally:
            process.terminate()
            process.wait(timeout=5)


def test_changing_measurements_do_not_reannounce_a_rule(commander):
    comms, _ = commander
    assert len({
        RulesDue("last review was 21 min ago", 100),
        RulesDue("last review was 26 min ago", 100),
    }) == 1
    assert RulesDue("new overdue review", 101) != RulesDue("old overdue review", 100)
    rule = comms.registry.storage_rule
    above = rule.floor_gib + 1
    assert len({DiskLow(rule, above), DiskLow(rule, above + 0.1)}) == 1
    assert len({DiskFloorLow(rule, rule.floor_gib - 1),
                DiskFloorLow(rule, rule.floor_gib - 2)}) == 1
    assert DiskFloorLow(rule, rule.floor_gib - 1) != DiskLow(rule, above)
    assert DiskLow(replace(rule, cleanup_gib=rule.cleanup_gib + 1), above) != DiskLow(rule, above)
