"""Held-order recovery and unanswered DMs on an independently copied bus."""

from agent_comms.channel_targets import CommandTarget
import asyncio
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
from uuid import uuid4

import pytest

from agent_comms import agent_events as events
from agent_comms.bus import Bus
from agent_comms.bus_publication import AgentOrigin, HumanOrigin
from agent_comms.child_process import ProcessIdentity
from agent_comms.commander.config import CommanderConfig
from agent_comms.commander.order import Commander, OrderFile, SayVerb, SendVerb
from agent_comms.commander.questions import UnansweredBurst, UnansweredUnitAsks
from agent_comms.commander.standing import (
    HeartbeatSilent,
    HeartbeatNotStarted,
    MissedHeartbeat,
    AnswersTo,
    UnitAskUnanswered,
)
from agent_comms.comms import Comms
from agent_comms.commander_heartbeat import CommanderHeartbeat
from agent_comms.coordinator import Coordination
from agent_comms.errors import CommittedMessageError, HumanSendRefusedError, RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import (
    CreateProblemGoalAction,
    ReissuedOrderGoalAction,
    SetGoalAction,
)
from agent_comms.goal_presentation import RunnableExecution
from agent_comms.goal_states import ActiveGoal
from agent_comms.goals import CountCheckResult, OrderCheck
from agent_comms.owner_requests import NewestOpenRequest, RequestDirective
from agent_comms.input_disposition import InputDispositions
from agent_comms.installer.backup import StoreBackup
from agent_comms.launch_root import LaunchRoot
from agent_comms.message_page import MessagePageRequest
from agent_comms.messages import ConversationRole, InfoKind, Message, NoticeRole, QuestionKind
from agent_comms.message_reference import MessageReference
from agent_comms.native_arguments import GivenBinary
from agent_comms.order_runs import MechanicalOrderRun
from agent_comms.read_basis import DMDisplayScope, RepliesDisplayScope
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.standing_rules import StandingRule
from agent_comms.thread_identity import CoreWatcherIncarnation
from agent_comms.thread_model import DeclaredModel
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.todos import (
    ClaimOrder,
    OrderClaim,
    OrderMessage,
    PendingProblemOrder,
    ProgressRead,
    TodoConflict,
)
from agent_comms.ui_model.unit_state import AwaitingCommander, Blocked, Detached, UnitState
from cli_command import commander
from conftest import declare_test_bus
from native_backend_fixture import native_backend_fixture, wait_for
from test_goal_execution import turn_on


