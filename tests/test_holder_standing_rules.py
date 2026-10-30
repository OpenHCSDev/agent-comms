"""Held-order recovery and unanswered DMs on an independently copied bus."""
from pathlib import Path


from agent_comms.repo import Checkout
import asyncio
import json
from contextlib import nullcontext
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial

import pytest

from agent_comms import agent_events as events
from agent_comms.bus_publication import AgentOrigin, HumanOrigin
from agent_comms.channel_targets import CommandTarget, ThreadTarget
from agent_comms.child_process import ProcessIdentity
from agent_comms.commander.order import Commander, OrderFile, SayVerb, SendVerb
from agent_comms.commander.questions import UnansweredUnitAsks
from agent_comms.commander.standing import (
    HeartbeatNotStarted,
    HeartbeatSilent,
    HolderGone,
    MissedHeartbeat,
    UnitAskUnanswered,
)
from agent_comms.commander_heartbeat import CommanderHeartbeat
from agent_comms.commander.vibe_check import ChokeholdUnanswered, VibeCheckUnanswered
from agent_comms.commander_hud import CommanderFrame
from agent_comms.task_attachments.commander_pings import CommanderUpdate
from agent_comms.task_attachments.owner_notices import ChokeholdNotice, VibeCheckNotice
from agent_comms.comms import wire
from agent_comms.coordinator import Coordination
from agent_comms.errors import (
    CommittedMessageError, HumanSendRefusedError, RelationViolationError, TodoConflict, TodoError,
)
from agent_comms.field_codec import FieldCodec, InputId, MessageId, OrderId, RecordProblems, TurnId
from agent_comms.goal_actions import (
    CreateProblemGoalAction,
    ReissuedOrderGoalAction,
    SetGoalAction,
)
from agent_comms.goal_presentation import RunnableExecution
from agent_comms.goal_states import ActiveGoal
from agent_comms.goals import CountCheckResult, OrderCheck
from commander_heartbeat_fixture import capture_frame
from agent_comms.held_orders import HeldOrders
from agent_comms.input_disposition import InputDispositions
from agent_comms.input_key import AcpInputKey
from agent_comms.installer.backup import StoreBackup
from agent_comms.launch_root import LaunchRoot
from agent_comms.message_page import MessagePageRequest
from agent_comms.message_reference import MessageReference
from agent_comms.messages import ConversationRole, InfoKind, Message, NoticeRole, QuestionKind
from agent_comms.native_arguments import GivenBinary
from agent_comms.order_runs import MechanicalOrderRun
from agent_comms.order_story import FleetStory
from agent_comms.order_tools import CommsFleetStoryTool, CommsTodoListTool, TodoList
from agent_comms.owner_requests import NewestOpenRequest, RequestDirective
from agent_comms.read_basis import DMDisplayScope, RepliesDisplayScope
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.service_passes import ServicePassStore
from agent_comms.standing_rules import OwedAnswer, StandingRule
from agent_comms.store_files import ExclusiveLock, StoreLockContention, _store_lock
from agent_comms.thread_identity import CoreWatcherIncarnation
from agent_comms.thread_model import DeclaredModel
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.threads import Thread
from agent_comms.todos import (
    ClaimOrder,
    CorrectMergedLinkTodo,
    MergedLink,
    PullRequestLink,
    ResumeClaimOrder,
    ExpectedProblem,
    OrderClaim,
    OrderMessage,
    PendingProblemOrder,
    ProgressRead,
)
from agent_comms.tool_caller import NoThreadCaller
from agent_comms.turn_digests import OrderEditCountRead
from agent_comms.ui_model.unit_state import AwaitingCommander, Blocked, Detached, UnitState
from cli_command import commander
from conftest import declare_test_bus
from native_backend_fixture import native_backend_fixture, wait_for
from test_goal_execution import turn_on


UNRESOLVED_INPUT_KEY = AcpInputKey(InputId.mint())

def board_read(tool, comms, caller, thread="lead"):
    """A board tool's reply names whose board it read; the rest is that board's record."""
    reply = tool.invoke(comms, {"thread": thread}, caller=caller)
    assert reply.pop("board") == thread
    return reply



