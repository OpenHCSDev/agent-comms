"""Held-order recovery and unanswered DMs on an independently copied bus."""
from contextlib import closing
from pathlib import Path
from dataclasses import replace
import os
import sqlite3
import time
from uuid import uuid4

import pytest

from source_revision_fixture import SourceRepository

from agent_comms.bus_publication import AgentOrigin
from agent_comms.change_report import FileLine
from agent_comms.comms import Comms
from agent_comms.coordination_database import CoordinationStore
from agent_comms.errors import RelationViolationError
from agent_comms.installer.carry import CarryChain, CarryLedger
from agent_comms.locked_store import SQLiteFile
from agent_comms.commander.config import CommanderConfig
from agent_comms.commander.order import Commander, SayVerb
from agent_comms.commander.standing import UnownedWheelSpin
from agent_comms.ui_model.unit_state import (
    AwaitingCommander, Blocked, Detached, UnitState, RetryableUnitBlock, UnresolvedUnitBlock,
)
from agent_comms.commander_heartbeat import CommanderHeartbeat
from agent_comms.goal_actions import BlockedGoalAction, CreateProblemGoalAction, SetGoalAction
from agent_comms.goal_attempts import AttemptRecord, GoalAttemptStore, GoalWorkDecision
from agent_comms.goal_attempt_phase import ResolvedAttempt
from agent_comms.goal_generation import GenerationState, ReadyGeneration, RetryableGeneration
from agent_comms.goal_states import ActiveGoal, BlockedGoal
from agent_comms.goals import CountCheckResult, Goal, OrderCheck, RequestGoalOrder
from agent_comms.input_disposition import InputDispositions
from agent_comms.messages import Message, QuestionKind
from agent_comms.message_page import MessagePageRequest
from agent_comms.read_basis import DMDisplayScope
from agent_comms.order_runs import MechanicalOrderRun
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.repo import Repo
from agent_comms.standing_rules import StandingRule
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.installer.backup import StoreBackup
from agent_comms.child_process import ProcessIdentity
from agent_comms.todos import DeliverTodo, FixScope, FixShape, OperationDelivery, PendingProblemOrder, TodoConflict
from agent_comms.thread_identity import CoreWatcherIncarnation
from cutover.carry_goal_work_decision import GoalWorkDecisions, RETIRED_TABLE


@pytest.fixture
def plan_repository():
    """Read the actual plan repository and installed commits; never invent a Core history."""
    return Repo.of(Path(__file__).resolve().parents[1])


@pytest.fixture
def held_bus(comms, tmp_path, source_repository, plan_repository):
    """One setup owner: real declarations, checked orders, UNKNOWN row, then a copy."""
    source = source_repository
    for name in ("lead", "worker", "outsider"):
        comms.registry.declare(comms.registry.new_thread(name=name, tags=frozenset(), worktree=str(source.checkout),
            process_identity=ProcessIdentity.capture(os.getpid())))
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
                id=f"order-{problem}", owner=FileLine("owner.py", 2), scope=source.scope(), run=MechanicalOrderRun(),
                check=OrderCheck(str(source.checkout), "printf 1", CountCheckResult(1)),
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
        commander="lead", root=comms.root, state=tmp_path, repos=(plan_repository,),
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


def test_an_active_goal_with_an_unresolved_attempt_is_reported_never_retried(held_bus):
    goal = held_bus.registry.require("worker").goal.require()
    store = held_bus.goals.open_goal_store()
    generation = store.require_generation(goal.id)
    store.recover_unreserved_ready(goal.id, generation.number)
    store.reserve(goal.id, generation.number, ready_grant=store.ready_grant(goal.id, generation.number))
    reserved = store.require_generation(goal.id)
    violation, = Blocked.violations(held_bus, "lead")
    assert isinstance(violation.blocks[0], UnresolvedUnitBlock)
    assert violation.orders == ("P1", "P2")
    violation.enforce(held_bus, "lead")
    assert held_bus.registry.require("worker").goal.require() == goal
    assert isinstance(goal.state, ActiveGoal)
    assert store.require_generation(goal.id) == reserved


@pytest.fixture(params=[member() for member in GenerationState.members_with(GenerationState)])
def holder_generation(request):
    return request.param


def test_unresolved_block_accepts_ledger_state_independently_of_goal_state(holder_generation):
    generation = holder_generation
    goal = Goal("Held work", "goal", published_seq=0)
    expected = not isinstance(generation, RetryableGeneration)
    assert UnresolvedUnitBlock.accepts(goal, generation) == expected
    blocked = replace(goal, state=BlockedGoal("Failed turn"))
    assert UnresolvedUnitBlock.accepts(blocked, generation) == expected


