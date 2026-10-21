"""Held-order recovery and unanswered DMs on an independently copied bus."""
import asyncio
import json
import os
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import replace
from functools import partial
from pathlib import Path
from uuid import uuid4

import pytest
from cutover.carry_goal_work_decision import RETIRED_TABLE, GoalWorkDecisions

from agent_comms import agent_events as events
from agent_comms.bus_publication import AgentOrigin
from agent_comms.child_process import ProcessIdentity
from agent_comms.commander.config import CommanderConfig
from agent_comms.commander.order import Commander, OrderFile, SayVerb, SendVerb
from agent_comms.commander.standing import (
    Detached,
    GoalCannotRun,
    HeartbeatSilent,
    HolderCannotRun,
    UnitAskUnanswered,
    UnownedWheelSpin,
)
from agent_comms.commander_heartbeat import CommanderHeartbeat
from agent_comms.comms import Comms
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordinator import Coordination
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import (
    CreateProblemGoalAction, DirectiveGoalAction, ReissuedOrderGoalAction, SetGoalAction,
)
from agent_comms.goal_attempts import GoalAttemptStore, GoalWorkDecision
from agent_comms.goal_presentation import RunnableExecution
from agent_comms.goal_states import ActiveGoal
from agent_comms.goals import CountCheckResult, Goal, OrderCheck, RequestGoalOrder
from agent_comms.input_disposition import InputDispositions
from agent_comms.installer.backup import StoreBackup
from agent_comms.installer.carry import CarryChain, CarryLedger
from agent_comms.launch_root import LaunchRoot
from agent_comms.locked_store import SQLiteFile
from agent_comms.message_page import MessagePageRequest
from agent_comms.messages import InfoKind, Message
from agent_comms.native_arguments import GivenBinary
from agent_comms.order_runs import MechanicalOrderRun
from agent_comms.read_basis import DMDisplayScope
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.standing_rules import StandingRule
from agent_comms.thread_identity import CoreWatcherIncarnation
from agent_comms.thread_model import DeclaredModel
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.todos import (
    PendingProblemOrder,
    TodoConflict,
)
from conftest import declare_test_bus
from native_backend_fixture import native_backend_fixture, wait_for
from test_goal_execution import turn_on


@pytest.fixture
def held_bus(comms, tmp_path, source_repository, configured_repository):
    """One setup owner: real declarations, checked orders, UNKNOWN row, then a copy."""
    source = source_repository
    scope = source.scope()
    comms.messaging.initialize_private_initial_protocol()
    declare_test_bus(comms, channels=('comms',), threads=tuple(
        comms.registry.new_thread(name=name, tags=frozenset({'comms'}), worktree=str(source.checkout))
        for name in ('lead', 'worker', 'outsider')
    ))
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
                id=f"order-{problem}", owner=scope.shape.parents[0], scope=scope, run=MechanicalOrderRun(),
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
    configured_repository(comms)
    copied = tmp_path / "copied-bus"
    StoreBackup.copy_tree(comms.root, copied)
    copied_bus = Comms(copied)
    configured_repository(copied_bus)
    return copied_bus


def blocked_holder(comms):
    """Settle a real failed turn; never manufacture a stored backend block."""
    owner = comms.registry.require("worker")
    comms.registry.register(replace(owner, process_identity=ProcessIdentity.capture(os.getpid())))

    async def fail_turn():
        async with turn_on(comms) as turn:
            permit, = turn.goal_permit.claims
            await turn.progress.done(events.Done("The previous attempt failed", False))
        return permit.reservation

    return asyncio.run(fail_turn())


def test_an_active_goal_with_an_unresolved_attempt_is_reported_never_retried(held_bus):
    goal = held_bus.registry.require("worker").goal.require()
    store = held_bus.goals.open_goal_store()
    generation = store.require_generation(goal.id)
    store.recover_unreserved_ready(goal.id, generation.number)
    store.reserve(goal.id, generation.number, ready_grant=store.ready_grant(goal.id, generation.number))
    reserved = store.require_generation(goal.id)
    violation, = GoalCannotRun.violations(held_bus, "lead")
    assert violation.orders == ("P1", "P2")
    violation.enforce(held_bus, "lead")
    assert held_bus.registry.require("worker").goal.require() == goal
    assert isinstance(goal.state, ActiveGoal)
    assert store.require_generation(goal.id) == reserved


