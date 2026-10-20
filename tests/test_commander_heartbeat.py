"""Heartbeat acceptance uses the actual rule family, worker, publication and inbox."""

import os
from contextlib import contextmanager
from dataclasses import replace

from agent_comms.child_process import ProcessIdentity
from agent_comms.commander_heartbeat import CommanderHeartbeat
from agent_comms.coordinator import Coordination
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goals import RequestGoalOrder
from agent_comms.rule_reviews import AnsweredRuleReviewEntry, DrawnRuleReviewEntry, RuleReviews
from agent_comms.standing_rules import DiskLow, RulesDue
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_status import StoppedThreadStatus
from test_commander_problem_orders import create


def connector_sender(comms):
    comms.registry.declare(
        comms.registry.new_thread("claude-ai", frozenset(), str(comms.root), created_at=10.0)
    )


def external_commander(comms, name):
    comms.registry.declare(replace(comms.registry.require(name), execution=ExternalThreadExecution))
    return comms.registry.external_process(name, ProcessIdentity.capture(os.getpid()))


async def test_real_rules_publish_once_per_change_and_rotate_units(commander, commander_script):
    comms, _ = commander
    connector_sender(comms)
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
        reviews.write("lead", DrawnRuleReviewEntry(("Keep behavior with its owner.",), 0))
        reviews.write(
            "lead",
            AnsweredRuleReviewEntry(
                1, "This review uses the existing Core rule owner and actual inbox publication."
            ),
        )
        await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
        changed = list(comms.bus.inbox("lead"))
        assert len(changed) == 3
        assert "NO LONGER CURRENT: RULES DUE" in changed[-1].body
        assert "RULE HUNT DUE" not in changed[-1].body
        assert not comms.registry.require("lead").goal.require().order.directives
    await Coordination.run_worker(lambda: heartbeat.watch_pass(opened))
    assert not heartbeat.reviews
    assert list(comms.bus.inbox("lead")) == changed


async def test_each_registered_request_holder_has_independent_wakes(commander, commander_script):
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


def test_changing_measurements_do_not_reannounce_a_rule(commander):
    comms, _ = commander
    assert (
        RulesDue("last review was 21 min ago", 100).wake_key()
        == RulesDue("last review was 26 min ago", 100).wake_key()
    )
    rule = comms.registry.storage_rule
    above = rule.floor_gib + 1
    assert DiskLow(rule, above).wake_key() == DiskLow(rule, above + 0.1).wake_key()
    assert DiskLow(rule, rule.floor_gib - 1).wake_key() != DiskLow(rule, above).wake_key()
