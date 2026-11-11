"""Held-order recovery and unanswered DMs on an independently copied bus."""
import asyncio
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from functools import partial
from pathlib import Path

import pytest

from agent_comms import agent_events as events
from agent_comms.comms import wire
from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.service_passes import ServicePassStore
from agent_comms.errors import (
    CommittedMessageError,
    HumanSendRefusedError,
    RelationViolationError,
    TodoError,
)
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.input_key import AcpInputKey
from agent_comms.execution.launch_root import LaunchRoot
from agent_comms.execution.native_arguments import GivenBinary
from agent_comms.execution.turn_digests import OrderEditCountRead
from agent_comms.input_disposition import InputDispositions
from agent_comms.installer.backup import StoreBackup
from agent_comms.messaging.bus_publication import AgentOrigin
from agent_comms.messaging.channel_targets import (
    CommandTarget,
    CommsTag,
    TeammatesTag,
    ThreadTarget,
)
from agent_comms.messaging.initial_origin import HumanOrigin
from agent_comms.messaging.message_page import MessagePageRequest
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.messages import (
    ConversationRole,
    InfoKind,
    Message,
    NoticeRole,
    QuestionKind,
)
from agent_comms.messaging.read_basis import DMDisplayScope, RepliesDisplayScope
from agent_comms.messaging.sending import TeamReceipts
from agent_comms.nominal.field_codec import (
    FieldCodec,
    GoalId,
    InputId,
    MessageId,
    OrderId,
    RecordProblems,
    TurnId,
)
from agent_comms.planning.goal_actions import (
    CreateProblemGoalAction,
    ReissuedOrderGoalAction,
    SetGoalAction,
)
from agent_comms.planning.goal_presentation import RunnableExecution
from agent_comms.planning.goal_states import ActiveGoal
from agent_comms.planning.goals import CountCheckResult, OrderCheck, ProblemGoalOrder
from agent_comms.planning.held_orders import HeldOrders
from agent_comms.planning.order_runs import MechanicalOrderRun
from agent_comms.planning.order_story import TeamStory
from agent_comms.planning.order_words import StrikeReason
from agent_comms.planning.orders import (
    ClaimOrder,
    CorrectLinkTodo,
    CorrectMergedLinkTodo,
    DoneTodoState,
    DraftListedPullRequest,
    OrderClaim,
    OrderMessage,
    PendingProblemOrder,
    ProgressRead,
    ReleaseTodo,
    ResumeClaimOrder,
    TodoConflict,
)
from agent_comms.planning.owner_requests import NewestOpenRequest, RequestDirective
from agent_comms.planning.problem_findings import ExpectedProblem
from agent_comms.planning.pull_requests import MergedLink, PullRequestLink
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.registry.heartbeat_delivery import OnHeartbeatDelivery
from agent_comms.registry.thread_model import DeclaredModel
from agent_comms.registry.thread_status import StoppedThreadStatus
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.recorded_time import RecordedEventTime
from agent_comms.storage.store_files import ExclusiveLock, StoreLockContention, _store_lock
from agent_comms.task_attachments.owner_notices import ChokeholdNotice, VibeCheckNotice
from agent_comms.task_attachments.team_pings import TeamUpdate
from agent_comms.team.order import ListedProblems, OrderFile, SayVerb, SendVerb, Team
from agent_comms.team.questions import UnansweredTeammateAsks
from agent_comms.team.standing import (
    HeartbeatNotStarted,
    HeartbeatSilent,
    HolderGone,
    MissedHeartbeat,
    UnitAskUnanswered,
)
from agent_comms.team.standing_rules import OwedAnswer, StandingRule
from agent_comms.team.team_heartbeat import TeamHeartbeat
from agent_comms.team.team_hud import TeamFrame
from agent_comms.team.vibe_check import ChokeholdUnanswered, VibeCheckUnanswered
from agent_comms.transport.connector import ConnectorSendTool
from agent_comms.transport.connector_caller import DirectorCaller
from agent_comms.transport.tool_caller import ForkCaller, NoThreadCaller, PeerCaller, ThreadCaller
from agent_comms.transport.tools.orders import CommsTeamStoryTool, CommsTodoListTool, TodoList
from agent_comms.ui_model.teammate_state import AwaitingTeam, Blocked, Detached, TeammateState
from cli_command import team
from conftest import declare_test_bus, team_as
from goal_owner_fixture import GoalTurnOwners
from native_backend_fixture import native_backend_fixture, wait_for
from team_heartbeat_fixture import capture_frame

UNRESOLVED_INPUT_KEY = AcpInputKey(InputId.mint())