def publish(comms, sender, target, timestamp, *, answers=()):
    return comms.bus.publisher.publish_ordinary(
        Message(sender=sender, target=target, type=InfoKind, body="Not a question", timestamp=timestamp,
                answers=answers), origin=AgentOrigin(),
    )


def test_failed_holder_is_reported_without_reissuing_orders_or_replaying_unknown(held_bus):
    blocked_holder(held_bus)
    goal = held_bus.registry.require("worker").goal.require()
    store = held_bus.goals.open_goal_store()
    generation = store.require_generation(goal.id)
    orders = held_bus.goals.todos.held_orders()
    inputs = InputDispositions(held_bus.root)
    unresolved = inputs.lookup("unresolved-input")
    violation, = GoalCannotRun.violations(held_bus, "lead")
    assert violation.orders == ("P1", "P2")
    violation.enforce(held_bus, "lead")
    assert held_bus.registry.require("worker").goal == goal
    assert held_bus.goals.todos.held_orders() == orders
    assert store.require_generation(goal.id) == generation
    assert inputs.lookup("unresolved-input") == unresolved
    assert GoalCannotRun.violations(held_bus, "lead") == (violation,)


def test_an_order_to_a_blocked_goal_activates_it_and_keeps_other_orders(held_bus):
    blocked_holder(held_bus)
    before = held_bus.registry.require("worker").goal.require()
    todo = held_bus.goals.todos.get("P1")
    held_bus.goals.update_goal("worker", ReissuedOrderGoalAction.for_todo(
        todo, "lead", held_bus.registry.require("lead").incarnation,
    ))
    current = held_bus.registry.require("worker").goal.require()
    assert current.id == before.id and isinstance(current.state, ActiveGoal)
    assert {todo.id for todo in held_bus.goals.todos.for_goal(
        held_bus.registry.require("worker").incarnation, current.id,
    )} == {"P1", "P2"}


async def test_sigterm_group_preserves_active_goal_and_never_reissues_orders(
    held_bus, native_runtime_package, tmp_path,
):
    provider_root = tmp_path / "stopping-provider"
    provider_root.mkdir(mode=0o700)
    async with native_backend_fixture(provider_root, package=native_runtime_package) as fixture:
        fixture.provider.status = 0
        fixture.provider.response_timeout = 60
        worker = held_bus.registry.require("worker")
        held_bus.registry.register(replace(worker, model=DeclaredModel("response-local/fixture")))
        goal = held_bus.registry.require("worker").goal.require()
        orders = held_bus.goals.todos.held_orders()
        unresolved = InputDispositions(held_bus.root).lookup("unresolved-input")
        with LaunchRoot.from_path(held_bus.root).selected():
            await Coordination.run_worker(partial(
                held_bus.owners.start, "worker", agent_bin=GivenBinary("pi")))
            try:
                await wait_for(lambda: fixture.provider.posts == 1)
                assert held_bus.registry.require("worker").executing
                await Coordination.run_worker(partial(held_bus.owners.stop, "worker"))
                assert held_bus.registry.require("worker").goal == goal
                assert isinstance(held_bus.goals.goal_execution("worker"), RunnableExecution)
                for violation in GoalCannotRun.violations(held_bus, "lead"):
                    violation.enforce(held_bus, "lead")
                assert held_bus.goals.todos.held_orders() == orders
                assert InputDispositions(held_bus.root).lookup("unresolved-input") == unresolved
            finally:
                if held_bus.registry.require("worker").process_alive:
                    await Coordination.run_worker(partial(held_bus.owners.stop, "worker"))


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
        assert GoalCannotRun.violations(held_bus, "lead") == ()
    finally:
        held_bus.agents.finish_turn(lease)


