"""Held-order recovery and unanswered DMs on an independently copied bus."""
from contextlib import closing
from dataclasses import replace
import os
import sqlite3
import time
from uuid import uuid4

import pytest

from agent_comms.bus_publication import AgentOrigin
from agent_comms.change_report import FileLine
from agent_comms.comms import Comms
from agent_comms.commander.config import CommanderConfig
from agent_comms.commander.standing import (
    Blocked, Detached, HolderCannotRun, UnitAskUnanswered, UnownedWheelSpin,
)
from agent_comms.commander_heartbeat import CommanderHeartbeat
from agent_comms.goal_actions import BlockedGoalAction, CreateProblemGoalAction, SetGoalAction
from agent_comms.goal_attempts import AttemptRecord
from agent_comms.goal_attempt_phase import ResolvedAttempt
from agent_comms.goal_generation import ReadyGeneration
from agent_comms.goal_states import ActiveGoal
from agent_comms.goals import CountCheckResult, OrderCheck, RequestGoalOrder
from agent_comms.input_disposition import InputDispositions
from agent_comms.messages import InfoKind, Message
from agent_comms.message_page import MessagePageRequest
from agent_comms.read_basis import DMDisplayScope
from agent_comms.order_runs import MechanicalOrderRun
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.standing_rules import StandingRule
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.installer.backup import StoreBackup
from agent_comms.child_process import ProcessIdentity
from agent_comms.todos import PendingProblemOrder, TodoConflict
from agent_comms.thread_identity import CoreWatcherIncarnation


@pytest.fixture
def held_bus(comms, tmp_path):
    """One setup owner: real declarations, checked orders, UNKNOWN row, then a copy."""
    for name in ("lead", "worker", "outsider"):
        comms.registry.declare(comms.registry.new_thread(name=name, tags=frozenset(), worktree=str(tmp_path)))
    comms.goals.update_goal("lead", SetGoalAction(
        text="Repair the held work", goal_id="request", order=RequestGoalOrder(),
    ))
    now = time.time()
    for problem in ("P1", "P2"):
        comms.goals.update_goal("worker", CreateProblemGoalAction(
            problem=problem, commander="lead", request="request",
            repo=RepositoryName.from_wire("OpenHCSDev/agent-comms"), text=f"Repair {problem}",
            review_at=RecordedEventTime(now + 3600),
            order=PendingProblemOrder(
                id=f"order-{problem}", owner=FileLine(__file__, 1), run=MechanicalOrderRun(),
                check=OrderCheck(str(tmp_path), "printf 1", CountCheckResult(1)),
                edits=("Repair the existing owner",), banned=("No second store",),
                report="Report the result", at=RecordedEventTime(now - 3600),
                first_call_deadline=RecordedEventTime(now + 3600),
            ),
        ))
    inputs = InputDispositions(comms.root)
    inputs.record("unresolved-input", seq=None, owner="worker", claim_scope=1,
                  target="worker", text="Do not replay this input")
    assert inputs.bind("unresolved-input", claim_scope=1, turn_id="old-turn",
                       native_id=uuid4().hex, text="Do not replay this input")
    config = CommanderConfig(
        commander="lead", root=comms.root, state=tmp_path, repos=(),
        worktrees=tmp_path, scratch=tmp_path, disk=tmp_path,
    )
    config.root_path(comms.root).write_text(config.model_dump_json())
    copied = tmp_path / "copied-bus"
    StoreBackup.copy_tree(comms.root, copied)
    return Comms(copied)


def blocked_holder(comms):
    """An actual failed ledger reservation, not merely a displayed blocked label."""
    goal = comms.registry.require("worker").goal.require()
    store = comms.goals.open_goal_store()
    generation = store.require_generation(goal.id)
    store.recover_unreserved_ready(goal.id, generation.number)
    reservation = store.reserve(goal.id, generation.number, ready_grant=store.ready_grant(goal.id, generation.number))
    reservation.fail(store, "The previous attempt has an uncertain result")
    comms.goals.update_goal("worker", BlockedGoalAction(progress="A reason unrelated to matching text"))
    return reservation


def publish(comms, sender, target, timestamp, *, answers=()):
    return comms.bus.publisher.publish_ordinary(
        Message(sender=sender, target=target, type=InfoKind, body="Not a question", timestamp=timestamp,
                answers=answers), origin=AgentOrigin(),
    )


def test_blocked_holder_republishes_every_order_without_replaying_unknown(held_bus, comms):
    reservation = blocked_holder(held_bus)
    original = comms.registry.require("worker").goal.require()
    inputs = InputDispositions(held_bus.root)
    unresolved = inputs.lookup("unresolved-input")
    violation, = Blocked.violations(held_bus, "lead")
    assert violation.block_record.is_file()
    print(violation.line())
    previous_orders = {todo.id: todo.order.checked().id
                       for todo in held_bus.goals.todos.held_orders()}
    violation.enforce(held_bus, "lead")
    current = held_bus.registry.require("worker").goal.require()
    assert current.id == original.id and isinstance(current.state, ActiveGoal)
    assigned = held_bus.goals.todos.for_goal(held_bus.registry.require("worker").incarnation, current.id)
    assert {todo.id for todo in assigned} == {"P1", "P2"}
    assert all(todo.order.checked().id != previous_orders[todo.id] for todo in assigned)
    assert isinstance(held_bus.goals.open_goal_store().require_generation(current.id).lifecycle,
                      ReadyGeneration)
    with closing(sqlite3.connect(held_bus.goals.open_goal_store().path)) as connection:
        assert AttemptRecord.read_in(connection, reservation.attempt_id).holds(reservation, ResolvedAttempt())
    assert inputs.lookup("unresolved-input") == unresolved
    assert comms.registry.require("worker").goal.require() == original
    assert Blocked.violations(held_bus, "lead") == ()