@pytest.fixture
def held_bus(comms, tmp_path, source_repository, configured_repository, installed_runtime):
    """One setup owner: real declarations, checked orders, UNKNOWN row, then a copy.

    Its standing rules read the installed build, so the held bus owns an installed runtime."""
    source = source_repository
    scope = source.scope()
    # Two order holders never share one worktree, so the outsider has its own checkout.
    outsider = tmp_path / 'outsider-checkout'
    source.git(source.checkout, 'worktree', 'add', '-q', '-b', 'outsider-work', str(outsider))
    declare_test_bus(comms, channels=('comms',), threads=tuple(
        Thread(name=name, tags=frozenset({'comms'}), worktree=Checkout(worktree))
        for name, worktree in (('lead', source.checkout), ('worker', source.checkout),
                               ('outsider', outsider))
    ))
    request = comms.goals.todos.add_request(
        comms.registry.require('lead').incarnation, 'Repair the held work').id
    now = time.time()
    # One order covers both problems: Core refuses a unit a second, unrelated order.
    order = PendingProblemOrder(
        owner=scope.shape.parents[0], scope=scope, run=MechanicalOrderRun(),
        check=OrderCheck(source.checkout, "printf 0", CountCheckResult(1)),
        edits=("Repair the existing owner",), banned=("No second store",),
        report="Report the result", at=RecordedEventTime(now - 3600),
        first_call_deadline=RecordedEventTime(now + 3600),
    )
    for problem in ("P1", "P2"):
        comms.goals.update_goal("worker", CreateProblemGoalAction(
            problem=problem, commander="lead", request=request,
            assigner=comms.registry.require('lead').incarnation,
            repo=RepositoryName.from_wire("OpenHCSDev/agent-comms"), text=f"Repair {problem}",
            review_at=RecordedEventTime(now + 3600), order=order,
        ))
    inputs = InputDispositions(comms.root)
    inputs.record(UNRESOLVED_INPUT_KEY, seq=None, owner="worker", claim_scope=1,
                  target="worker", text="Do not replay this input")
    assert inputs.bind(
        UNRESOLVED_INPUT_KEY,
        claim_scope=1,
        turn_id=TurnId('310c74d7ac243239a928d4021c709acb'),
        native_id=InputId.mint(),
        text="Do not replay this input",
    )
    configured_repository(comms)
    copied = tmp_path / "copied-bus"
    StoreBackup.copy_tree(comms.root, copied)
    copied_bus = wire(copied)
    configured_repository(copied_bus)
    return copied_bus


def test_stop_rejects_a_human_before_any_registry_change(comms, tmp_path):
    reader = comms.messaging.user_identity(Checkout(tmp_path))
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
            todo.id, OrderMessage(todo.revision, lead, original.message_id, at=RecordedEventTime(time.time())))
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
    frame = capture_frame(comms, "lead", ProgressRead())
    return tuple(state for state in UnitState.current(comms, "lead", frame)
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
        todo.id, OrderMessage(todo.revision, lead, original.message_id, at=RecordedEventTime(time.time())))
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
            comms.registry.declare(Thread("lead", frozenset({"comms"}), worktree))
    before = comms.bus.bus.revision()
    operation = ClaimOrder(todo.revision, worker, todo.held().generation, claim)
    if change in ("rename", "rename_holder"):
        def registry_cut_held():
            try:
                with _store_lock(comms.registry.store.path, ExclusiveLock(), StoreLockContention(0)):
                    return False
            except BlockingIOError:
                return True

        if notification_fails:
            marker, = comms.bus.bus.source_paths(comms.bus.bus.path.parent)
            marker.unlink()
            marker.mkdir()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with comms.goals.todos.writing():
                future = pool.submit(operation.record, comms.goals, todo.id)
                asyncio.run(asyncio.wait_for(wait_for(registry_cut_held), timeout=2))
                assert not future.done()
            if notification_fails:
                # The bus committed the record; only its completion failed. That is never an
                # unsent claim, and resuming returns the committed original's sequence.
                with pytest.raises(CommittedMessageError) as failed:
                    future.result(timeout=5)
                changed = comms.goals.todos.get(todo.id)
                assert failed.value.record.seq == before + 1
                marker.rmdir()
                marker.touch()
                retained, = changed.order.commander_claims()
                ResumeClaimOrder(changed.revision, worker, changed.held().generation,
                                 retained).record(comms.goals, todo.id)
                assert comms.bus.bus.revision() == before + 1
                assert comms.goals.todos.get(todo.id) == changed
            else:
                changed = future.result(timeout=5)
        delivery = comms.bus.bus.delivery(before + 1)
        recipient = "renamed" if change == "rename" else "lead"
        assert delivery.message.target == recipient
        assert delivery.message.sender == ("renamed-worker" if change == "rename_holder" else "worker")
        assert [(row.canonical_thread, row.recipient_lookup) for row in delivery.recipients] == [
            (recipient, lead.require_recorded().lookup)]
        assert comms.goals.todos.get(todo.id) == changed
        retained = comms.goals.todos.get(todo.id).order.commander_claims()[0]
        # The retained claim is the publication's content; the bus adds its sequence and the
        # mentions it resolved.
        assert retained.message.publication_snapshot == delivery.message.publication_snapshot
        assert delivery.message.mentions
    else:
        with pytest.raises(HumanSendRefusedError):
            operation.record(comms.goals, todo.id)
        assert comms.bus.bus.revision() == before
        assert comms.goals.todos.get(todo.id) == todo


def unanswered(comms):
    frame = capture_frame(comms, "lead", ProgressRead())
    return UnitAskUnanswered.broken(
        comms, "lead", observed=UnitState.current(comms, "lead", frame), frame=frame)


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