def board_read(tool, comms, caller, thread="colleague"):
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
        NativeThread(name=name, tags=frozenset({'comms'}), worktree=Checkout(worktree))
        for name, worktree in (('colleague', source.checkout), ('worker', source.checkout),
                               ('outsider', outsider))
    ))
    request = comms.goals.todos.add_request(
        comms.registry.require('colleague').incarnation, 'Repair the held work').id
    now = time.time()
    # One order covers both problems: Core refuses a holder a second, unrelated order.
    order = PendingProblemOrder(
        owner=scope.shape.parents[0], scope=scope, run=MechanicalOrderRun(),
        check=OrderCheck(source.checkout, "printf 0", CountCheckResult(1)),
        edits=("Repair the existing owner",), banned=("No second store",),
        report="Report the result", at=RecordedEventTime(now - 3600),
        first_call_deadline=RecordedEventTime(now + 3600),
    )
    generation = GoalId.mint()
    colleague = comms.registry.require("colleague")
    worker = comms.registry.require("worker")
    for problem in ("P1", "P2"):
        comms.goals.todos.create(
            problem, RepositoryName.from_wire("OpenHCSDev/agent-comms"), f"Repair {problem}",
            creator=colleague, owner=worker, assigner=colleague.incarnation, generation=generation,
            request=request, review_at=RecordedEventTime(now + 3600), order=order,
        )
    comms.goals.update_goal("worker", SetGoalAction(
        text=ProblemGoalOrder.text, order=ProblemGoalOrder(), goal_id=generation,
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
def claim_order(held_bus, github_cli):
    """Publish the real order and claim messages, using their retained identities."""
    def claim(problem, *, pull_request=1, head="a" * 40):
        todo = held_bus.goals.todos.get(problem)
        colleague = held_bus.registry.require("colleague").incarnation
        worker = held_bus.registry.require("worker").incarnation
        original = held_bus.messaging.send("colleague", "worker", todo.order.instructions(todo)).message
        todo = held_bus.goals.todos.apply(
            todo.id, OrderMessage(todo.revision, colleague, original.message_id, at=RecordedEventTime(time.time())))
        github_cli.put(todo.repo, DraftListedPullRequest(
            number=pull_request, branch="repair", head=head, base="main",
            updated=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            reviewable_times=(),
        ))
        claimed = OrderClaim.for_order(todo, worker, PullRequestLink(todo.repo, pull_request), head,
                                      "Review the published repair", (original.reference,))
        claimed = replace(claimed, message=replace(
            claimed.message, timestamp=time.time() - AwaitingTeam.seconds - 1))
        ClaimOrder(todo.revision, worker, todo.held().generation, claimed).record(
            held_bus.goals, todo.id)
        return claimed
    return claim


def state_cards(comms, member):
    frame = capture_frame(comms, ProgressRead())
    return tuple(state for state in TeammateState.current(comms, frame)
                 if isinstance(state, member))


@pytest.mark.parametrize("change,notification_fails", [
    ("rename", False), ("rename_holder", False), ("remove", False), ("recreate", False),
    ("rename", True),
])
def test_order_claim_publishes_only_to_its_retained_team_birth(
    held_bus, change, notification_fails,
):
    comms = held_bus
    todo = comms.goals.todos.get("P1")
    colleague = comms.registry.require("colleague").incarnation
    worker = comms.registry.require("worker").incarnation
    original = comms.messaging.send("colleague", "worker", todo.order.instructions(todo)).message
    todo = comms.goals.todos.apply(
        todo.id, OrderMessage(todo.revision, colleague, original.message_id, at=RecordedEventTime(time.time())))
    claim = OrderClaim.for_order(
        todo, worker, PullRequestLink(todo.repo, 1), "a" * 40,
        "Review this published repair with @outsider", (original.reference,))
    if change == "rename":
        comms.registry.rename("colleague", "renamed")
    elif change == "rename_holder":
        comms.registry.rename("worker", "renamed-worker")
    else:
        worktree = comms.registry.require("colleague").worktree
        comms.registry.unregister("colleague")
        comms.registry.remove("colleague")
        if change == "recreate":
            comms.registry.declare(NativeThread("colleague", frozenset({"comms"}), worktree))
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
                retained, = changed.order.team_claims()
                ResumeClaimOrder(changed.revision, worker, changed.held().generation,
                                 retained).record(comms.goals, todo.id)
                assert comms.bus.bus.revision() == before + 1
                assert comms.goals.todos.get(todo.id) == changed
            else:
                changed = future.result(timeout=5)
        delivery = comms.bus.bus.delivery(before + 1)
        recipient = "renamed" if change == "rename" else "colleague"
        assert delivery.message.target == recipient
        assert delivery.message.sender == ("renamed-worker" if change == "rename_holder" else "worker")
        assert [(row.canonical_thread, row.recipient_lookup) for row in delivery.recipients] == [
            (recipient, colleague.require_recorded().lookup)]
        assert comms.goals.todos.get(todo.id) == changed
        retained = comms.goals.todos.get(todo.id).order.team_claims()[0]
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
    frame = capture_frame(comms, ProgressRead())
    return UnitAskUnanswered.broken(
        comms, observed=TeammateState.current(comms, frame), frame=frame)


async def blocked_holder(comms, owners: GoalTurnOwners):
    """Settle a real failed turn; keep its owner alive for the test's observations."""
    owner = comms.registry.require("worker")
    comms.registry.register(replace(owner, process_identity=ProcessIdentity.capture(os.getpid())))
    turns = await owners.for_bus(comms)
    async with turns(comms) as turn:
        permit, = turn.goal_permit.claims
        await turn.progress.done(events.Done("The previous attempt failed", False))
    return permit.reservation


def test_an_active_goal_with_an_unresolved_attempt_is_reported_never_retried(held_bus):
    goal = held_bus.registry.require("worker").goal.require()
    store = held_bus.goals.open_goal_store()
    generation = store.require_generation(goal.id)
    store.recover_unreserved_ready(goal.id, generation.number)
    store.reserve(goal.id, generation.number, ready_grant=store.ready_grant(goal.id, generation.number))
    reserved = store.require_generation(goal.id)
    violation, = state_cards(held_bus, Blocked)
    assert violation.orders == ("P1", "P2")
    violation.enforce(held_bus)
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
        problem="P3", request=original.request,
        assigner=comms.registry.require("colleague").incarnation, repo=original.repo,
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
    comms.registry.declare(NativeThread("worker", frozenset({"comms"}), worker.worktree))

    frame = capture_frame(comms, ProgressRead())
    broken = StandingRule.broken(
        comms, observed=TeammateState.current(comms, frame), frame=frame)
    gone = {rule.todo.id: rule.line() for rule in broken if isinstance(rule, HolderGone)}
    assert set(gone) == {"P1", "P2", "P3"}
    assert "order withdraw P1" in gone["P1"] and "order close P2" in gone["P2"]
    assert {rule.todo.id for rule in HolderGone.violations(comms)} == set(gone)
    assert frame.todos == ()
    fleet, = FieldCodec.decode(
        TeamStory, CommsTeamStoryTool.invoke(comms, {}, caller=NoThreadCaller())).lines
    count, named = fleet.split(": ")
    assert count == "3 orders held by teammates no longer registered"
    assert set(named.split(", ")) == {"P1 worker", "P2 worker", "P3 outsider"}
    listed = FieldCodec.decode(TodoList, board_read(CommsTodoListTool, comms, NoThreadCaller()))
    assert {"P1", "P2", "P3"} <= {todo.id for todo in listed.todos} and listed.progress == {}
    assert tuple(OrderEditCountRead.for_team(comms.goals, "colleague")) == ()
    with pytest.raises(ValueError, match="pass --artifact FILE"):
        comms.goals.todos.get("P2").order.closing_artifact()


def order_file(comms, tmp_path):
    """A well-formed order for the worker, so a send reaches the answer gate."""
    order = replace(comms.goals.todos.get("P1").order.checked(), id=OrderId.mint())
    document = OrderFile(order, "The worker's next step waits on the teammate's answer.",
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
        Message(user.name, "colleague", body, InfoKind, timestamp=timestamp,
                task=notice.of(comms.registry.snapshot(), "colleague")),
        destination=ThreadTarget("colleague"), origin=HumanOrigin.of(user),
    )


def test_an_order_rule_observation_compares_its_cited_repositories(held_bus):
    """An order's scope cites FileLines whose repository is a declared family: the
    observation key compares it, so a rule holding the order keys without crashing."""
    first, second = (held_bus.goals.todos.get(problem) for problem in ("P1", "P2"))
    assert HolderGone(first).observation_key() == HolderGone(
        held_bus.goals.todos.get("P1")).observation_key()
    assert HolderGone(first).observation_key() != HolderGone(second).observation_key()


@pytest.mark.asyncio
async def test_failed_holder_is_reported_without_reissuing_orders_or_replaying_unknown(
    held_bus, goal_turn_owners,
):
    await blocked_holder(held_bus, goal_turn_owners)
    goal = held_bus.registry.require("worker").goal.require()
    store = held_bus.goals.open_goal_store()
    generation = store.require_generation(goal.id)
    orders = held_bus.goals.todos.held_orders()
    inputs = InputDispositions(held_bus.root)
    unresolved = inputs.lookup(UNRESOLVED_INPUT_KEY)
    violation, = state_cards(held_bus, Blocked)
    assert violation.orders == ("P1", "P2")
    violation.enforce(held_bus)
    assert held_bus.registry.require("worker").goal == goal
    assert held_bus.goals.todos.held_orders() == orders
    assert store.require_generation(goal.id) == generation
    assert inputs.lookup(UNRESOLVED_INPUT_KEY) == unresolved
    assert state_cards(held_bus, Blocked) == (violation,)


@pytest.mark.asyncio
async def test_an_owner_approved_order_to_a_blocked_goal_activates_it_and_keeps_its_group(
    held_bus, goal_turn_owners,
):
    await blocked_holder(held_bus, goal_turn_owners)
    before = held_bus.registry.require("worker").goal.require()
    todo = held_bus.goals.todos.get("P1")
    other = held_bus.goals.todos.get("P2")
    # P2 shares P1's order, so the reissue carries it: a holder holds one order at a time.
    held_bus.goals.update_goal("worker", replace(
        ReissuedOrderGoalAction.for_todo(todo),
        assigner=held_bus.registry.require("worker").acting_identity,
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
                held_bus.owners.start_many, ("worker",), agent_bin=GivenBinary("pi")))
            try:
                await wait_for(lambda: fixture.provider.posts == 1)
                assert held_bus.registry.require("worker").executing
                await Coordination.run_worker(partial(held_bus.owners.stop, "worker"))
                assert held_bus.registry.require("worker").goal == goal
                assert isinstance(held_bus.goals.goal_execution("worker"), RunnableExecution)
                for violation in state_cards(held_bus, Blocked):
                    violation.enforce(held_bus)
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
            owner=held_bus.registry.require("outsider"),
            assigner=held_bus.registry.require("worker").acting_identity,
            generation=todo.held().generation, request=todo.request.require(), review_at=todo.review_at,
            order=todo.order.checked().reissued(time.time()),
        )
    assert held_bus.goals.todos.get(todo.id) == todo


def test_merge_through_a_prerequisite_link_is_taken_back_and_the_order_changes_owner(held_bus):
    """P935: a prerequisite PR linked to the order merged it; only that link leaves, the
    order is pending again under its own id, check, run and times, and then changes owner."""
    store = held_bus.goals.todos
    colleague = held_bus.registry.require("colleague").incarnation
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
        return CorrectMergedLinkTodo(revision, colleague, reason, previous=previous)

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
    corrected = store.apply("P1", ReleaseTodo(
        corrected.revision, held_bus.registry.require("worker").acting_identity,
        corrected.held(), StrikeReason("The holder releases the corrected repair for its next owner.")))
    store.assign((ExpectedProblem("P1", corrected.revision),), owner=outsider,
                 assigner=outsider.acting_identity,
                 generation=goals["outsider"].require().id, request=corrected.request.require(),
                 review_at=corrected.review_at, order=corrected.order)
    assigned = store.get("P1")
    assert assigned.held().owner_identity == outsider.incarnation
    assert assigned.order == corrected.order
    assert {name: held_bus.registry.require(name).goal for name in goals} == goals


def test_unlink_takes_another_orders_pull_request_off_a_held_order(held_bus, durable_home):
    """P904: a claim recorded another order's PR on a held order. `order unlink` corrects the
    order in place while its holder still holds it; only that link leaves, and a later send
    carries only the links the corrected order keeps."""
    store = held_bus.goals.todos
    held = store.get("P1")
    foreign = PullRequestLink(held.repo, 3566)
    own = PullRequestLink(RepositoryName.from_wire("OpenHCSDev/toad"), 1319)
    with store.writing() as connection:
        replace(held, revision=held.revision + 1, order=replace(
            held.order.checked(), pull_requests=(foreign, own))).upsert(connection)
    linked = store.get("P1")

    def unlink(*arguments: str):
        return team('--root', str(held_bus.root), 'order', 'unlink', 'P1', *arguments,
                    '--why', 'a claim recorded P883 work on this order', '--as', 'colleague')

    refused = unlink('openhcsdev/agent-comms', '1')
    assert refused.returncode != 0 and 'does not link' in refused.stdout + refused.stderr
    assert store.get("P1") == linked
    run = unlink('openhcsdev/agent-comms', '3566')
    assert run.returncode == 0, run.stdout + run.stderr
    corrected = store.get("P1")
    assert corrected.order.pull_requests == (own,)
    assert corrected.order.corrections[-1] == CorrectLinkTodo(
        linked.revision, held_bus.registry.require("colleague").incarnation,
        "a claim recorded P883 work on this order", previous=(foreign, own), link=foreign,
        at=corrected.order.corrections[-1].at)
    assert (replace(corrected.order, pull_requests=(), corrections=())
            == replace(linked.order, pull_requests=(), corrections=()))
    assert ((corrected.holder, corrected.review_at, corrected.strike_count)
            == (linked.holder, linked.review_at, linked.strike_count))
    resent = replace(corrected.order, pull_requests=())
    assert ListedProblems().carried(resent, ["P1"], {"P1": corrected}).pull_requests == (own,)


def test_unanswered_uses_age_direct_target_and_later_answer_references(held_bus):
    now = time.time()
    publish(held_bus, "worker", "colleague", now - 7200)  # before every held order
    publish(held_bus, "worker", "colleague", now - AwaitingTeam.seconds + 30)
    publish(held_bus, "outsider", "colleague", now - 1800)  # not a holder
    publish(held_bus, "worker", "outsider", now - 1800)  # not its colleague
    question = publish(held_bus, "worker", "colleague", now - 901)
    # An unrelated later answer and message text do not answer this reference.
    publish(held_bus, "colleague", "worker", now - 800)
    violation, = unanswered(held_bus)
    assert violation.message.reference == question.reference
    held_bus.messaging.send("colleague", "worker", "Responding", answers=(question.reference,))
    assert unanswered(held_bus) == ()
    another = publish(held_bus, "worker", "colleague", now - 950)
    held_bus.messaging.send("colleague", "outsider", "Answer in another conversation",
                            answers=(another.reference,))
    assert unanswered(held_bus) == ()


def test_unanswered_pages_match_the_complete_answer_reference(held_bus):
    now = time.time()
    questions = tuple(publish(held_bus, "worker", "colleague", now - 900 - offset)
                      for offset in range(105))
    with pytest.raises(RelationViolationError):
        publish(held_bus, "colleague", "worker", now,
                answers=(MessageReference(
                    questions[0].seq, MessageId.mint("not-the-original-hash")),))
    answered = (questions[1].reference, questions[-1].reference)
    held_bus.messaging.send("colleague", "outsider", "Answers retain their original references",
                            answers=answered)
    assert tuple(item.message.reference for item in UnitAskUnanswered.violations(held_bus)) == tuple(
        question.reference for question in reversed(questions) if question.reference not in answered
    )


def test_an_unanswered_ask_is_a_row_on_its_debtors_tool_calls(held_bus):
    """The teammate an ask was sent to owes it: the row names that teammate, and the same
    ask owed by two teammates is two rows, so neither debtor's row is deduplicated away."""
    asked = publish(held_bus, "worker", "colleague", time.time() - 900)
    owed, = (rule for rule in UnitAskUnanswered.violations(held_bus)
             if rule.message.reference == asked.reference)
    colleague = held_bus.registry.require("colleague").incarnation
    assert owed.debtor == colleague and colleague in owed.named_threads()
    other = replace(owed, debtor=held_bus.registry.require("outsider").incarnation)
    assert other.observation_key() != owed.observation_key()


def test_answering_an_unrelated_peer_leaves_the_team_question_open(held_bus):
    now = time.time()
    owed = publish(held_bus, "worker", "colleague", now - 30)
    unrelated = publish(held_bus, "worker", "outsider", now - 20)
    scope = RepliesDisplayScope(frozenset({"colleague"}), frozenset({"worker"}))
    assert held_bus.bus.bus.unanswered_page(
        scope, since=now - 60, before=now, upper=unrelated.seq + 1, limit=1,
    ) == (owed,)
    publish(held_bus, "colleague", "outsider", now - 10, answers=(unrelated.reference,))
    assert held_bus.bus.bus.unanswered_page(
        scope, since=now - 60, before=now, upper=unrelated.seq + 20, limit=1,
    ) == (owed,)


@pytest.mark.parametrize("target,body,role,human,eligible", (
    ("colleague", "Direct question", ConversationRole(), False, True),
    ("colleague", "Direct informational message", NoticeRole(), False, True),
    ("outsider", "Question for another peer", ConversationRole(), False, False),
    ("#comms", "@colleague Your question", ConversationRole(), False, True),
    ("#comms", "@outsider Another question", ConversationRole(), False, False),
    ("#comms", "@colleague Informational notice", NoticeRole(), False, False),
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
    scope = RepliesDisplayScope(frozenset({"colleague"}), frozenset({sender}))
    assert scope.includes(original) is eligible
    assert held_bus.bus.bus.unanswered_page(
        scope, since=now - 60, before=now, upper=original.seq + 1, limit=1,
    ) == ((original,) if eligible else ())
    reply = publish(held_bus, "colleague", sender, now - 10, answers=(original.reference,))
    assert held_bus.bus.bus.unanswered_page(
        scope, since=now - 60, before=now, upper=reply.seq + 1, limit=1,
    ) == ()


@pytest.mark.parametrize('target,body', [
    ('colleague', 'The worker needs an answer'),
    ('#comms', '@colleague The worker needs an answer'),
])
def test_team_say_answers_an_unanswered_teammate_message(held_bus, target, body, tmp_path):
    question = held_bus.bus.publisher.publish_ordinary(
        Message(sender='worker', target=target, type=QuestionKind, body=body,
                timestamp=time.time() - AwaitingTeam.seconds - 1), destination=CommandTarget.named(target), origin=AgentOrigin(),
    )
    ctx = team_as(held_bus.root, "colleague")
    reply = tmp_path / 'reply.txt'
    reply.write_text('Responding to the worker with the actual teammate command')
    say = SayVerb(units="worker", body_file=str(reply), answers=(question.message_id,))
    say.apply(ctx)
    assert unanswered(held_bus) == ()
    # A nonexistent identity refuses without publishing a reply that drops its answer.
    unknown = MessageId("0" * 12)
    revision = held_bus.bus.bus.revision()
    with pytest.raises(RecordProblems, match=unknown):
        SayVerb(units="worker", body_file=str(reply), answers=(unknown,)).apply(ctx)
    assert held_bus.bus.bus.revision() == revision
    # An already answered original opens no new ask, so naming it refuses without publishing.
    revision = held_bus.bus.bus.revision()
    with pytest.raises(RecordProblems, match="not an open ask"):
        SayVerb(units="worker", body_file=str(reply), answers=(question.message_id,)).apply(ctx)
    assert held_bus.bus.bus.revision() == revision


def test_an_unanswered_teammate_message_gates_send_until_a_say_answers_it(held_bus, tmp_path):
    question = publish(held_bus, "worker", "colleague", time.time() - AwaitingTeam.seconds - 1)
    ctx = team_as(held_bus.root, "colleague")
    reply = tmp_path / "reply.txt"
    reply.write_text("Waiting on the merge queue, owner unit-goalblock")
    with pytest.raises(RecordProblems, match=question.message_id):
        SendVerb(unit="worker", body_file=order_file(held_bus, tmp_path)).apply(ctx)
    # A reply to the asker answers its open asks without naming them.
    SayVerb(units="worker", body_file=str(reply)).apply(ctx)
    assert held_bus.bus.bus.lookup(held_bus.bus.bus.revision()).answers == (question.reference,)
    assert unanswered(held_bus) == ()
    # Naming it again after it was answered names nothing open, so it refuses without publishing.
    revision = held_bus.bus.bus.revision()
    with pytest.raises(RecordProblems, match="not an open ask"):
        SayVerb(units="worker", body_file=str(reply), answers=(question.message_id,)).apply(ctx)
    assert held_bus.bus.bus.revision() == revision


def test_an_order_to_an_archived_thread_records_nothing(held_bus, tmp_path):
    held_bus.registry.archive("outsider")
    ctx = team_as(held_bus.root, "colleague")
    before = {todo.id for todo in ctx.todos()}
    order = replace(held_bus.goals.todos.get("P1").order.checked(), id=OrderId.mint())
    document = OrderFile(order, "An order for a thread nobody can start.",
                         "This is stupid because nothing can ever carry it out.")
    with pytest.raises(RelationViolationError, match="outsider cannot take an order"):
        ctx.send("outsider", document, request=NewestOpenRequest())
    assert {todo.id for todo in ctx.todos()} == before


def test_withdrawing_from_an_archived_holder_releases_the_order_in_the_channel(held_bus):
    held_bus.registry.archive("worker")
    team_as(held_bus.root, "colleague").withdraw(
        "P1", "This order went to a thread that was archived and can never start it.")
    assert held_bus.goals.todos.get("P1").state is DoneTodoState
    posted = held_bus.bus.bus.lookup(held_bus.bus.bus.revision())
    assert "Stop that work" in posted.body and posted.target == CommsTag.channel()


def test_answering_one_owed_message_leaves_another_owed(held_bus, tmp_path, capsys):
    """An answer is never refused for the debts it leaves: they warn and stay owed."""
    first = publish(held_bus, "worker", "colleague", time.time() - AwaitingTeam.seconds - 2)
    second = publish(held_bus, "worker", "colleague", time.time() - AwaitingTeam.seconds - 1)
    ctx = team_as(held_bus.root, "colleague")
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
    owed = publish(held_bus, "worker", "colleague", time.time() - AwaitingTeam.seconds - 1)
    ripening = publish(held_bus, "worker", "colleague", time.time() - 60)
    ctx = team_as(held_bus.root, "colleague")
    with pytest.raises(RecordProblems) as refused:
        SendVerb(unit="worker", body_file=order_file(held_bus, tmp_path)).apply(ctx)
    assert {owed.message_id, ripening.message_id} <= set(
        re.search(r"order say worker BODYFILE --answers (.*)", str(refused.value)).group(1).split())


def test_an_owed_answer_is_a_heartbeat_line_and_never_a_vibe_check(held_bus):
    """The board already states an owed answer and locks the verbs on it; a vibe check saying it
    again on a second channel restated one fact, so no degraded sign carries it."""
    from agent_comms.team.vibe_check import VibeCheck

    ask = publish(held_bus, "worker", "colleague", time.time() - AwaitingTeam.seconds - 1)
    frame = capture_frame(held_bus, ProgressRead())
    assert [rule.message.message_id for rule in OwedAnswer.scan_observed(
        held_bus, (), frame)] == [ask.message_id]
    assert VibeCheck.scan_observed(held_bus, (), frame) == ()


def test_an_owed_vibe_check_refuses_only_verbs_that_answer_nothing(held_bus, tmp_path):
    """The vibe check waits like any owed message: a reply naming a teammate's ask still lands."""
    check = escalate(held_bus, VibeCheckNotice, time.time() - 3600)
    ask = publish(held_bus, "worker", "colleague", time.time() - AwaitingTeam.seconds - 1)
    ctx = team_as(held_bus.root, "colleague")
    reply = tmp_path / "reply.txt"
    reply.write_text("Waiting on the merge queue, owner unit-goalblock")
    with pytest.raises(RecordProblems, match=check.message_id):
        SendVerb(unit="worker", body_file=order_file(held_bus, tmp_path)).apply(ctx)
    SayVerb(units="worker", body_file=str(reply), answers=(ask.message_id,)).apply(ctx)
    assert held_bus.bus.bus.lookup(held_bus.bus.bus.revision()).answers == (ask.reference,)


def test_only_the_addressed_teammates_own_answer_clears_its_vibe_check(held_bus):
    """No seat: a vibe check is one teammate's. A peer's reply naming it, quote or not, leaves it
    owed (it could otherwise release that teammate's chokehold); the addressed teammate's own
    reply clears it."""
    check = escalate(held_bus, VibeCheckNotice, time.time() - 3600)
    held_bus.registry.declare(NativeThread("peer", frozenset({TeammatesTag.declared_name}),
                                     Checkout(Path.cwd())), StoppedThreadStatus())
    peer, colleague = (held_bus.registry.require(name) for name in ("peer", "colleague"))
    held_bus.messaging.send("peer", "colleague", "Answered.", answers=(check.reference,),
                            answerer=TeamReceipts(peer.acting_identity,
                                                       (peer.incarnation, colleague.incarnation)))
    with held_bus.bus.bus.reading() as source:
        assert [message.reference for message in VibeCheckNotice.unanswered(
            source, held_bus.registry.snapshot(), "colleague", time.time())] == [check.reference]
    held_bus.messaging.send("colleague", "peer", "Mine, answered.", answers=(check.reference,))
    with held_bus.bus.bus.reading() as source:
        assert VibeCheckNotice.unanswered(
            source, held_bus.registry.snapshot(), "colleague", time.time()) == ()


def test_an_answer_sent_to_another_teammate_is_refused_and_leaves_the_ask_owed(held_bus, tmp_path):
    question = publish(held_bus, "worker", "colleague", time.time() - AwaitingTeam.seconds - 1)
    ctx = team_as(held_bus.root, "colleague")
    reply = tmp_path / "reply.txt"
    reply.write_text("Waiting on the merge queue, owner unit-goalblock")
    revision = held_bus.bus.bus.revision()
    with pytest.raises(RecordProblems, match=(f"{question.message_id} was asked by worker; "
                                          "answer it in a message to worker")):
        SayVerb(units="outsider", body_file=str(reply), answers=(question.message_id,)).apply(ctx)
    assert held_bus.bus.bus.revision() == revision
    assert [owed.message.message_id for owed in unanswered(held_bus)] == [question.message_id]


def test_peer_verbs_owe_own_mail_not_the_selected_storage_leads_mail(held_bus, tmp_path):
    foreign = publish(held_bus, "worker", "colleague", time.time() - AwaitingTeam.seconds - 2)
    ctx = team_as(held_bus.root, "colleague")
    reply = tmp_path / "peer-reply.txt"
    reply.write_text("The independent peer is maintaining its own work.")
    peer = PeerCaller("outsider")
    SayVerb(acting_as=peer, units="#comms", body_file=str(reply)).apply(ctx)
    posted = held_bus.bus.bus.lookup(held_bus.bus.bus.revision())
    assert posted.sender == "outsider" and posted.answers == ()
    assert foreign.reference in {rule.message.reference for rule in OwedAnswer.opened(
        held_bus, held_bus.registry.require("colleague").incarnation)}
    with pytest.raises(RecordProblems, match="not an open ask owed by outsider"):
        SayVerb(acting_as=peer, units="worker", body_file=str(reply),
                answers=(foreign.message_id,)).apply(ctx)
    own = publish(held_bus, "worker", "outsider", time.time() - AwaitingTeam.seconds - 1)
    with pytest.raises(RecordProblems, match=own.message_id):
        SayVerb(acting_as=peer, units="#comms", body_file=str(reply)).apply(ctx)
    SayVerb(acting_as=peer, units="worker", body_file=str(reply),
            answers=(own.message_id,)).apply(ctx)
    assert held_bus.bus.bus.lookup(held_bus.bus.bus.revision()).answers == (own.reference,)
    assert foreign.reference in {rule.message.reference for rule in OwedAnswer.opened(
        held_bus, held_bus.registry.require("colleague").incarnation)}


def test_bound_child_owes_actual_parents_directive_not_the_storage_leads_mail(held_bus, tmp_path):
    foreign = publish(held_bus, "worker", "colleague", time.time() - AwaitingTeam.seconds - 2)
    directive = publish(held_bus, "outsider", "worker", time.time() - 3600)
    store = held_bus.goals.todos
    request = store.add_request(held_bus.registry.require("worker").incarnation,
                                "The actual parent's request stays with that parent.")
    store.record_directive(request.id, RequestDirective(directive.seq, time.time() - 1))
    child = ForkCaller("answer-child")
    child.join(held_bus, "worker")
    ctx = team_as(held_bus.root, "colleague")
    reply = tmp_path / "child-reply.txt"
    reply.write_text("The real child answers its actual parent's directive.")
    # The raw child identity has no parent authority merely from its provenance.
    SayVerb(acting_as=PeerCaller(child.name), units="#comms", body_file=str(reply)).apply(ctx)
    with pytest.raises(RecordProblems, match="not an open ask owed by answer-child"):
        SayVerb(acting_as=PeerCaller(child.name), units="outsider", body_file=str(reply),
                answers=(directive.message_id,)).apply(ctx)
    with pytest.raises(RecordProblems, match=directive.message_id) as refused:
        SayVerb(fork=child, units="#comms", body_file=str(reply)).apply(ctx)
    assert str(foreign.message_id) not in str(refused.value)
    SayVerb(fork=child, units="outsider", body_file=str(reply),
            answers=(directive.message_id,)).apply(ctx)
    posted = held_bus.bus.bus.lookup(held_bus.bus.bus.revision())
    assert posted.sender == child.name and posted.answers == (directive.reference,)
    assert foreign.reference in {rule.message.reference for rule in OwedAnswer.opened(
        held_bus, held_bus.registry.require("colleague").incarnation)}
    assert not any(rule.message.reference == directive.reference
                   for rule in OwedAnswer.opened(held_bus, held_bus.registry.require("worker").incarnation))
    held_bus.registry.unregister(child.name)
    held_bus.registry.remove(child.name)
    assert not any(rule.message.reference == directive.reference
                   for rule in OwedAnswer.opened(held_bus, held_bus.registry.require("worker").incarnation))
    assert foreign.reference in {rule.message.reference for rule in OwedAnswer.opened(
        held_bus, held_bus.registry.require("colleague").incarnation)}


def test_shared_request_directive_is_owed_only_by_its_frozen_recipient(held_bus):
    request = held_bus.goals.todos.add_request(
        held_bus.registry.require("colleague").incarnation, "A shared task with a private instruction.")
    ConnectorSendTool(target="worker", request=request.id,
                      body="Read the shared task and report the result.").apply(
                          held_bus, DirectorCaller("outsider"))
    recorded = held_bus.goals.todos.owner_request(request.id)
    directive, = recorded.directives
    worker = held_bus.registry.require("worker").incarnation
    colleague = held_bus.registry.require("colleague").incarnation
    assert {rule.message.seq for rule in OwedAnswer.opened(held_bus, worker)} == {directive.seq}
    assert directive.seq not in {rule.message.seq for rule in OwedAnswer.opened(held_bus, colleague)}
    assert recorded.teammate == colleague


def test_answer_reader_refuses_a_reborn_parent_between_actual_registry_cuts(held_bus):
    child = ForkCaller("parent-rebirth-reader")
    child.join(held_bus, "worker")
    ctx = Team.read_root(held_bus.root).model_copy(update={"actor": child})
    represented = ctx.actor_identity.represented_owners
    parent = held_bus.registry.require("worker")
    assert represented[1] == parent.incarnation
    held_bus.registry.unregister(parent.name)
    held_bus.registry.remove(parent.name)
    replacement = replace(parent, created_at=parent.created_at + 1)
    held_bus.registry.register(replacement)
    assert held_bus.registry.require(parent.name).incarnation == replacement.incarnation
    assert held_bus.registry.require(parent.name).incarnation != parent.incarnation
    directive = publish(held_bus, "outsider", replacement.name, time.time() - 3600)
    store = held_bus.goals.todos
    request = store.add_request(replacement.incarnation, "The replacement's private request.")
    store.record_directive(request.id, RequestDirective(directive.seq, time.time() - 1))
    assert not represented[0].represents(replacement.incarnation)
    assert OwedAnswer.opened(held_bus, represented[0]) == ()
    with pytest.raises(RelationViolationError, match="Answer owner worker changed incarnation"):
        OwedAnswer.opened(held_bus, represented[1])
    assert directive.reference in {
        rule.message.reference for rule in OwedAnswer.opened(held_bus, replacement.incarnation)}


def test_an_overdue_owner_directive_gates_until_answered(held_bus, tmp_path):
    directive = held_bus.bus.publisher.publish_ordinary(
        Message(sender="outsider", target="colleague", type=InfoKind, body="Ship the install now",
                timestamp=time.time() - 3600), destination=CommandTarget.named("colleague"), origin=AgentOrigin(),
    )
    store = held_bus.goals.todos
    selected = NewestOpenRequest().select(store, held_bus.registry.require('colleague').incarnation)
    store.record_directive(selected[0].id, RequestDirective(
        directive.seq, directive.timestamp + RequestDirective.window))
    ctx = team_as(held_bus.root, "colleague")
    reply = tmp_path / "reply.txt"
    reply.write_text("Installing now")
    with pytest.raises(RecordProblems, match=re.escape(
        f"ACT colleague's owner directive {directive.seq}:{directive.message_id}"
    )):
        SayVerb(units="#comms", body_file=str(reply)).apply(ctx)
    SayVerb(units="#comms", body_file=str(reply), answers=(directive.message_id,)).apply(ctx)
    SayVerb(units="#comms", body_file=str(reply)).apply(ctx)
    before = held_bus.bus.bus.revision()
    with pytest.raises(RecordProblems, match="not an open ask owed by colleague"):
        SayVerb(units="#comms", body_file=str(reply), answers=(directive.message_id,)).apply(ctx)
    assert held_bus.bus.bus.revision() == before
    assert held_bus.bus.bus.lookup(held_bus.bus.bus.revision()).answers == ()


@pytest.mark.parametrize('body', ['@outsider This question is yours', 'The worker broadcasts its result'])
def test_channel_messages_without_a_team_addressee_are_not_a_question(held_bus, body):
    held_bus.messaging.send('worker', '#comms', body, type=QuestionKind)
    with held_bus.bus.bus.reading() as source:
        questions = UnansweredTeammateAsks.for_orders(
            source, held_bus.registry.snapshot(), 'colleague',
            HeldOrders.read(held_bus).holders, time.time(),
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
        run = team('--root', str(held_bus.root), 'order', *arguments,
                   *(() if '--as' in arguments else ('--as', 'colleague')))
        assert run.returncode == 0, run.stdout + run.stderr

    # The idle outsider takes it: the worker already holds P1 and P2, and the allocation
    # policy refuses a third order to a holder while a running teammate holds none.
    # Problem creates the first order through Team.send, including its message link.
    cli('problem', str(order_file), '--to', 'outsider', '--where', 'owner.py:2', '--as', 'outsider')
    first = held_bus.goals.todos.get('P3')
    assert first.latest_message.message_id
    document = replace(document, problems=('P3',),
                       order=replace(document.order, id=OrderId.mint()))
    order_file.write_text(json.dumps(FieldCodec.encode(document)))
    cli('send', 'outsider', str(order_file), '--clean', '--as', 'outsider')
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


@pytest.mark.asyncio
async def test_one_state_per_teammate_even_when_blocked_and_detached(held_bus, goal_turn_owners):
    held_bus.registry.register(held_bus.registry.require("worker"), StoppedThreadStatus())
    detached, = state_cards(held_bus, Detached)
    assert detached.unit.name == "worker" and set(detached.orders) == {"P1", "P2"}
    await blocked_holder(held_bus, goal_turn_owners)
    blocked, = state_cards(held_bus, Blocked)
    frame = capture_frame(held_bus, ProgressRead())
    states = TeammateState.current(held_bus, frame)
    assert tuple(state for state in states if state.unit == blocked.unit) == (blocked,)
    assert blocked not in StandingRule.broken(
        held_bus, observed=states, frame=frame)


@pytest.mark.asyncio
async def test_startup_heartbeat_enforces_and_publishes_both_new_rule_lines(held_bus, goal_turn_owners):
    await blocked_holder(held_bus, goal_turn_owners)
    question = publish(held_bus, "worker", "colleague", time.time() - 901)
    expected = tuple(rule.line() for rule in (
        *state_cards(held_bus, Blocked), *unanswered(held_bus),
    ))
    heartbeat = TeamHeartbeat()
    colleague = held_bus.registry.require("colleague")
    held_bus.registry.register(replace(colleague, process_identity=ProcessIdentity.capture(os.getpid())))
    # The assertion reads the published board message; publication is off by default.
    held_bus.threads.set_heartbeat_delivery("colleague", OnHeartbeatDelivery)
    await heartbeat.run(lambda: nullcontext(held_bus))
    saved, = TeamUpdate.latest(held_bus, colleague.incarnation)
    assert saved.handled
    assert isinstance(held_bus.registry.require("worker").goal.require().state, ActiveGoal)
    owner = held_bus.messaging.user_identity(Checkout(held_bus.root)).name
    page = MessagePageRequest(DMDisplayScope.capture(
        owner, "colleague", held_bus.registry.snapshot(),
    )).read_opened(held_bus.bus.bus)
    message = next(message for message in page.messages if message.task == saved)
    # The compact body truncates lines; the original board entries keep every complete alert.
    board = tuple(entry.text for entry in message.task.board(held_bus))
    assert set(expected) <= set(board)
    assert any(question.reference.message_id in text for text in board)


def test_successful_pass_health_does_not_depend_on_messages(held_bus, monkeypatch):
    colleague = held_bus.registry.require("colleague")
    held_bus.registry.register(replace(colleague, process_identity=ProcessIdentity.capture(os.getpid())))
    passes = ServicePassStore.at(held_bus.root)
    passes.passed(TeamHeartbeat.__name__)
    assert not passes.read().teammate_completed
    worker = held_bus.registry.require("worker").incarnation
    observed = (colleague.incarnation, worker)
    passes.heartbeat_passed((worker,), observed)
    passes.heartbeat_passed((colleague.incarnation,), observed)
    completed = passes.read().teammate_completed[colleague.incarnation]
    assert HeartbeatSilent.violations(held_bus) == ()
    # The longest normal pass is quiet, not silent.
    monkeypatch.setattr(time, "time", lambda: completed + TeamHeartbeat.pass_duration)
    assert HeartbeatSilent.violations(held_bus) == ()
    before = held_bus.bus.bus.revision()
    later = completed + HeartbeatSilent.seconds + 1
    monkeypatch.setattr(time, "time", lambda: later)
    silent, = HeartbeatSilent.violations(held_bus)
    assert isinstance(silent, MissedHeartbeat)
    assert silent.since == completed + HeartbeatSilent.seconds
    # Another teammate's pass cannot clear this alarm.
    passes.heartbeat_passed((worker,), observed)
    assert HeartbeatSilent.violations(held_bus) == (silent,)
    # A successful quiet pass clears the alarm without inventing a conversation.
    passes.heartbeat_passed((colleague.incarnation,), observed)
    assert HeartbeatSilent.violations(held_bus) == ()
    assert held_bus.bus.bus.revision() == before


def test_pass_missing_a_restarting_team_keeps_its_completion(held_bus):
    """An install leaves the colleague's process dead for one pass, which retires nothing."""
    colleague = held_bus.registry.require("colleague")
    alive = replace(colleague, process_identity=ProcessIdentity.capture(os.getpid()))
    heartbeat = TeamHeartbeat()
    held_bus.registry.register(alive)
    asyncio.run(heartbeat.run(lambda: nullcontext(held_bus)))
    passes = ServicePassStore.at(held_bus.root)
    completed = passes.read().teammate_completed[colleague.incarnation]
    held_bus.registry.register(colleague)
    asyncio.run(heartbeat.run(lambda: nullcontext(held_bus)))
    # Stopped for one pass, it still holds the owner's open request, so it keeps its frame.
    assert [hud.teammate for hud in TeamFrame.read_all(held_bus.root)] == [colleague.incarnation]
    held_bus.registry.register(alive)
    assert passes.read().teammate_completed[colleague.incarnation] >= completed
    assert HeartbeatSilent.violations(held_bus) == ()


def test_first_pass_is_owed_from_oldest_request_and_survives_renames(held_bus, monkeypatch):
    colleague = held_bus.registry.require("colleague")
    held_bus.registry.register(replace(colleague, process_identity=ProcessIdentity.capture(os.getpid())))
    request, = TeamHeartbeat.requested_by(held_bus.registry.require("colleague"), held_bus.goals)
    due = request.created_at + HeartbeatSilent.seconds
    monkeypatch.setattr(time, "time", lambda: due + 1)
    never, = HeartbeatSilent.violations(held_bus)
    assert isinstance(never, HeartbeatNotStarted)
    assert never.request == request.id and never.since == due
    held_bus.goals.todos.add_request(colleague.incarnation, "A newer independent request")
    held_bus.messaging.send_user_message(
        ThreadTarget("colleague"), "A DM is not a completed pass", worktree=Checkout(held_bus.root))
    assert HeartbeatSilent.violations(held_bus) == (never,)
    passes = ServicePassStore.at(held_bus.root)
    observed = (colleague.incarnation,)
    passes.heartbeat_passed(observed, observed)
    completed = passes.read().teammate_completed[colleague.incarnation]
    assert HeartbeatSilent.violations(held_bus) == ()
    held_bus.threads.rename_self("renamed-colleague", caller=ThreadCaller("colleague"))
    monkeypatch.setattr(time, "time", lambda: completed + HeartbeatSilent.seconds + 1)
    silent, = HeartbeatSilent.violations(held_bus)
    assert isinstance(silent, MissedHeartbeat)
    assert silent.since == completed + HeartbeatSilent.seconds
    renamed = (held_bus.registry.require("renamed-colleague").incarnation,)
    passes.heartbeat_passed(renamed, renamed)
    assert HeartbeatSilent.violations(held_bus) == ()


def test_one_answer_covers_an_escalation_burst_sent_before_it(held_bus):
    now = time.time()
    burst = [escalate(held_bus, VibeCheckNotice, now - 30 + line) for line in range(4)]
    owner = burst[0].sender
    assert len(VibeCheckNotice.unanswered(held_bus.bus.bus, held_bus.registry.snapshot(), "colleague", time.time())) == 4
    publish(held_bus, "colleague", owner, now - 20, answers=(burst[0].reference,))
    assert VibeCheckNotice.unanswered(held_bus.bus.bus, held_bus.registry.snapshot(), "colleague", time.time()) == ()
    later = escalate(held_bus, VibeCheckNotice, now - 10)
    assert [m.message_id for m in VibeCheckNotice.unanswered(held_bus.bus.bus, held_bus.registry.snapshot(), "colleague", time.time())] == [
        later.message_id]


def test_an_answer_covers_by_bus_order_whatever_its_clock_says(held_bus):
    """Answered is decided by bus sequence alone: a reply the bus ordered after the alarm covers
    it even when the replier's clock ran behind, and a line the bus ordered after the reply stays
    owed even when its clock ran behind the reply's."""
    now = time.time()
    check = escalate(held_bus, VibeCheckNotice, now - 10)
    reply = publish(held_bus, "colleague", check.sender, now - 20, answers=(check.reference,))
    assert reply.seq == check.seq + 1 and reply.timestamp < check.timestamp
    for beat in (time.time(), time.time() + VibeCheckUnanswered.seconds):
        assert VibeCheckNotice.unanswered(held_bus.bus.bus, held_bus.registry.snapshot(), "colleague", beat) == ()
    later = escalate(held_bus, VibeCheckNotice, now - 30)
    assert [m.reference for m in VibeCheckNotice.unanswered(
        held_bus.bus.bus, held_bus.registry.snapshot(), "colleague", time.time())] == [later.reference]


def test_answering_the_owner_does_not_answer_his_escalation(held_bus):
    """Escalations share the owner's thread with everything he types; only an answer naming a
    line of the burst covers it, and an escalation never becomes his request or his words."""
    now = time.time()
    check = escalate(held_bus, VibeCheckNotice, now - 30)
    typed = held_bus.messaging.send_user_message(
        ThreadTarget("colleague"), "Something I typed", worktree=Checkout(held_bus.root))
    publish(held_bus, "colleague", typed.sender, now - 10, answers=(typed.reference,))
    assert [m.reference for m in VibeCheckNotice.unanswered(
        held_bus.bus.bus, held_bus.registry.snapshot(), "colleague", time.time())] == [check.reference]
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


@pytest.mark.asyncio
async def test_a_compliant_answer_resumes_a_held_teammate_whose_attempt_was_interrupted(
    held_bus, goal_turn_owners,
):
    """A held teammate whose last attempt failed refuses a status-only resume; the chokehold's
    release continues its generation the way a fresh order does, so one such teammate neither stays
    paused nor stops the release of the others (the heartbeat died on it, 2026-10-21 22:43)."""
    from agent_comms.planning.goal_actions import (
        ActiveGoalAction,
        ChokeholdPausedGoalAction,
        CompliedGoalAction,
        OwnerInvocable,
    )

    await blocked_holder(held_bus, goal_turn_owners)
    held_bus.goals.update_goal("worker", ChokeholdPausedGoalAction())
    with pytest.raises(ValueError, match="unresolved"):
        held_bus.goals.update_goal("worker", ActiveGoalAction(), actor=OwnerInvocable)
    held_bus.goals.update_goal(
        "worker", CompliedGoalAction(witness=MessageId("c0ffee000001")), actor=OwnerInvocable)
    assert isinstance(held_bus.registry.require("worker").goal.require().state, ActiveGoal)


def test_a_compliant_answer_resumes_a_held_teammate_whose_problems_closed_during_the_hold(held_bus):
    """The heartbeat died on every pass from 13:08 on 2026-10-24: a teammate whose problems were
    withdrawn while the chokehold held it refused the release, so no teammate after it resumed."""
    from agent_comms.planning.goal_actions import (
        ChokeholdPausedGoalAction,
        CompliedGoalAction,
        OwnerInvocable,
    )
    from agent_comms.planning.order_words import StrikeReason
    from agent_comms.planning.orders import ReleaseTodo, WithdrawTodo

    held_bus.goals.update_goal("worker", ChokeholdPausedGoalAction())
    colleague = held_bus.registry.require("colleague").incarnation
    worker = held_bus.registry.require("worker")
    todos = held_bus.goals.todos
    for todo in todos.for_goal(worker.incarnation, worker.goal.require().id):
        released = todos.apply(todo.id, ReleaseTodo(todo.revision, todo.held().owner_identity, todo.held(), StrikeReason(
            "The holder explicitly releases its exact assignment before the repair moves elsewhere.")))
        todos.apply(todo.id, WithdrawTodo(
            released.revision, colleague,
            "The requested repair already merged into main through another problem."))
    assert todos.for_goal(worker.incarnation, worker.goal.require().id) == ()
    held_bus.goals.update_goal(
        "worker", CompliedGoalAction(witness=MessageId("c0ffee000001")), actor=OwnerInvocable)
    assert isinstance(held_bus.registry.require("worker").goal.require().state, ActiveGoal)


def test_a_restarted_heartbeat_does_not_resend_an_escalation_burst(held_bus):
    """Deduplication sees answered and unanswered bursts through the same retained bus."""
    first = "first escalation identity"
    VibeCheckNotice.burst_once(held_bus, held_bus.registry.require("colleague").incarnation, (first,), first)
    VibeCheckNotice.burst_once(held_bus, held_bus.registry.require("colleague").incarnation, (first,), first)
    sent = VibeCheckNotice.unanswered(held_bus.bus.bus, held_bus.registry.snapshot(), "colleague", time.time())
    assert len(sent) == 1
    publish(held_bus, "colleague", sent[0].sender, time.time(), answers=(sent[0].reference,))
    VibeCheckNotice.burst_once(held_bus, held_bus.registry.require("colleague").incarnation, (first,), first)
    assert not VibeCheckNotice.unanswered(held_bus.bus.bus, held_bus.registry.snapshot(), "colleague", time.time())
    second = "second escalation identity"
    VibeCheckNotice.burst_once(held_bus, held_bus.registry.require("colleague").incarnation, (second,), second)
    assert [message.body for message in VibeCheckNotice.unanswered(
        held_bus.bus.bus, held_bus.registry.snapshot(), "colleague", time.time())] == [second]
