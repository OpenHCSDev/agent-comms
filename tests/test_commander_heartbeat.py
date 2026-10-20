"""Heartbeat acceptance uses the actual rule family, worker, publication and inbox."""

import json
import os
from contextlib import contextmanager
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
    heartbeat = CommanderHeartbeat("claude-ai")

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
    assert not heartbeat.reviews
    assert list(comms.bus.inbox("lead")) == changed


async def test_each_registered_request_holder_has_independent_wakes(commander):
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
    heartbeat = CommanderHeartbeat("claude-ai")

    @contextmanager
    def opened():
        yield comms

    with external_commander(comms, "lead"), external_commander(comms, "second"):
        await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
        await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
        assert len(comms.bus.inbox("lead")) == len(comms.bus.inbox("second")) == 1
        assert not comms.bus.inbox("worker")
    await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
    assert not heartbeat.reviews


async def test_directive_backlog_ignores_order_revision_and_summarizes_new_directive(
        commander, monkeypatch):
    from agent_comms.connector import CALLER
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

    heartbeat = CommanderHeartbeat("claude-ai")
    with external_commander(comms, "lead"), external_commander(comms, "worker"):
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
        ordered = comms.registry.require("lead").goal.require()
        assert ordered.revision > initial.revision
        assert ordered.id == initial.id and ordered.order.directives == initial.order.directives
        await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
        assert received() == first

        direct(60)
        await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
        changed = received()
        assert len(changed) == 2
        directive_lines = [line for line in changed[-1].body.splitlines()
                           if "DIRECTIVE UNACTED:" in line]
        assert len(directive_lines) == 1 and "61 claude-ai directives" in directive_lines[0]
        assert not directive_lines[0].startswith("NO LONGER CURRENT:")
        latest = comms.registry.require("lead").goal.require().order.directives[-1]
        assert f"newest {latest.seq} due {latest.due_time}" in directive_lines[0]
        await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
        assert received() == changed


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