def test_holds_whose_holder_was_deleted_or_registered_again_are_reported_by_every_reader(
    held_bus,
):
    """One registry split owns orphaned holds: the heartbeat's standing read names each one
    with what clears it, and the fleet story, todo list and edit counts read the same split."""
    comms = held_bus
    original = comms.goals.todos.get("P1")
    comms.goals.update_goal("outsider", CreateProblemGoalAction(
        problem="P3", commander="lead", request=original.request,
        assigner=comms.registry.require("lead").incarnation, repo=original.repo,
        text="Repair P3", review_at=original.review_at,
        order=replace(original.order, id=OrderId.mint()),
    ))
    merged = comms.goals.todos.get("P2")
    with comms.goals.todos.writing() as db:
        replace(merged, order=merged.order.merged("b" * 40), revision=merged.revision + 1).upsert(db)
    worker = comms.registry.require("worker")
    for name in ("worker", "outsider"):
        comms.registry.unregister(name)
        comms.registry.remove(name)
    comms.registry.declare(Thread("worker", frozenset({"comms"}), worker.worktree))

    frame = capture_frame(comms, "lead", ProgressRead())
    broken = StandingRule.broken(
        comms, "lead", observed=UnitState.current(comms, "lead", frame), frame=frame)
    gone = {rule.todo.id: rule.line() for rule in broken if isinstance(rule, HolderGone)}
    assert set(gone) == {"P1", "P2", "P3"}
    assert "order withdraw P1" in gone["P1"] and "order close P2" in gone["P2"]
    assert {rule.todo.id for rule in HolderGone.violations(comms, "lead")} == set(gone)
    assert frame.todos == ()
    fleet, = FieldCodec.decode(
        FleetStory, board_read(CommsFleetStoryTool, comms, NoThreadCaller())).lines
    count, named = fleet.split(": ")
    assert count == "3 orders held by units no longer registered"
    assert set(named.split(", ")) == {"P1 worker", "P2 worker", "P3 outsider"}
    listed = FieldCodec.decode(TodoList, board_read(CommsTodoListTool, comms, NoThreadCaller()))
    assert {"P1", "P2", "P3"} <= {todo.id for todo in listed.todos} and listed.progress == {}
    assert tuple(OrderEditCountRead.for_commander(comms, "lead")) == ()
    with pytest.raises(ValueError, match="pass --artifact FILE"):
        comms.goals.todos.get("P2").order.closing_artifact()


def order_file(comms, tmp_path):
    """A well-formed order for the worker, so a send reaches the answer gate."""
    order = replace(comms.goals.todos.get("P1").order.checked(), id=OrderId.mint())
    document = OrderFile(order, "The worker's next step waits on the commander's answer.",
                         "This is stupid because an owed answer stalls the worker.")
    path = tmp_path / "order.json"
    path.write_text(json.dumps(FieldCodec.encode(document)))
    return str(path)


def publish(comms, sender, target, timestamp, *, answers=()):
    return comms.bus.publisher.publish_ordinary(
        Message(sender=sender, target=target, type=QuestionKind, body="Inspect the pending work",
                timestamp=timestamp, answers=answers), destination=CommandTarget.named(target), origin=AgentOrigin(),
    )


def escalate(comms, notice, timestamp, body="Answer the escalation"):
    """One escalation line in the owner's voice, at a chosen time."""
    user = comms.messaging.user_identity(Checkout(comms.root))
    return comms.bus.publisher.publish_ordinary(
        Message(user.name, "lead", body, InfoKind, timestamp=timestamp,
                task=notice.of(comms.registry.snapshot(), "lead")),
        destination=ThreadTarget("lead"), origin=HumanOrigin.of(user),
    )


def test_an_order_rule_observation_compares_its_cited_repositories(held_bus):
    """An order's scope cites FileLines whose repository is a declared family: the
    observation key compares it, so a rule holding the order keys without crashing."""
    first, second = (held_bus.goals.todos.get(problem) for problem in ("P1", "P2"))
    assert HolderGone(first).observation_key() == HolderGone(
        held_bus.goals.todos.get("P1")).observation_key()
    assert HolderGone(first).observation_key() != HolderGone(second).observation_key()


def test_failed_holder_is_reported_without_reissuing_orders_or_replaying_unknown(held_bus):
    blocked_holder(held_bus)
    goal = held_bus.registry.require("worker").goal.require()
    store = held_bus.goals.open_goal_store()
    generation = store.require_generation(goal.id)
    orders = held_bus.goals.todos.held_orders()
    inputs = InputDispositions(held_bus.root)
    unresolved = inputs.lookup(UNRESOLVED_INPUT_KEY)
    violation, = state_cards(held_bus, Blocked)
    assert violation.orders == ("P1", "P2")
    violation.enforce(held_bus, "lead")
    assert held_bus.registry.require("worker").goal == goal
    assert held_bus.goals.todos.held_orders() == orders
    assert store.require_generation(goal.id) == generation
    assert inputs.lookup(UNRESOLVED_INPUT_KEY) == unresolved
    assert state_cards(held_bus, Blocked) == (violation,)


def test_an_order_to_a_blocked_goal_activates_it_and_keeps_its_group(held_bus):
    blocked_holder(held_bus)
    before = held_bus.registry.require("worker").goal.require()
    todo = held_bus.goals.todos.get("P1")
    other = held_bus.goals.todos.get("P2")
    # P2 shares P1's order, so the reissue carries it: a unit holds one order at a time.
    held_bus.goals.update_goal("worker", replace(
        ReissuedOrderGoalAction.for_todo(todo, "lead"),
        together=(ExpectedProblem(other.id, other.revision),)))
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
        unresolved = InputDispositions(held_bus.root).lookup(UNRESOLVED_INPUT_KEY)
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
                assert InputDispositions(held_bus.root).lookup(UNRESOLVED_INPUT_KEY) == unresolved
            finally:
                if held_bus.registry.require("worker").process_alive:
                    await Coordination.run_worker(partial(held_bus.owners.stop, "worker"))