def test_an_order_to_a_blocked_goal_activates_it_and_keeps_other_orders(held_bus):
    blocked_holder(held_bus)
    before = held_bus.registry.require("worker").goal.require()
    todo = held_bus.goals.todos.get("P1")
    held_bus.goals.update_goal("worker", UnownedWheelSpin.order_for(
        todo, "lead", held_bus.registry.require("lead").incarnation,
    ))
    current = held_bus.registry.require("worker").goal.require()
    assert current.id == before.id and isinstance(current.state, ActiveGoal)
    assert {todo.id for todo in held_bus.goals.todos.for_goal(
        held_bus.registry.require("worker").incarnation, current.id,
    )} == {"P1", "P2"}


def test_goal_generation_cannot_transfer_to_a_different_owner(held_bus):
    todo = held_bus.goals.todos.get("P1")
    with pytest.raises(TodoConflict):
        held_bus.goals.todos.assign(
            todo.id, expected_revision=todo.revision,
            owner=held_bus.registry.require("outsider"), assigner=CoreWatcherIncarnation.watcher(),
            generation=todo.held().generation, goal=todo.goal, review_at=todo.review_at,
            order=todo.order.checked().reissued(time.time()),
        )
    assert held_bus.goals.todos.get(todo.id) == todo


def test_a_running_turn_is_not_a_stalled_holder(held_bus):
    blocked_holder(held_bus)
    thread = held_bus.registry.require("worker")
    held_bus.registry.register(replace(thread, process_identity=ProcessIdentity.capture(os.getpid())))
    owner = held_bus.agents.begin_turn("worker", "current-direct-task")
    lease = owner.thread.turn_lease
    assert lease is not None
    try:
        assert Blocked.violations(held_bus, "lead") == ()
    finally:
        held_bus.agents.finish_turn(lease)


def test_unanswered_uses_age_direct_target_and_later_answer_references(held_bus):
    now = time.time()
    publish(held_bus, "worker", "lead", now - 7200)  # before every held order
    publish(held_bus, "worker", "lead", now - 870)  # younger than fifteen minutes
    publish(held_bus, "outsider", "lead", now - 1800)  # not a holder
    publish(held_bus, "worker", "outsider", now - 1800)  # not its commander
    question = publish(held_bus, "worker", "lead", now - 901)
    # An unrelated later answer and message text do not answer this reference.
    publish(held_bus, "lead", "worker", now - 800)
    violation, = UnitAskUnanswered.violations(held_bus, "lead")
    assert violation.reference == question.reference
    print(violation.line())
    held_bus.messaging.send("lead", "worker", "Responding", answers=(question.reference,))
    assert UnitAskUnanswered.violations(held_bus, "lead") == ()
    another = publish(held_bus, "worker", "lead", now - 950)
    held_bus.messaging.send("lead", "outsider", "Answer in another conversation",
                            answers=(another.reference,))
    assert UnitAskUnanswered.violations(held_bus, "lead") == ()


def test_detached_and_whole_family_scan_still_find_nonreceiving_holders(held_bus):
    blocked_holder(held_bus)
    held_bus.registry.register(held_bus.registry.require("worker"), StoppedThreadStatus())
    detached, = Detached.violations(held_bus, "lead")
    assert detached.unit == "worker" and set(detached.orders) == {"P1", "P2"}
    family = HolderCannotRun.violations(held_bus, "lead")
    assert detached in family
    blocked, = Blocked.violations(held_bus, "lead")
    assert blocked in family
    broken = StandingRule.broken(held_bus, "lead")
    assert all(rule in broken for rule in family)


def test_startup_heartbeat_enforces_and_publishes_both_new_rule_lines(held_bus):
    blocked_holder(held_bus)
    question = publish(held_bus, "worker", "lead", time.time() - 901)
    expected = tuple(rule.line() for rule in (
        *Blocked.violations(held_bus, "lead"), *UnitAskUnanswered.violations(held_bus, "lead"),
    ))
    heartbeat = CommanderHeartbeat()
    commander = held_bus.registry.require("lead")
    held_bus.registry.register(replace(commander, process_identity=ProcessIdentity.capture(os.getpid())))
    heartbeat.pass_body(held_bus)
    assert commander.incarnation in heartbeat.reviews
    assert isinstance(held_bus.registry.require("worker").goal.require().state, ActiveGoal)
    page = held_bus.bus.log.read_page(MessagePageRequest(DMDisplayScope.capture(
        heartbeat.sender, "lead", held_bus.registry.snapshot(),
    )))
    message = next(message for message in page.messages if message.sender == heartbeat.sender)
    assert all(line in message.body for line in expected)
    assert question.reference.message_id in message.body
    print(message.body)