def test_unanswered_uses_age_direct_target_and_later_answer_references(held_bus):
    now = time.time()
    publish(held_bus, "worker", "lead", now - 7200)  # before every held order
    publish(held_bus, "worker", "lead", now - UnitAskUnanswered.seconds + 30)
    publish(held_bus, "outsider", "lead", now - 1800)  # not a holder
    publish(held_bus, "worker", "outsider", now - 1800)  # not its commander
    question = publish(held_bus, "worker", "lead", now - 901)
    # An unrelated later answer and message text do not answer this reference.
    publish(held_bus, "lead", "worker", now - 800)
    violation, = UnitAskUnanswered.violations(held_bus, "lead")
    assert violation.message.reference == question.reference
    print(violation.line())
    held_bus.messaging.send("lead", "worker", "Responding", answers=(question.reference,))
    assert UnitAskUnanswered.violations(held_bus, "lead") == ()
    another = publish(held_bus, "worker", "lead", now - 950)
    held_bus.messaging.send("lead", "outsider", "Answer in another conversation",
                            answers=(another.reference,))
    assert UnitAskUnanswered.violations(held_bus, "lead") == ()


@pytest.mark.parametrize('target,body', [
    ('lead', 'The worker needs an answer'),
    ('#comms', '@lead The worker needs an answer'),
])
def test_commander_say_answers_an_unanswered_unit_message(held_bus, target, body, tmp_path):
    question = held_bus.bus.publisher.publish_ordinary(
        Message(sender='worker', target=target, type=InfoKind, body=body,
                timestamp=time.time() - UnitAskUnanswered.seconds - 1), origin=AgentOrigin(),
    )
    ctx = Commander.read_root(held_bus.root)
    reply = tmp_path / 'reply.txt'
    reply.write_text('Responding to the worker with the actual commander command')
    say = SayVerb(units="worker", body_file=str(reply), answers=(question.message_id,))
    say.apply(ctx)
    assert UnitAskUnanswered.violations(held_bus, "lead") == ()
    with pytest.raises(SystemExit, match="unanswered unit messages"):
        SayVerb(units="worker", body_file="unused", answers=("not-a-message",)).answer_references(ctx)


def test_an_unanswered_unit_message_gates_send_and_say_until_answered(held_bus, tmp_path):
    question = publish(held_bus, "worker", "lead", time.time() - UnitAskUnanswered.seconds - 1)
    ctx = Commander.read_root(held_bus.root)
    reply = tmp_path / "reply.txt"
    reply.write_text("Waiting on the merge queue, owner unit-goalblock")
    for verb in (SendVerb(unit="worker", body_file="unused"),
                 SayVerb(units="worker", body_file=str(reply))):
        with pytest.raises(SystemExit, match=question.message_id):
            verb.apply(ctx)
    SayVerb(units="worker", body_file=str(reply), answers=(question.message_id,)).apply(ctx)
    assert UnitAskUnanswered.violations(held_bus, "lead") == ()
    with pytest.raises(SystemExit, match="REFUSED: --answers"):
        SendVerb(unit="worker", body_file="unused", answers=(question.message_id,)).apply(ctx)


def test_answering_one_owed_message_leaves_another_owed(held_bus, tmp_path):
    first = publish(held_bus, "worker", "lead", time.time() - UnitAskUnanswered.seconds - 2)
    second = publish(held_bus, "worker", "lead", time.time() - UnitAskUnanswered.seconds - 1)
    ctx = Commander.read_root(held_bus.root)
    reply = tmp_path / "reply.txt"
    reply.write_text("Waiting on the merge queue, owner unit-goalblock")
    with pytest.raises(SystemExit, match=second.message_id):
        SayVerb(units="worker", body_file=str(reply), answers=(first.message_id,)).apply(ctx)
    assert {owed.message.message_id for owed in UnitAskUnanswered.violations(held_bus, "lead")} == {
        first.message_id, second.message_id}
    SayVerb(units="worker", body_file=str(reply),
            answers=(first.message_id, second.message_id)).apply(ctx)
    assert UnitAskUnanswered.violations(held_bus, "lead") == ()