def test_goal_generation_cannot_transfer_to_a_different_owner(held_bus):
    todo = held_bus.goals.todos.get("P1")
    with pytest.raises(TodoConflict):
        held_bus.goals.todos.assign(
            (ExpectedProblem(todo.id, todo.revision),),
            owner=held_bus.registry.require("outsider"), assigner=CoreWatcherIncarnation.watcher(),
            generation=todo.held().generation, request=todo.request.require(), review_at=todo.review_at,
            order=todo.order.checked().reissued(time.time()),
        )
    assert held_bus.goals.todos.get(todo.id) == todo


def test_merge_through_a_prerequisite_link_is_taken_back_and_the_order_changes_owner(held_bus):
    """P935: a prerequisite PR linked to the order merged it; only that link leaves, the
    order is pending again under its own id, check, run and times, and then changes owner."""
    store = held_bus.goals.todos
    lead = held_bus.registry.require("lead").incarnation
    held = store.get("P1")
    prerequisite = PullRequestLink(held.repo, 3596)
    implementation = PullRequestLink(held.repo, 3700)
    with store.writing() as connection:
        replace(held, revision=held.revision + 1, order=replace(
            held.order.checked(), pull_requests=(prerequisite, implementation),
        ).merged("33bd05c64abc")).upsert(connection)
    merged = store.get("P1")
    reason = "the merged prerequisite pull request held none of this order's checker work"

    def correction(revision: int, previous: MergedLink) -> CorrectMergedLinkTodo:
        return CorrectMergedLinkTodo(revision, lead, reason, previous=previous)

    for refused in (correction(held.revision, MergedLink(prerequisite, "33bd05c64abc")),
                    correction(merged.revision, MergedLink(PullRequestLink(held.repo, 1),
                                                           "33bd05c64abc")),
                    correction(merged.revision, MergedLink(prerequisite, "0123456789ab"))):
        with pytest.raises(TodoError):
            store.apply("P1", refused)
        assert store.get("P1") == merged
    with pytest.raises(TodoError, match="records no merge"):
        correction(held.revision, MergedLink(prerequisite, "33bd05c64abc")).apply(held)

    store.apply("P1", correction(merged.revision, MergedLink(prerequisite, "33bd05c64abc")))
    corrected = store.get("P1")
    assert isinstance(corrected.order, PendingProblemOrder)
    assert corrected.order.pull_requests == (implementation,)
    assert corrected.order.corrections[-1].previous == MergedLink(prerequisite, "33bd05c64abc")
    assert (replace(corrected.order, pull_requests=(), corrections=())
            == replace(held.order, pull_requests=(), corrections=()))
    assert (corrected.holder, corrected.review_at) == (merged.holder, merged.review_at)

    held_bus.goals.update_goal("outsider", SetGoalAction(text="Absorb the checker audit work."))
    goals = {name: held_bus.registry.require(name).goal for name in ("worker", "outsider")}
    outsider = held_bus.registry.require("outsider")
    store.assign((ExpectedProblem("P1", corrected.revision),), owner=outsider, assigner=lead,
                 generation=goals["outsider"].require().id, request=corrected.request.require(),
                 review_at=corrected.review_at, order=corrected.order)
    assigned = store.get("P1")
    assert assigned.held().owner_identity == outsider.incarnation
    assert assigned.order == corrected.order
    assert {name: held_bus.registry.require(name).goal for name in goals} == goals


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


def test_unanswered_pages_match_the_complete_answer_reference(held_bus):
    now = time.time()
    questions = tuple(publish(held_bus, "worker", "lead", now - 900 - offset)
                      for offset in range(105))
    with pytest.raises(RelationViolationError):
        publish(held_bus, "lead", "worker", now,
                answers=(MessageReference(
                    questions[0].seq, MessageId.mint("not-the-original-hash")),))
    answered = (questions[1].reference, questions[-1].reference)
    held_bus.messaging.send("lead", "outsider", "Answers retain their original references",
                            answers=answered)
    assert tuple(item.message.reference for item in UnitAskUnanswered.violations(held_bus, "lead")) == tuple(
        question.reference for question in reversed(questions) if question.reference not in answered
    )


def test_answering_an_unrelated_peer_leaves_the_commander_question_open(held_bus):
    now = time.time()
    owed = publish(held_bus, "worker", "lead", now - 30)
    unrelated = publish(held_bus, "worker", "outsider", now - 20)
    scope = RepliesDisplayScope(frozenset({"lead"}), frozenset({"worker"}))
    assert held_bus.bus.bus.unanswered_page(
        scope, since=now - 60, before=now, upper=unrelated.seq + 1, limit=1,
    ) == (owed,)
    publish(held_bus, "lead", "outsider", now - 10, answers=(unrelated.reference,))
    assert held_bus.bus.bus.unanswered_page(
        scope, since=now - 60, before=now, upper=unrelated.seq + 20, limit=1,
    ) == (owed,)