@pytest.fixture
def held_bus(comms, tmp_path, source_repository, configured_repository):
    """One setup owner: real declarations, checked orders, UNKNOWN row, then a copy."""
    source = source_repository
    scope = source.scope()
    Bus.install(Bus.path_in(comms.root))
    declare_test_bus(comms, channels=('comms',), threads=tuple(
        comms.registry.new_thread(name=name, tags=frozenset({'comms'}), worktree=str(source.checkout))
        for name in ('lead', 'worker', 'outsider')
    ))
    request = comms.goals.todos.add_request(
        comms.registry.require('lead').incarnation, 'Repair the held work').id
    now = time.time()
    for problem in ("P1", "P2"):
        comms.goals.update_goal("worker", CreateProblemGoalAction(
            problem=problem, commander="lead", request=request,
            assigner=comms.registry.require('lead').incarnation,
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


def test_stop_rejects_a_human_before_any_registry_change(comms, tmp_path):
    reader = comms.messaging.user_identity(str(tmp_path))
    before = comms.registry.store.path.read_bytes()
    with pytest.raises(RelationViolationError, match="Only agent threads can be stopped"):
        comms.owners.stop(reader.name)
    assert comms.registry.store.path.read_bytes() == before


@pytest.fixture
def claim_order(held_bus):
    """Publish the real order and claim messages, using their retained identities."""
    def claim(problem):
        todo = held_bus.goals.todos.get(problem)
        lead = held_bus.registry.require("lead").incarnation
        worker = held_bus.registry.require("worker").incarnation
        original = held_bus.messaging.send("lead", "worker", todo.order.instructions(todo)).message
        todo = held_bus.goals.todos.apply(
            todo.id, OrderMessage(todo.revision, lead, original.message_id))
        claimed = OrderClaim.for_order(todo, worker, 1, "a" * 40,
                                      "Review the published repair", original.reference,
                                      store=held_bus.goals.todos)
        claimed = replace(claimed, message=replace(
            claimed.message, timestamp=time.time() - AwaitingCommander.seconds - 1))
        ClaimOrder(todo.revision, worker, todo.held().generation, claimed).record(
            held_bus.goals, todo.id)
        return claimed
    return claim


def state_cards(comms, member):
    return tuple(state for state in UnitState.current(comms, "lead", ProgressRead())
                 if isinstance(state, member))


@pytest.mark.parametrize("change,notification_fails", [
    ("rename", False), ("rename_holder", False), ("remove", False), ("recreate", False),
    ("rename", True),
])
def test_order_claim_publishes_only_to_its_retained_commander_birth(
    held_bus, change, notification_fails,
):
    comms = held_bus
    todo = comms.goals.todos.get("P1")
    lead = comms.registry.require("lead").incarnation
    worker = comms.registry.require("worker").incarnation
    original = comms.messaging.send("lead", "worker", todo.order.instructions(todo)).message
    todo = comms.goals.todos.apply(
        todo.id, OrderMessage(todo.revision, lead, original.message_id))
    claim = OrderClaim.for_order(
        todo, worker, 1, "a" * 40, "Review this published repair with @outsider", original.reference,
        store=comms.goals.todos)
    if change == "rename":
        comms.registry.rename("lead", "renamed")
    elif change == "rename_holder":
        comms.registry.rename("worker", "renamed-worker")
    else:
        worktree = comms.registry.require("lead").worktree
        comms.registry.unregister("lead")
        comms.registry.remove("lead")
        if change == "recreate":
            comms.registry.declare(comms.registry.new_thread("lead", frozenset({"comms"}), worktree))
    before = comms.bus.bus.revision()
    operation = ClaimOrder(todo.revision, worker, todo.held().generation, claim)
    if change in ("rename", "rename_holder"):
        def registry_cut_held():
            try:
                with comms.registry.store.locked(blocking=False):
                    return False
            except BlockingIOError:
                return True

        if notification_fails:
            marker = comms.bus.bus.path.with_suffix(".changed")
            marker.unlink()
            marker.mkdir()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with comms.goals.todos.writing():
                future = pool.submit(operation.record, comms.goals, todo.id)
                asyncio.run(asyncio.wait_for(wait_for(registry_cut_held), timeout=2))
                assert not future.done()
            if notification_fails:
                with pytest.raises(CommittedMessageError) as failed:
                    future.result(timeout=5)
                changed = comms.goals.todos.get(todo.id)
                assert failed.value.record.seq == before + 1
            else:
                changed = future.result(timeout=5)
        delivery = comms.bus.bus.delivery(before + 1)
        recipient = "renamed" if change == "rename" else "lead"
        assert delivery.message.target == recipient
        assert delivery.message.sender == ("renamed-worker" if change == "rename_holder" else "worker")
        assert [(row.canonical_thread, row.recipient_lookup) for row in delivery.audience.recipients] == [
            (recipient, lead.require_recorded().lookup)]
        assert comms.goals.todos.get(todo.id) == changed
        with comms.bus.bus.reading() as source:
            retained = comms.goals.todos.get(todo.id).order.commander_claims()[0]
            assert retained.original(source) == Message.from_message(delivery.message)
            assert retained.original(source).mentions
    else:
        with pytest.raises(HumanSendRefusedError):
            operation.record(comms.goals, todo.id)
        assert comms.bus.bus.revision() == before
        assert comms.goals.todos.get(todo.id) == todo


def unanswered(comms):
    return UnitAskUnanswered.broken(
        comms, "lead", observed=UnitState.current(comms, "lead", ProgressRead()))


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
    violation, = state_cards(held_bus, Blocked)
    assert violation.orders == ("P1", "P2")
    violation.enforce(held_bus, "lead")
    assert held_bus.registry.require("worker").goal.require() == goal
    assert isinstance(goal.state, ActiveGoal)
    assert store.require_generation(goal.id) == reserved


def publish(comms, sender, target, timestamp, *, answers=()):
    return comms.bus.publisher.publish_ordinary(
        Message(sender=sender, target=target, type=QuestionKind, body="Inspect the pending work",
                timestamp=timestamp, answers=answers), destination=CommandTarget.named(target), origin=AgentOrigin(),
    )


def test_failed_holder_is_reported_without_reissuing_orders_or_replaying_unknown(held_bus):
    blocked_holder(held_bus)
    goal = held_bus.registry.require("worker").goal.require()
    store = held_bus.goals.open_goal_store()
    generation = store.require_generation(goal.id)
    orders = held_bus.goals.todos.held_orders()
    inputs = InputDispositions(held_bus.root)
    unresolved = inputs.lookup("unresolved-input")
    violation, = state_cards(held_bus, Blocked)
    assert violation.orders == ("P1", "P2")
    violation.enforce(held_bus, "lead")
    assert held_bus.registry.require("worker").goal == goal
    assert held_bus.goals.todos.held_orders() == orders
    assert store.require_generation(goal.id) == generation
    assert inputs.lookup("unresolved-input") == unresolved
    assert state_cards(held_bus, Blocked) == (violation,)


def test_an_order_to_a_blocked_goal_activates_it_and_keeps_other_orders(held_bus):
    blocked_holder(held_bus)
    before = held_bus.registry.require("worker").goal.require()
    todo = held_bus.goals.todos.get("P1")
    held_bus.goals.update_goal("worker", ReissuedOrderGoalAction.for_todo(todo, "lead"))
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
                for violation in state_cards(held_bus, Blocked):
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
            generation=todo.held().generation, request=todo.request.require(), review_at=todo.review_at,
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
        assert state_cards(held_bus, Blocked) == ()
    finally:
        held_bus.agents.finish_turn(lease)


def test_unanswered_uses_age_direct_target_and_later_answer_references(held_bus):
    now = time.time()
    publish(held_bus, "worker", "lead", now - 7200)  # before every held order
    publish(held_bus, "worker", "lead", now - AwaitingCommander.seconds + 30)
    publish(held_bus, "outsider", "lead", now - 1800)  # not a holder
    publish(held_bus, "worker", "outsider", now - 1800)  # not its commander
    question = publish(held_bus, "worker", "lead", now - 901)
    # An unrelated later answer and message text do not answer this reference.
    publish(held_bus, "lead", "worker", now - 800)
    violation, = unanswered(held_bus)
    assert violation.message.reference == question.reference
    print(violation.line())
    held_bus.messaging.send("lead", "worker", "Responding", answers=(question.reference,))
    assert unanswered(held_bus) == ()
    another = publish(held_bus, "worker", "lead", now - 950)
    held_bus.messaging.send("lead", "outsider", "Answer in another conversation",
                            answers=(another.reference,))
    assert unanswered(held_bus) == ()


def test_latest_answer_uses_the_complete_original_reference_and_time_cut(held_bus):
    since = time.time() - 60
    question = publish(held_bus, "worker", "lead", since + 1)
    reply = publish(held_bus, "lead", "worker", since + 2, answers=(question.reference,))
    publish(held_bus, "lead", "worker", since + 3)
    publish(held_bus, "lead", "worker", since + 4,
            answers=(MessageReference(question.seq, "wrong-original"),))
    expired = publish(held_bus, "worker", "lead", since - 1)
    publish(held_bus, "lead", "worker", since + 5, answers=(expired.reference,))
    scope = RepliesDisplayScope(frozenset({"lead"}), frozenset({"worker"}))
    assert tuple(message.reference for message in AnswersTo(scope, since).read_opened(
        held_bus.bus.bus)) == (reply.reference,)
    latest = publish(held_bus, "lead", "worker", since + 6, answers=(question.reference,))
    assert tuple(message.reference for message in AnswersTo(scope, since).read_opened(
        held_bus.bus.bus)) == (latest.reference,)
    # Bus order and the producer's timestamp are independent answers.
    backdated = publish(held_bus, "lead", "worker", since + 2.5, answers=(question.reference,))
    unrelated = publish(held_bus, "worker", "outsider", since + 7)
    publish(held_bus, "lead", "worker", since + 8, answers=(unrelated.reference,))
    assert AnswersTo(scope, since).read_opened(held_bus.bus.bus) == (backdated,)
    assert held_bus.bus.bus.answer_timestamp(scope, since=since) == latest.timestamp


def test_unanswered_pages_match_the_complete_answer_reference(held_bus):
    now = time.time()
    questions = tuple(publish(held_bus, "worker", "lead", now - 900 - offset)
                      for offset in range(105))
    publish(held_bus, "lead", "worker", now,
            answers=(MessageReference(questions[0].seq, "not-the-original-hash"),))
    answered = (questions[1].reference, questions[-1].reference)
    held_bus.messaging.send("lead", "outsider", "Answers retain their original references",
                            answers=answered)
    assert tuple(item.message.reference for item in UnitAskUnanswered.violations(held_bus, "lead")) == tuple(
        question.reference for question in reversed(questions) if question.reference not in answered
    )


def test_answering_an_unrelated_peer_does_not_clear_the_commander_burst(held_bus):
    now = time.time()
    owed = publish(held_bus, "worker", "lead", now - 30)
    unrelated = publish(held_bus, "worker", "outsider", now - 20)
    scope = RepliesDisplayScope(frozenset({"lead"}), frozenset({"worker"}))
    assert held_bus.bus.bus.unanswered_page(
        scope, since=now - 60, before=now, upper=unrelated.seq + 1, limit=1,
    ) == (owed,)
    publish(held_bus, "lead", "outsider", now - 10, answers=(unrelated.reference,))
    assert AnswersTo(scope, now - 60).read_opened(held_bus.bus.bus) == ()
    assert UnansweredBurst(scope, now - 60, now).read_opened(held_bus.bus.bus) == (owed,)
    reply = publish(held_bus, "lead", "worker", now - 5, answers=(owed.reference,))
    assert AnswersTo(scope, now - 60).read_opened(held_bus.bus.bus) == (reply,)
    assert UnansweredBurst(scope, now - 60, now).read_opened(held_bus.bus.bus) == ()


@pytest.mark.parametrize("target,body,role,human,eligible", (
    ("lead", "Direct question", ConversationRole(), False, True),
    ("lead", "Direct informational message", NoticeRole(), False, True),
    ("outsider", "Question for another peer", ConversationRole(), False, False),
    ("#comms", "@lead Your question", ConversationRole(), False, True),
    ("#comms", "@outsider Another question", ConversationRole(), False, False),
    ("#comms", "@lead Informational notice", NoticeRole(), False, False),
    ("#comms", "Collective question", ConversationRole(), True, True),
))
def test_latest_answer_retains_the_original_response_eligibility(
    held_bus, target, body, role, human, eligible,
):
    now = time.time()
    user = held_bus.messaging.user_identity(str(held_bus.root)) if human else None
    sender = user.name if user else "worker"
    origin = HumanOrigin.of(user) if user else AgentOrigin()
    original = held_bus.bus.publisher.publish_ordinary(
        Message(sender, target, body, InfoKind, timestamp=now - 20, role=role), destination=CommandTarget.named(target), origin=origin,
    )
    scope = RepliesDisplayScope(frozenset({"lead"}), frozenset({sender}))
    assert scope.includes(original) is eligible
    assert held_bus.bus.bus.unanswered_page(
        scope, since=now - 60, before=now, upper=original.seq + 1, limit=1,
    ) == ((original,) if eligible else ())
    reply = publish(held_bus, "lead", sender, now - 10, answers=(original.reference,))
    found = AnswersTo(scope, now - 60).read_opened(held_bus.bus.bus)
    assert found == ((reply,) if eligible else ())
    assert held_bus.bus.bus.unanswered_page(
        scope, since=now - 60, before=now, upper=reply.seq + 1, limit=1,
    ) == ()


def test_latest_answer_retains_the_original_commander_alias(held_bus):
    now = time.time()
    original = publish(held_bus, "worker", "lead", now - 20)
    held_bus.threads._rename_thread("lead", "renamed-lead")
    reply = publish(held_bus, "renamed-lead", "worker", now - 10, answers=(original.reference,))
    scope = RepliesDisplayScope(
        held_bus.registry.snapshot().names_for("renamed-lead"), frozenset({"worker"}),
    )
    assert AnswersTo(scope, now - 60).read_opened(held_bus.bus.bus) == (reply,)


@pytest.mark.parametrize('target,body', [
    ('lead', 'The worker needs an answer'),
    ('#comms', '@lead The worker needs an answer'),
])
def test_commander_say_answers_an_unanswered_unit_message(held_bus, target, body, tmp_path):
    question = held_bus.bus.publisher.publish_ordinary(
        Message(sender='worker', target=target, type=QuestionKind, body=body,
                timestamp=time.time() - AwaitingCommander.seconds - 1), destination=CommandTarget.named(target), origin=AgentOrigin(),
    )
    ctx = Commander.read_root(held_bus.root)
    reply = tmp_path / 'reply.txt'
    reply.write_text('Responding to the worker with the actual commander command')
    say = SayVerb(units="worker", body_file=str(reply), answers=(question.message_id,))
    say.apply(ctx)
    assert unanswered(held_bus) == ()
    # A name that is not open needs no answer: it is dropped, never a refusal of the verb.
    SayVerb(units="worker", body_file=str(reply), answers=("not-a-message",)).apply(ctx)
    assert held_bus.bus.bus.lookup(held_bus.bus.bus.revision()).answers == ()


def test_an_unanswered_unit_message_gates_send_and_say_until_answered(held_bus, tmp_path):
    question = publish(held_bus, "worker", "lead", time.time() - AwaitingCommander.seconds - 1)
    ctx = Commander.read_root(held_bus.root)
    reply = tmp_path / "reply.txt"
    reply.write_text("Waiting on the merge queue, owner unit-goalblock")
    for verb in (SendVerb(unit="worker", body_file="unused"),
                 SayVerb(units="worker", body_file=str(reply))):
        with pytest.raises(SystemExit, match=question.message_id):
            verb.apply(ctx)
    SayVerb(units="worker", body_file=str(reply), answers=(question.message_id,)).apply(ctx)
    assert unanswered(held_bus) == ()
    # Naming it again after it was answered names nothing open, so it answers nothing.
    SayVerb(units="worker", body_file=str(reply), answers=(question.message_id,)).apply(ctx)
    assert held_bus.bus.bus.lookup(held_bus.bus.bus.revision()).answers == ()


def test_answering_one_owed_message_leaves_another_owed(held_bus, tmp_path):
    first = publish(held_bus, "worker", "lead", time.time() - AwaitingCommander.seconds - 2)
    second = publish(held_bus, "worker", "lead", time.time() - AwaitingCommander.seconds - 1)
    ctx = Commander.read_root(held_bus.root)
    reply = tmp_path / "reply.txt"
    reply.write_text("Waiting on the merge queue, owner unit-goalblock")
    with pytest.raises(SystemExit, match=second.message_id):
        SayVerb(units="worker", body_file=str(reply), answers=(first.message_id,)).apply(ctx)
    assert {owed.message.message_id for owed in unanswered(held_bus)} == {
        first.message_id, second.message_id}
    SayVerb(units="worker", body_file=str(reply),
            answers=(first.message_id, second.message_id)).apply(ctx)
    assert unanswered(held_bus) == ()


def test_an_overdue_owner_directive_gates_until_answered(held_bus, tmp_path):
    directive = held_bus.bus.publisher.publish_ordinary(
        Message(sender="outsider", target="lead", type=InfoKind, body="Ship the install now",
                timestamp=time.time() - 3600), destination=CommandTarget.named("lead"), origin=AgentOrigin(),
    )
    store = held_bus.goals.todos
    selected = NewestOpenRequest().select(store, held_bus.registry.require('lead').incarnation)
    store.record_directive(selected[0].id, RequestDirective(
        directive.seq, directive.timestamp + RequestDirective.window))
    ctx = Commander.read_root(held_bus.root)
    reply = tmp_path / "reply.txt"
    reply.write_text("Installing now")
    with pytest.raises(SystemExit, match=f"DIRECTIVE UNACTED: owner directive {directive.seq}:{directive.message_id}"):
        SayVerb(units="#comms", body_file=str(reply)).apply(ctx)
    SayVerb(units="#comms", body_file=str(reply), answers=(directive.message_id,)).apply(ctx)
    SayVerb(units="#comms", body_file=str(reply)).apply(ctx)
    SayVerb(units="#comms", body_file=str(reply), answers=(directive.message_id,)).apply(ctx)
    assert held_bus.bus.bus.lookup(held_bus.bus.bus.revision()).answers == ()


@pytest.mark.parametrize('body', ['@outsider This question is yours', 'The worker broadcasts its result'])
def test_channel_messages_without_a_commander_addressee_are_not_a_question(held_bus, body):
    held_bus.messaging.send('worker', '#comms', body, type=QuestionKind)
    with held_bus.bus.bus.reading() as source:
        questions = UnansweredUnitAsks.for_orders(
            source, held_bus.registry.snapshot(), 'lead',
            StandingRule.holders(held_bus, 'lead'), time.time(),
        )
    assert not any(questions.values())


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
        run = commander('--config', str(CommanderConfig.path(durable_home)), 'order', *arguments)
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


def test_one_state_per_unit_even_when_blocked_and_detached(held_bus, installed_runtime):
    held_bus.registry.register(held_bus.registry.require("worker"), StoppedThreadStatus())
    detached, = state_cards(held_bus, Detached)
    assert detached.unit.name == "worker" and set(detached.orders) == {"P1", "P2"}
    blocked_holder(held_bus)
    blocked, = state_cards(held_bus, Blocked)
    states = UnitState.current(held_bus, "lead", ProgressRead())
    assert tuple(state for state in states if state.unit == blocked.unit) == (blocked,)
    assert blocked not in StandingRule.broken(held_bus, "lead", observed=states)


def test_startup_heartbeat_enforces_and_publishes_both_new_rule_lines(held_bus, installed_runtime):
    blocked_holder(held_bus)
    question = publish(held_bus, "worker", "lead", time.time() - 901)
    expected = tuple(rule.line() for rule in (
        *state_cards(held_bus, Blocked), *unanswered(held_bus),
    ))
    heartbeat = CommanderHeartbeat()
    commander = held_bus.registry.require("lead")
    held_bus.registry.register(replace(commander, process_identity=ProcessIdentity.capture(os.getpid())))
    heartbeat.pass_body(held_bus, heartbeat.observe(held_bus))
    assert commander.incarnation in heartbeat.reviews
    assert isinstance(held_bus.registry.require("worker").goal.require().state, ActiveGoal)
    page = MessagePageRequest(DMDisplayScope.capture(
        heartbeat.sender, "lead", held_bus.registry.snapshot(),
    )).read_opened(held_bus.bus.bus)
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
    states = UnitState.current(held_bus, "lead", ProgressRead())
    review.wake(held_bus, CommanderHeartbeat.sender, commander, (), states)
    review.wake(held_bus, CommanderHeartbeat.sender, commander, (), states)
    page = MessagePageRequest(DMDisplayScope.capture(
        CommanderHeartbeat.sender, "lead", held_bus.registry.snapshot(),
    )).read_opened(held_bus.bus.bus)
    changed, unchanged = (message for message in page.messages
                          if message.sender == CommanderHeartbeat.sender)
    assert "Standing rules clear." in changed.body
    assert unchanged.body == f"COMMANDER HEARTBEAT: unchanged since seq {changed.seq}, 0 violations."
    assert HeartbeatSilent.violations(held_bus, "lead") == ()
    later = unchanged.timestamp + HeartbeatSilent.seconds + 1
    monkeypatch.setattr(time, "time", lambda: later)
    # Neither a commander reply nor a fresh beat sent to another commander proves
    # this commander's heartbeat is alive.
    held_bus.messaging.send("lead", CommanderHeartbeat.sender, "I read the last beat")
    held_bus.messaging.send(CommanderHeartbeat.sender, "outsider", "Another commander is healthy")
    silent, = HeartbeatSilent.violations(held_bus, "lead")
    assert silent.beat == unchanged.seq
    assert silent.line().startswith(f"HEARTBEAT SILENT: no beat since seq {unchanged.seq}")


def test_first_heartbeat_is_owed_from_the_oldest_open_request_then_survives_renames(
        held_bus, monkeypatch):
    commander = held_bus.registry.require("lead")
    held_bus.registry.register(replace(commander, process_identity=ProcessIdentity.capture(os.getpid())))
    request, = CommanderHeartbeat.requested_by(held_bus.registry.require("lead"), held_bus.goals)
    assert HeartbeatSilent.violations(held_bus, "lead") == ()
    due = request.created_at + HeartbeatSilent.seconds
    monkeypatch.setattr(time, "time", lambda: due + 1)
    never, = HeartbeatSilent.violations(held_bus, "lead")
    assert isinstance(never, HeartbeatNotStarted)
    assert never.request == request.id and never.since == due
    assert "HEARTBEAT NOT STARTED" in never.line()
    assert HeartbeatSilent.broken(held_bus, "lead", observed=()) == (never,)

    # A newer request and mere service registration cannot reset older unpaid service.
    held_bus.goals.todos.add_request(commander.incarnation, "A newer independent request")
    CommanderHeartbeat.register(held_bus.registry)
    assert HeartbeatSilent.violations(held_bus, "lead") == (never,)
    fresh = held_bus.bus.publisher.publish_ordinary(Message(
        sender=CommanderHeartbeat.sender, target="lead", body="The heartbeat is running now",
        type=InfoKind, timestamp=due + 1,
    ), destination=CommandTarget.named("lead"), origin=AgentOrigin())
    assert HeartbeatSilent.violations(held_bus, "lead") == ()

    held_bus.threads._rename_thread("lead", "renamed-lead")
    held_bus.threads._rename_thread(CommanderHeartbeat.sender, "renamed-heartbeat")
    monkeypatch.setattr(time, "time", lambda: fresh.timestamp + HeartbeatSilent.seconds + 1)
    held_bus.messaging.send("renamed-lead", "renamed-heartbeat", "Read the earlier beat")
    silent, = HeartbeatSilent.violations(held_bus, "renamed-lead")
    assert isinstance(silent, MissedHeartbeat) and silent.beat == fresh.seq
    assert silent.since == fresh.timestamp + HeartbeatSilent.seconds
    newer = held_bus.bus.publisher.publish_ordinary(Message(
        sender="renamed-heartbeat", target="renamed-lead", body="Heartbeat after both renames",
        type=InfoKind, timestamp=time.time(),
    ), destination=CommandTarget.named("renamed-lead"), origin=AgentOrigin())
    assert HeartbeatSilent.violations(held_bus, "renamed-lead") == ()
    monkeypatch.setattr(time, "time", lambda: newer.timestamp + HeartbeatSilent.seconds + 1)
    held_bus.messaging.send("renamed-lead", "renamed-heartbeat", "Read the new beat too")
    latest, = HeartbeatSilent.violations(held_bus, "renamed-lead")
    assert isinstance(latest, MissedHeartbeat) and latest.beat == newer.seq


def test_one_answer_covers_an_escalation_burst_sent_before_it(held_bus):
    from agent_comms.commander.vibe_check import VibeCheckSender

    VibeCheckSender.register(held_bus.registry)
    now = time.time()
    burst = [publish(held_bus, VibeCheckSender.sender, "lead", now - 30 + line) for line in range(4)]
    assert len(VibeCheckSender.unanswered(held_bus.bus.bus, held_bus.registry.snapshot(), "lead", time.time())) == 4
    publish(held_bus, "lead", VibeCheckSender.sender, now - 20, answers=(burst[0].reference,))
    assert VibeCheckSender.unanswered(held_bus.bus.bus, held_bus.registry.snapshot(), "lead", time.time()) == ()
    later = publish(held_bus, VibeCheckSender.sender, "lead", now - 10)
    assert [m.message_id for m in VibeCheckSender.unanswered(held_bus.bus.bus, held_bus.registry.snapshot(), "lead", time.time())] == [
        later.message_id]


def test_naming_one_line_of_a_burst_answers_the_burst_at_the_gate(held_bus):
    """The gate reads an answer the way UnansweredBurst reads it once sent: naming any line of
    an escalation burst covers the sender's other lines, while another sender's stay owed."""
    from agent_comms.commander.vibe_check import (
        ChokeholdSender,
        ChokeholdUnanswered,
        VibeCheckSender,
        VibeCheckUnanswered,
    )

    VibeCheckSender.register(held_bus.registry)
    ChokeholdSender.register(held_bus.registry)
    now = time.time() - 3600
    checks = [publish(held_bus, VibeCheckSender.sender, "lead", now + line) for line in range(3)]
    holds = [publish(held_bus, ChokeholdSender.sender, "lead", now + 10 + line) for line in range(3)]
    vibe = [VibeCheckUnanswered(message) for message in checks]
    hold = [ChokeholdUnanswered(message) for message in holds]
    assert all(rule.answered_by((checks[-1],)) for rule in vibe)
    assert not any(rule.answered_by((checks[-1],)) for rule in hold)
    assert all(rule.answered_by((checks[0], holds[1])) for rule in (*vibe, *hold))


def test_a_compliant_answer_resumes_a_held_unit_whose_attempt_was_interrupted(held_bus):
    """A held unit whose last attempt failed refuses a status-only resume; the chokehold's
    release continues its generation the way a fresh order does, so one such unit neither stays
    paused nor stops the release of the others (the heartbeat died on it, 2026-10-21 22:43)."""
    from agent_comms.goal_actions import (
        ActiveGoalAction,
        ChokeholdPausedGoalAction,
        CompliedGoalAction,
        OwnerInvocable,
    )

    blocked_holder(held_bus)
    held_bus.goals.update_goal("worker", ChokeholdPausedGoalAction())
    with pytest.raises(ValueError, match="unresolved"):
        held_bus.goals.update_goal("worker", ActiveGoalAction(), actor=OwnerInvocable)
    held_bus.goals.update_goal(
        "worker", CompliedGoalAction(witness="compliant-answer"), actor=OwnerInvocable)
    assert isinstance(held_bus.registry.require("worker").goal.require().state, ActiveGoal)


def test_a_restarted_heartbeat_does_not_resend_an_escalation_burst(held_bus):
    """Deduplication sees answered and unanswered bursts through the same retained bus."""
    from agent_comms.commander.vibe_check import VibeCheckSender

    first = "first escalation identity"
    VibeCheckSender.burst_once(held_bus, "lead", (first,), first)
    VibeCheckSender.burst_once(held_bus, "lead", (first,), first)
    sent = VibeCheckSender.unanswered(held_bus.bus.bus, held_bus.registry.snapshot(), "lead", time.time())
    assert len(sent) == 1
    publish(held_bus, "lead", VibeCheckSender.sender, time.time(), answers=(sent[0].reference,))
    assert VibeCheckSender.sent(held_bus, "lead", first)
    VibeCheckSender.burst_once(held_bus, "lead", (first,), first)
    assert not VibeCheckSender.unanswered(held_bus.bus.bus, held_bus.registry.snapshot(), "lead", time.time())
    second = "second escalation identity"
    VibeCheckSender.burst_once(held_bus, "lead", (second,), second)
    assert [message.body for message in VibeCheckSender.unanswered(
        held_bus.bus.bus, held_bus.registry.snapshot(), "lead", time.time())] == [second]