def test_an_overdue_owner_directive_gates_until_answered(held_bus, tmp_path):
    directive = held_bus.bus.publisher.publish_ordinary(
        Message(sender="outsider", target="lead", type=InfoKind, body="Ship the install now",
                timestamp=time.time() - 3600), origin=AgentOrigin(),
    )
    held_bus.goals.update_goal("lead", DirectiveGoalAction(
        text=directive.body, seq=directive.seq, at=directive.timestamp))
    ctx = Commander.read_root(held_bus.root)
    reply = tmp_path / "reply.txt"
    reply.write_text("Installing now")
    with pytest.raises(SystemExit, match=f"DIRECTIVE UNACTED: owner directive {directive.seq}:{directive.message_id}"):
        SayVerb(units="#comms", body_file=str(reply)).apply(ctx)
    SayVerb(units="#comms", body_file=str(reply), answers=(directive.message_id,)).apply(ctx)
    SayVerb(units="#comms", body_file=str(reply)).apply(ctx)


@pytest.mark.parametrize('body', ['@outsider This question is yours', 'The worker broadcasts its result'])
def test_channel_messages_without_a_commander_addressee_are_not_a_question(held_bus, body):
    held_bus.messaging.send('worker', '#comms', body)
    assert UnitAskUnanswered.asks(held_bus, 'lead', time.time()) == ()


def test_send_and_check_accept_a_deletion_check_on_fresh_main(
    held_bus, source_repository, tmp_path, durable_home, configured_repository,
):
    source = source_repository
    # Advance only the remote: the configured tree stays stale and dirty throughout.
    content = source.main_source + 'obsolete = 1\n' * 4
    blob = source.git_input(source.checkout, content, 'hash-object', '-w', '--stdin')
    tree = source.git_input(source.checkout, f'100644 blob {blob}\towner.py\n', 'mktree')
    main = source.git(source.checkout, 'commit-tree', tree, '-p', source.main_commit,
                      '-m', 'Four obsolete declarations').strip()
    source.git(source.checkout, 'push', 'origin', f'{main}:refs/heads/main')
    local = source.checkout / 'owner.py'
    local.write_text(source.main_source + '# Uncommitted user work\n')
    before = local.read_bytes()
    head = source.git(source.checkout, 'rev-parse', 'HEAD')
    worktrees = configured_repository(held_bus, source).worktrees
    original = held_bus.goals.todos.get('P1')
    check = OrderCheck(str(source.checkout), 'grep -c obsolete owner.py', CountCheckResult(0))
    order = replace(original.order.checked(), id='delete-obsolete', check=check,
                    banned=('optional guards', 'maybe.py'), report='Report optional examples')
    document = OrderFile(order, 'Four obsolete declarations remain after their reader was deleted.',
                         'This is stupid because dead declarations still occupy the source.',
                         unplanned_reason='Repair the order tool before returning to the planned work.',
                         problems=())
    order_file = tmp_path / 'order.json'
    order_file.write_text(json.dumps(FieldCodec.encode(document)))

    def cli(*arguments):
        run = subprocess.run((sys.executable, '-c',
            'from agent_comms.commander.cli import main; main()', '--config', str(CommanderConfig.path(durable_home)),
            'order', *arguments), capture_output=True, text=True, timeout=90)
        assert run.returncode == 0, run.stdout + run.stderr
        print(run.stdout)

    # The idle outsider takes it: the worker already holds P1 and P2, and the allocation
    # policy refuses a third order to a holder while a running unit holds none.
    # Problem creates the first order through Commander.send, including its message link.
    cli('problem', str(order_file), '--to', 'outsider', '--where', 'owner.py:2')
    first = held_bus.goals.todos.get('P3')
    assert first.messages
    document = replace(document, problems=('P3',),
                       order=replace(document.order, id='replacement-deletion'))
    order_file.write_text(json.dumps(FieldCodec.encode(document)))
    cli('send', 'outsider', str(order_file), '--clean')
    sent = held_bus.goals.todos.get('P3')
    assert sent.order.check == check
    corrected = 'grep -c "^obsolete" owner.py'
    cli('check', 'P3', '--check', f'{source.checkout} :: {corrected} -> 0', '--why',
        'The original count also included comments rather than only declarations')
    current = held_bus.goals.todos.get('P3')
    assert current.order.id == sent.order.id
    assert current.review_at == sent.review_at and current.strikes == sent.strikes
    assert current.order.check.command == corrected
    assert current.order.check.result == CountCheckResult(0)
    # Done's result rule remains the inverse: the delivered commit must meet the target.
    current.order.check.result.require(current.order.check.at(
        source.branch_commit, str(source.checkout), worktrees))
    with pytest.raises(ValueError):
        current.order.check.result.require(current.order.check.at(main, str(source.checkout), worktrees))
    assert source.git(source.checkout, 'rev-parse', 'HEAD') == head
    assert local.read_bytes() == before
    assert not tuple(worktrees.iterdir())
    print('Deletion expected 0, remote main reads 4: send and check accepted; stale dirty tree unchanged.')