@pytest.mark.parametrize("target,body,role,human,eligible", (
    ("lead", "Direct question", ConversationRole(), False, True),
    ("lead", "Direct informational message", NoticeRole(), False, True),
    ("outsider", "Question for another peer", ConversationRole(), False, False),
    ("#comms", "@lead Your question", ConversationRole(), False, True),
    ("#comms", "@outsider Another question", ConversationRole(), False, False),
    ("#comms", "@lead Informational notice", NoticeRole(), False, False),
    ("#comms", "Collective question", ConversationRole(), True, True),
))
def test_unanswered_page_retains_the_original_response_eligibility(
    held_bus, target, body, role, human, eligible,
):
    now = time.time()
    user = held_bus.messaging.user_identity(Checkout(held_bus.root)) if human else None
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
    assert held_bus.bus.bus.unanswered_page(
        scope, since=now - 60, before=now, upper=reply.seq + 1, limit=1,
    ) == ()


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
    # A nonexistent identity refuses without publishing a reply that drops its answer.
    unknown = MessageId("0" * 12)
    revision = held_bus.bus.bus.revision()
    with pytest.raises(RecordProblems, match=unknown):
        SayVerb(units="worker", body_file=str(reply), answers=(unknown,)).apply(ctx)
    assert held_bus.bus.bus.revision() == revision
    # An already answered original exists, but opens no new ask.
    SayVerb(units="worker", body_file=str(reply), answers=(question.message_id,)).apply(ctx)
    assert held_bus.bus.bus.lookup(held_bus.bus.bus.revision()).answers == ()


def test_an_unanswered_unit_message_gates_send_until_a_say_answers_it(held_bus, tmp_path):
    question = publish(held_bus, "worker", "lead", time.time() - AwaitingCommander.seconds - 1)
    ctx = Commander.read_root(held_bus.root)
    reply = tmp_path / "reply.txt"
    reply.write_text("Waiting on the merge queue, owner unit-goalblock")
    with pytest.raises(RecordProblems, match=question.message_id):
        SendVerb(unit="worker", body_file=order_file(held_bus, tmp_path)).apply(ctx)
    # A reply to the asker answers its open asks without naming them.
    SayVerb(units="worker", body_file=str(reply)).apply(ctx)
    assert held_bus.bus.bus.lookup(held_bus.bus.bus.revision()).answers == (question.reference,)
    assert unanswered(held_bus) == ()
    # Naming it again after it was answered names nothing open, so it answers nothing.
    SayVerb(units="worker", body_file=str(reply), answers=(question.message_id,)).apply(ctx)
    assert held_bus.bus.bus.lookup(held_bus.bus.bus.revision()).answers == ()


def test_answering_one_owed_message_leaves_another_owed(held_bus, tmp_path, capsys):
    """An answer is never refused for the debts it leaves: they warn and stay owed."""
    first = publish(held_bus, "worker", "lead", time.time() - AwaitingCommander.seconds - 2)
    second = publish(held_bus, "worker", "lead", time.time() - AwaitingCommander.seconds - 1)
    ctx = Commander.read_root(held_bus.root)
    reply = tmp_path / "reply.txt"
    reply.write_text("Waiting on the merge queue, owner unit-goalblock")
    capsys.readouterr()
    SayVerb(units="worker", body_file=str(reply), answers=(first.message_id,)).apply(ctx)
    out = capsys.readouterr().out
    assert f"{second.seq}:{second.message_id}" in out
    assert f"{first.seq}:{first.message_id}" not in out
    assert held_bus.bus.bus.lookup(held_bus.bus.bus.revision()).answers == (first.reference,)
    assert [owed.message.message_id for owed in unanswered(held_bus)] == [second.message_id]
    with pytest.raises(RecordProblems, match=second.message_id):
        SendVerb(unit="worker", body_file=order_file(held_bus, tmp_path)).apply(ctx)


def test_a_refusal_lists_the_asks_still_inside_their_answer_window(held_bus, tmp_path):
    """A retry costs a call, so the refusal names the asks that fall due before it lands too."""
    owed = publish(held_bus, "worker", "lead", time.time() - AwaitingCommander.seconds - 1)
    ripening = publish(held_bus, "worker", "lead", time.time() - 60)
    ctx = Commander.read_root(held_bus.root)
    with pytest.raises(RecordProblems) as refused:
        SendVerb(unit="worker", body_file=order_file(held_bus, tmp_path)).apply(ctx)
    assert {owed.message_id, ripening.message_id} <= set(
        re.search(r"order say worker BODYFILE --answers (.*)", str(refused.value)).group(1).split())


def test_an_owed_answer_is_a_heartbeat_line_and_never_a_vibe_check(held_bus):
    """The board already states an owed answer and locks the verbs on it; a vibe check saying it
    again on a second channel restated one fact, so no degraded sign carries it."""
    from agent_comms.commander.vibe_check import VibeCheck

    ask = publish(held_bus, "worker", "lead", time.time() - AwaitingCommander.seconds - 1)
    frame = capture_frame(held_bus, "lead", ProgressRead())
    assert [rule.message.message_id for rule in OwedAnswer.scan_observed(
        held_bus, "lead", (), frame)] == [ask.message_id]
    assert VibeCheck.scan_observed(held_bus, "lead", (), frame) == ()


def test_an_owed_vibe_check_refuses_only_verbs_that_answer_nothing(held_bus, tmp_path):
    """The vibe check waits like any owed message: a reply naming a unit's ask still lands."""
    check = escalate(held_bus, VibeCheckNotice, time.time() - 3600)
    ask = publish(held_bus, "worker", "lead", time.time() - AwaitingCommander.seconds - 1)
    ctx = Commander.read_root(held_bus.root)
    reply = tmp_path / "reply.txt"
    reply.write_text("Waiting on the merge queue, owner unit-goalblock")
    with pytest.raises(RecordProblems, match=check.message_id):
        SendVerb(unit="worker", body_file=order_file(held_bus, tmp_path)).apply(ctx)
    SayVerb(units="worker", body_file=str(reply), answers=(ask.message_id,)).apply(ctx)
    assert held_bus.bus.bus.lookup(held_bus.bus.bus.revision()).answers == (ask.reference,)