def test_blocked_accepts_only_real_goal_blocks_with_retryable_generations(holder_generation):
    generation = holder_generation
    goal = Goal("Held work", "goal", published_seq=0)
    assert not RetryableUnitBlock.accepts(goal, generation)
    blocked = replace(goal, state=BlockedGoal("Failed turn"))
    assert RetryableUnitBlock.accepts(blocked, generation) == isinstance(generation, RetryableGeneration)


def publish(comms, sender, target, timestamp, *, answers=()):
    return comms.bus.publisher.publish_ordinary(
        Message(sender=sender, target=target, type=QuestionKind, body="A decision is needed", timestamp=timestamp,
                answers=answers), origin=AgentOrigin(),
    )


def test_blocked_holder_republishes_every_order_without_replaying_unknown(held_bus, comms):
    reservation = blocked_holder(held_bus)
    original = comms.registry.require("worker").goal.require()
    inputs = InputDispositions(held_bus.root)
    unresolved = inputs.lookup("unresolved-input")
    violation, = Blocked.violations(held_bus, "lead")
    assert violation.blocks[0].goal.id == original.id
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


def test_blocked_holder_reissues_only_undelivered_orders(held_bus):
    delivered = held_bus.goals.todos.get("P2")
    delivered = held_bus.goals.todos.apply("P2", DeliverTodo(
        delivered.revision, held_bus.registry.require("lead").incarnation,
        OperationDelivery(at=RecordedEventTime(time.time())), delivered.order.id))
    blocked_holder(held_bus)
    violation, = Blocked.violations(held_bus, "lead")
    assert violation.orders == ("P1",)
    violation.enforce(held_bus, "lead")
    assert held_bus.goals.todos.get("P2") == delivered


def test_blocked_holder_is_reissued_its_accepted_order_after_editing_its_scope(held_bus, tmp_path):
    directory = Path(held_bus.goals.todos.get("P1").order.checked().check.directory)
    owner = directory / "owner.py"
    owner.write_text(SourceRepository.main_source)
    now = time.time()
    held_bus.goals.update_goal("worker", CreateProblemGoalAction(
        problem="P3", commander="lead", request="request",
        repo=RepositoryName.from_wire("OpenHCSDev/agent-comms"), text="Repair P3",
        review_at=RecordedEventTime(now + 3600),
        order=PendingProblemOrder(
            id="order-P3", owner=FileLine("owner.py", 2), scope=SourceRepository.scope(),
            run=MechanicalOrderRun(), check=OrderCheck(str(directory), "printf 1", CountCheckResult(1)),
            edits=("Repair the existing owner",), banned=("No second store",),
            report="Report the result", at=RecordedEventTime(now - 3600),
            first_call_deadline=RecordedEventTime(now + 3600),
        ),
    ))
    accepted = held_bus.goals.todos.get("P3").order.checked().id
    owner.write_text('"""The unit moved its class while working the order."""\n\n\nclass Owner:\n    pass\n')
    blocked_holder(held_bus)
    violation, = Blocked.violations(held_bus, "lead")
    violation.enforce(held_bus, "lead")
    assert held_bus.goals.todos.get("P3").order.checked().id != accepted
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
    violation, = AwaitingCommander.violations(held_bus, "lead")
    assert violation.questions[0].reference == question.reference
    print(violation.line())
    held_bus.messaging.send("lead", "worker", "Responding", answers=(question.reference,))
    assert AwaitingCommander.violations(held_bus, "lead") == ()
    another = publish(held_bus, "worker", "lead", now - 950)
    held_bus.messaging.send("lead", "outsider", "Answer in another conversation",
                            answers=(another.reference,))
    assert AwaitingCommander.violations(held_bus, "lead") == ()


def test_commander_say_answers_an_unanswered_unit_message(held_bus):
    question = publish(held_bus, "worker", "lead", time.time() - 901)
    ctx = Commander(**CommanderConfig.read_root(held_bus.root).model_dump())
    say = SayVerb(units="worker", body_file="unused", answers=(question.message_id,))
    held_bus.messaging.send("lead", "worker", "Responding", answers=say.answer_references(ctx))
    assert AwaitingCommander.violations(held_bus, "lead") == ()
    with pytest.raises(SystemExit, match="unanswered unit messages"):
        SayVerb(units="worker", body_file="unused", answers=("not-a-message",)).answer_references(ctx)


def test_one_mro_state_owns_simultaneous_conditions(held_bus):
    held_bus.registry.register(held_bus.registry.require("worker"), StoppedThreadStatus())
    detached, = Detached.violations(held_bus, "lead")
    assert detached.unit.name == "worker" and set(detached.orders) == {"P1", "P2"}
    blocked_holder(held_bus)
    family = UnitState.violations(held_bus, "lead")
    assert len(family) == 1 and type(family[0]) is Blocked
    assert Detached.violations(held_bus, "lead") == ()
    assert family[0] in StandingRule.broken(held_bus, "lead")