def test_detached_and_goal_blocks_are_both_in_standing_rule_scan(held_bus, installed_runtime):
    blocked_holder(held_bus)
    held_bus.registry.register(held_bus.registry.require("worker"), StoppedThreadStatus())
    detached, = Detached.violations(held_bus, "lead")
    assert detached.unit == "worker" and set(detached.orders) == {"P1", "P2"}
    family = HolderCannotRun.violations(held_bus, "lead")
    assert detached in family
    blocked, = GoalCannotRun.violations(held_bus, "lead")
    broken = StandingRule.broken(held_bus, "lead")
    assert blocked in broken
    assert all(rule in broken for rule in family)


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


def test_startup_heartbeat_enforces_and_publishes_both_new_rule_lines(held_bus, installed_runtime):
    blocked_holder(held_bus)
    question = publish(held_bus, "worker", "lead", time.time() - 901)
    expected = tuple(rule.line() for rule in (
        *GoalCannotRun.violations(held_bus, "lead"), *UnitAskUnanswered.violations(held_bus, "lead"),
    ))
    heartbeat = CommanderHeartbeat()
    commander = held_bus.registry.require("lead")
    held_bus.registry.register(replace(commander, process_identity=ProcessIdentity.capture(os.getpid())))
    heartbeat.pass_body(held_bus, heartbeat.observe(held_bus))
    assert commander.incarnation in heartbeat.reviews
    assert isinstance(held_bus.registry.require("worker").goal.require().state, ActiveGoal)
    page = held_bus.bus.log.read_page(MessagePageRequest(DMDisplayScope.capture(
        heartbeat.sender, "lead", held_bus.registry.snapshot(),
    )))
    message = next(message for message in page.messages if message.sender == heartbeat.sender)
    assert all(line in message.body for line in expected)
    assert question.reference.message_id in message.body
    print(message.body)


def test_every_pass_beats_and_a_stale_beat_is_heartbeat_silent(held_bus, monkeypatch):
    """10:08 on 2026-10-21: the heartbeat sent only on change, so 31 failed passes read as calm."""
    from agent_comms.commander_heartbeat import CommanderReview

    commander = held_bus.registry.require("lead")
    held_bus.registry.register(replace(commander, process_identity=ProcessIdentity.capture(os.getpid())))
    commander = held_bus.registry.require("lead")
    review = CommanderReview(frozenset())
    CommanderHeartbeat.register(held_bus.registry)
    assert HeartbeatSilent.violations(held_bus, "lead") == ()
    with held_bus.bus.locked():
        review.wake(held_bus, CommanderHeartbeat.sender, commander, ())
        review.wake(held_bus, CommanderHeartbeat.sender, commander, ())
    page = held_bus.bus.log.read_page(MessagePageRequest(DMDisplayScope.capture(
        CommanderHeartbeat.sender, "lead", held_bus.registry.snapshot(),
    )))
    changed, unchanged = (message for message in page.messages
                          if message.sender == CommanderHeartbeat.sender)
    assert "Standing rules clear." in changed.body
    assert unchanged.body == f"COMMANDER HEARTBEAT: unchanged since seq {changed.seq}, 0 violations."
    assert HeartbeatSilent.violations(held_bus, "lead") == ()
    later = unchanged.timestamp + HeartbeatSilent.seconds + 1
    monkeypatch.setattr(time, "time", lambda: later)
    silent, = HeartbeatSilent.violations(held_bus, "lead")
    assert silent.beat == unchanged.seq
    assert silent.line().startswith(f"HEARTBEAT SILENT: no beat since seq {unchanged.seq}")