def test_an_answer_sent_to_another_unit_is_refused_and_leaves_the_ask_owed(held_bus, tmp_path):
    question = publish(held_bus, "worker", "lead", time.time() - AwaitingCommander.seconds - 1)
    ctx = Commander.read_root(held_bus.root)
    reply = tmp_path / "reply.txt"
    reply.write_text("Waiting on the merge queue, owner unit-goalblock")
    revision = held_bus.bus.bus.revision()
    with pytest.raises(RecordProblems, match=(f"{question.message_id} was asked by worker; "
                                          "answer it in a message to worker")):
        SayVerb(units="outsider", body_file=str(reply), answers=(question.message_id,)).apply(ctx)
    assert held_bus.bus.bus.revision() == revision
    assert [owed.message.message_id for owed in unanswered(held_bus)] == [question.message_id]


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
    with pytest.raises(RecordProblems, match=re.escape(
        f"ACT owner directive {directive.seq}:{directive.message_id}"
    )):
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
            HeldOrders.read(held_bus, 'lead').holders, time.time(),
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
    configured_repository(held_bus, source)
    worktrees = held_bus.registry.storage_rule.checkout_base
    original = held_bus.goals.todos.get('P1')
    check = OrderCheck(source.checkout, 'grep -c obsolete owner.py', CountCheckResult(0))
    order = replace(original.order.checked(), id=OrderId.mint(), check=check,
                    banned=('optional guards', 'maybe.py'), report='Report optional examples')
    document = OrderFile(order, 'Four obsolete declarations remain after their reader was deleted.',
                         'This is stupid because dead declarations still occupy the source.',
                         unplanned_reason='Repair the order tool before returning to the planned work.',
                         problems=())
    order_file = tmp_path / 'order.json'
    order_file.write_text(json.dumps(FieldCodec.encode(document)))

    def cli(*arguments):
        run = commander('--root', str(held_bus.root), 'order', *arguments)
        assert run.returncode == 0, run.stdout + run.stderr
        print(run.stdout)

    # The idle outsider takes it: the worker already holds P1 and P2, and the allocation
    # policy refuses a third order to a holder while a running unit holds none.
    # Problem creates the first order through Commander.send, including its message link.
    cli('problem', str(order_file), '--to', 'outsider', '--where', 'owner.py:2')
    first = held_bus.goals.todos.get('P3')
    assert first.latest_message.message_id
    document = replace(document, problems=('P3',),
                       order=replace(document.order, id=OrderId.mint()))
    order_file.write_text(json.dumps(FieldCodec.encode(document)))
    cli('send', 'outsider', str(order_file), '--clean')
    sent = held_bus.goals.todos.get('P3')
    assert sent.order.check == check
    corrected = 'grep -c "^obsolete" owner.py'
    cli('check', 'P3', '--check', f'{source.checkout} :: {corrected} -> 0', '--why',
        'The original count also included comments rather than only declarations')
    current = held_bus.goals.todos.get('P3')
    assert current.order.id == sent.order.id
    assert current.review_at == sent.review_at and current.strike_count == sent.strike_count
    assert current.order.check.command == corrected
    assert current.order.check.result == CountCheckResult(0)
    # Done's result rule remains the inverse: the delivered commit must meet the target.
    current.order.check.result.require(current.order.check.at(
        source.branch_commit, source.checkout, worktrees))
    with pytest.raises(ValueError):
        current.order.check.result.require(current.order.check.at(main, source.checkout, worktrees))
    assert source.git(source.checkout, 'rev-parse', 'HEAD') == head
    assert local.read_bytes() == before
    assert not tuple(worktrees.iterdir())
    print('Deletion expected 0, remote main reads 4: send and check accepted; stale dirty tree unchanged.')


def test_one_state_per_unit_even_when_blocked_and_detached(held_bus):
    held_bus.registry.register(held_bus.registry.require("worker"), StoppedThreadStatus())
    detached, = state_cards(held_bus, Detached)
    assert detached.unit.name == "worker" and set(detached.orders) == {"P1", "P2"}
    blocked_holder(held_bus)
    blocked, = state_cards(held_bus, Blocked)
    frame = capture_frame(held_bus, "lead", ProgressRead())
    states = UnitState.current(held_bus, "lead", frame)
    assert tuple(state for state in states if state.unit == blocked.unit) == (blocked,)
    assert blocked not in StandingRule.broken(
        held_bus, "lead", observed=states, frame=frame)


def test_startup_heartbeat_enforces_and_publishes_both_new_rule_lines(held_bus):
    blocked_holder(held_bus)
    question = publish(held_bus, "worker", "lead", time.time() - 901)
    expected = tuple(rule.line() for rule in (
        *state_cards(held_bus, Blocked), *unanswered(held_bus),
    ))
    heartbeat = CommanderHeartbeat()
    commander = held_bus.registry.require("lead")
    held_bus.registry.register(replace(commander, process_identity=ProcessIdentity.capture(os.getpid())))
    asyncio.run(heartbeat.run(lambda: nullcontext(held_bus)))
    saved, = CommanderUpdate.latest(held_bus, commander.incarnation)
    assert saved.handled
    assert isinstance(held_bus.registry.require("worker").goal.require().state, ActiveGoal)
    owner = held_bus.messaging.user_identity(Checkout(held_bus.root)).name
    page = MessagePageRequest(DMDisplayScope.capture(
        owner, "lead", held_bus.registry.snapshot(),
    )).read_opened(held_bus.bus.bus)
    message = next(message for message in page.messages if message.task == saved)
    # The compact body truncates lines; the original board entries keep every complete alert.
    board = tuple(entry.text for entry in message.task.board(held_bus, "lead"))
    assert set(expected) <= set(board)
    assert any(question.reference.message_id in text for text in board)
    print(message.body)