def legacy_decision_table(comms):
    store = comms.goals.open_goal_store()
    goal = comms.registry.require("worker").goal.require()
    generation = store.require_generation(goal.id)
    with closing(GoalAttemptStore.database(store.path)) as db:
        with SQLiteFile.write_transaction(db):
            GoalWorkDecision(goal.id, "recorded-decision", generation.number).claim(db)
            db.execute(f'ALTER TABLE "{GoalWorkDecision.declared_name}" RENAME TO "{RETIRED_TABLE}"')
    return store.path


def test_cutover_renames_copied_ledger_once_and_preserves_rows(held_bus, comms):
    path = legacy_decision_table(held_bus)
    original = comms.goals.open_goal_store().path.read_bytes()
    with closing(CoordinationStore.read_snapshot(path)) as db:
        before = tuple(db.execute(f'SELECT * FROM "{RETIRED_TABLE}"'))
    assert GoalWorkDecisions.carry(held_bus.root, apply=False).changed == 1
    carried = GoalWorkDecisions.carry(held_bus.root, apply=True)
    assert carried.changed == 1
    assert carried.rewritten == (str(path.relative_to(held_bus.root)),)
    assert GoalWorkDecisions.carry(held_bus.root, apply=True).changed == 0
    GoalAttemptStore(path.parent)  # the installed reader accepts the carried schema
    with closing(CoordinationStore.read_snapshot(path)) as db:
        assert tuple(db.execute(f'SELECT * FROM "{GoalWorkDecision.declared_name}"')) == before
    assert comms.goals.open_goal_store().path.read_bytes() == original
    assert held_bus.goals.open_goal_store().require_generation(
        held_bus.registry.require("worker").goal.require().id,
    ).number == 1
    assert path in GoalWorkDecisions.backup_files(held_bus.root)


def test_cutover_refuses_two_tables_without_choosing_rows(held_bus):
    path = legacy_decision_table(held_bus)
    with closing(GoalAttemptStore.database(path)) as db:
        GoalWorkDecision.create(db)
        db.commit()
    ledger = CarryLedger(held_bus.root)
    ledger.write(tuple(name for name in ledger.applied() if name != GoalWorkDecisions.name()))
    before = path.read_bytes()
    with pytest.raises(RelationViolationError):
        ledger.apply(GoalWorkDecisions.name(), lambda: GoalWorkDecisions.carry(held_bus.root, apply=True))
    assert path.read_bytes() == before
    assert GoalWorkDecisions.name() not in ledger.applied()


def test_install_discovers_and_records_decision_cutover_once(held_bus):
    chain = CarryChain.at(Path(__file__).resolve().parents[1])
    assert GoalWorkDecisions in chain.steps
    legacy_decision_table(held_bus)
    ledger = CarryLedger(held_bus.root)
    ledger.write(tuple(name for name in ledger.applied() if name != GoalWorkDecisions.name()))
    assert GoalWorkDecisions in chain.pending(ledger.applied())
    first = ledger.apply(GoalWorkDecisions.name(), lambda: GoalWorkDecisions.carry(held_bus.root, apply=True))
    assert first.changed == 1 and ledger.filename in first.rewritten
    second = ledger.apply(GoalWorkDecisions.name(), lambda: GoalWorkDecisions.carry(held_bus.root, apply=True))
    assert second.changed == 0 and second.rewritten == ()
    assert GoalWorkDecisions not in chain.pending(ledger.applied())


def test_two_restarted_heartbeats_never_repeat_a_committed_ping(held_bus):
    from agent_comms.commander_pings import LatestCommanderPing
    from agent_comms.launch_runtime import LaunchRoot
    from agent_comms.todos import CommanderCursor

    blocked_holder(held_bus)
    with LaunchRoot.from_path(held_bus.root).selected():
        first = CommanderHeartbeat()
        first.pass_body(held_bus, first.observe(held_bus))
        commander = held_bus.registry.require("lead").incarnation
        original = LatestCommanderPing.read(held_bus, first.sender, commander)
        assert original is not None
        assert len(original.pings) == 1 and original.body.startswith("BLOCKED worker:")
        assert isinstance(held_bus.registry.require("worker").goal.require().state, ActiveGoal)
        # A missing cursor simulates death after the committed send, before cursor advance.
        with held_bus.goals.todos.writing() as db:
            CommanderCursor.delete(db, commander=commander)
        for _ in range(2):
            restarted = CommanderHeartbeat()
            restarted.pass_body(held_bus, restarted.observe(held_bus))
            assert LatestCommanderPing.read(held_bus, restarted.sender, commander) == original
        with held_bus.goals.todos.snapshot() as db:
            cursor, = CommanderCursor.select(db, commander=commander)
        assert cursor.message == original.reference
        print(original.body, "RESTART 1: 0 repeated lines; RESTART 2: 0 repeated lines", sep="\n")