def test_successful_pass_health_does_not_depend_on_messages(held_bus, monkeypatch):
    commander = held_bus.registry.require("lead")
    held_bus.registry.register(replace(commander, process_identity=ProcessIdentity.capture(os.getpid())))
    passes = ServicePassStore.at(held_bus.root)
    passes.passed(CommanderHeartbeat.__name__)
    assert not passes.read().commander_completed
    worker = held_bus.registry.require("worker").incarnation
    observed = (commander.incarnation, worker)
    passes.heartbeat_passed((worker,), observed)
    passes.heartbeat_passed((commander.incarnation,), observed)
    completed = passes.read().commander_completed[commander.incarnation]
    assert HeartbeatSilent.violations(held_bus, "lead") == ()
    before = held_bus.bus.bus.revision()
    later = completed + HeartbeatSilent.seconds + 1
    monkeypatch.setattr(time, "time", lambda: later)
    silent, = HeartbeatSilent.violations(held_bus, "lead")
    assert isinstance(silent, MissedHeartbeat)
    assert silent.since == completed + HeartbeatSilent.seconds
    # Another commander's pass cannot clear this alarm.
    passes.heartbeat_passed((worker,), observed)
    assert HeartbeatSilent.violations(held_bus, "lead") == (silent,)
    # A successful quiet pass clears the alarm without inventing a conversation.
    passes.heartbeat_passed((commander.incarnation,), observed)
    assert HeartbeatSilent.violations(held_bus, "lead") == ()
    assert held_bus.bus.bus.revision() == before


def test_pass_missing_a_restarting_commander_keeps_its_completion(held_bus):
    """An install leaves the commander's process dead for one pass, which retires nothing."""
    commander = held_bus.registry.require("lead")
    alive = replace(commander, process_identity=ProcessIdentity.capture(os.getpid()))
    heartbeat = CommanderHeartbeat()
    held_bus.registry.register(alive)
    asyncio.run(heartbeat.run(lambda: nullcontext(held_bus)))
    passes = ServicePassStore.at(held_bus.root)
    completed = passes.read().commander_completed[commander.incarnation]
    held_bus.registry.register(commander)
    asyncio.run(heartbeat.run(lambda: nullcontext(held_bus)))
    assert CommanderFrame.read_all(held_bus.root) == ()
    held_bus.registry.register(alive)
    assert passes.read().commander_completed[commander.incarnation] == completed
    assert HeartbeatSilent.violations(held_bus, "lead") == ()


def test_first_pass_is_owed_from_oldest_request_and_survives_renames(held_bus, monkeypatch):
    commander = held_bus.registry.require("lead")
    held_bus.registry.register(replace(commander, process_identity=ProcessIdentity.capture(os.getpid())))
    request, = CommanderHeartbeat.requested_by(held_bus.registry.require("lead"), held_bus.goals)
    due = request.created_at + HeartbeatSilent.seconds
    monkeypatch.setattr(time, "time", lambda: due + 1)
    never, = HeartbeatSilent.violations(held_bus, "lead")
    assert isinstance(never, HeartbeatNotStarted)
    assert never.request == request.id and never.since == due
    held_bus.goals.todos.add_request(commander.incarnation, "A newer independent request")
    held_bus.messaging.send_user_message(
        ThreadTarget("lead"), "A DM is not a completed pass", worktree=Checkout(held_bus.root))
    assert HeartbeatSilent.violations(held_bus, "lead") == (never,)
    passes = ServicePassStore.at(held_bus.root)
    observed = (commander.incarnation,)
    passes.heartbeat_passed(observed, observed)
    completed = passes.read().commander_completed[commander.incarnation]
    assert HeartbeatSilent.violations(held_bus, "lead") == ()
    held_bus.threads._rename_thread("lead", "renamed-lead")
    monkeypatch.setattr(time, "time", lambda: completed + HeartbeatSilent.seconds + 1)
    silent, = HeartbeatSilent.violations(held_bus, "renamed-lead")
    assert isinstance(silent, MissedHeartbeat)
    assert silent.since == completed + HeartbeatSilent.seconds
    renamed = (held_bus.registry.require("renamed-lead").incarnation,)
    passes.heartbeat_passed(renamed, renamed)
    assert HeartbeatSilent.violations(held_bus, "renamed-lead") == ()


def test_one_answer_covers_an_escalation_burst_sent_before_it(held_bus):
    now = time.time()
    burst = [escalate(held_bus, VibeCheckNotice, now - 30 + line) for line in range(4)]
    owner = burst[0].sender
    assert len(VibeCheckNotice.unanswered(held_bus.bus.bus, held_bus.registry.snapshot(), "lead", time.time())) == 4
    publish(held_bus, "lead", owner, now - 20, answers=(burst[0].reference,))
    assert VibeCheckNotice.unanswered(held_bus.bus.bus, held_bus.registry.snapshot(), "lead", time.time()) == ()
    later = escalate(held_bus, VibeCheckNotice, now - 10)
    assert [m.message_id for m in VibeCheckNotice.unanswered(held_bus.bus.bus, held_bus.registry.snapshot(), "lead", time.time())] == [
        later.message_id]


def test_an_answer_covers_by_bus_order_whatever_its_clock_says(held_bus):
    """Answered is decided by bus sequence alone: a reply the bus ordered after the alarm covers
    it even when the replier's clock ran behind, and a line the bus ordered after the reply stays
    owed even when its clock ran behind the reply's."""
    now = time.time()
    check = escalate(held_bus, VibeCheckNotice, now - 10)
    reply = publish(held_bus, "lead", check.sender, now - 20, answers=(check.reference,))
    assert reply.seq == check.seq + 1 and reply.timestamp < check.timestamp
    for beat in (time.time(), time.time() + VibeCheckUnanswered.seconds):
        assert VibeCheckNotice.unanswered(held_bus.bus.bus, held_bus.registry.snapshot(), "lead", beat) == ()
    later = escalate(held_bus, VibeCheckNotice, now - 30)
    assert [m.reference for m in VibeCheckNotice.unanswered(
        held_bus.bus.bus, held_bus.registry.snapshot(), "lead", time.time())] == [later.reference]


def test_answering_the_owner_does_not_answer_his_escalation(held_bus):
    """Escalations share the owner's thread with everything he types; only an answer naming a
    line of the burst covers it, and an escalation never becomes his request or his words."""
    now = time.time()
    check = escalate(held_bus, VibeCheckNotice, now - 30)
    typed = held_bus.messaging.send_user_message(
        ThreadTarget("lead"), "Something I typed", worktree=Checkout(held_bus.root))
    publish(held_bus, "lead", typed.sender, now - 10, answers=(typed.reference,))
    assert [m.reference for m in VibeCheckNotice.unanswered(
        held_bus.bus.bus, held_bus.registry.snapshot(), "lead", time.time())] == [check.reference]
    assert tuple(check.retained_task_facts()) == ()
    assert tuple(typed.retained_task_facts()) != ()


def test_naming_one_line_of_a_burst_answers_the_burst_at_the_gate(held_bus):
    """The gate reads an answer the way unanswered reads it once sent: naming any line of
    an escalation burst covers its other lines, while another burst's stay owed."""
    now = time.time() - 3600
    checks = [escalate(held_bus, VibeCheckNotice, now + line) for line in range(3)]
    holds = [escalate(held_bus, ChokeholdNotice, now + 10 + line) for line in range(3)]
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
        "worker", CompliedGoalAction(witness=MessageId("c0ffee000001")), actor=OwnerInvocable)
    assert isinstance(held_bus.registry.require("worker").goal.require().state, ActiveGoal)


def test_a_compliant_answer_resumes_a_held_unit_whose_problems_closed_during_the_hold(held_bus):
    """The heartbeat died on every pass from 13:08 on 2026-10-24: a unit whose problems were
    withdrawn while the chokehold held it refused the release, so no unit after it resumed."""
    from agent_comms.goal_actions import (
        ChokeholdPausedGoalAction,
        CompliedGoalAction,
        OwnerInvocable,
    )
    from agent_comms.todos import ReleaseTodo, StrikeReason, WithdrawTodo

    held_bus.goals.update_goal("worker", ChokeholdPausedGoalAction())
    lead = held_bus.registry.require("lead").incarnation
    worker = held_bus.registry.require("worker")
    todos = held_bus.goals.todos
    for todo in todos.for_goal(worker.incarnation, worker.goal.require().id):
        released = todos.apply(todo.id, ReleaseTodo(todo.revision, lead, todo.held(), StrikeReason(
            "The commander moved this assigned repair elsewhere while the unit was held.")))
        todos.apply(todo.id, WithdrawTodo(
            released.revision, lead, released.holder_state.previous_assignment(),
            "The requested repair already merged into main through another problem."))
    assert todos.for_goal(worker.incarnation, worker.goal.require().id) == ()
    held_bus.goals.update_goal(
        "worker", CompliedGoalAction(witness=MessageId("c0ffee000001")), actor=OwnerInvocable)
    assert isinstance(held_bus.registry.require("worker").goal.require().state, ActiveGoal)


def test_a_restarted_heartbeat_does_not_resend_an_escalation_burst(held_bus):
    """Deduplication sees answered and unanswered bursts through the same retained bus."""
    first = "first escalation identity"
    VibeCheckNotice.burst_once(held_bus, "lead", (first,), first)
    VibeCheckNotice.burst_once(held_bus, "lead", (first,), first)
    sent = VibeCheckNotice.unanswered(held_bus.bus.bus, held_bus.registry.snapshot(), "lead", time.time())
    assert len(sent) == 1
    publish(held_bus, "lead", sent[0].sender, time.time(), answers=(sent[0].reference,))
    VibeCheckNotice.burst_once(held_bus, "lead", (first,), first)
    assert not VibeCheckNotice.unanswered(held_bus.bus.bus, held_bus.registry.snapshot(), "lead", time.time())
    second = "second escalation identity"
    VibeCheckNotice.burst_once(held_bus, "lead", (second,), second)
    assert [message.body for message in VibeCheckNotice.unanswered(
        held_bus.bus.bus, held_bus.registry.snapshot(), "lead", time.time())] == [second]
